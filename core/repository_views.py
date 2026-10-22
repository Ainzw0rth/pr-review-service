"""Owner-only local repository settings and explicit, retryable GitHub setup."""

import fcntl
import hashlib
import ipaddress
import json
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from .forms import RepositorySettingsForm
from .github import GitHub, IntegrationError
from .models import Credential, Organization, Repository
from .retries import is_retry_owner
from .secrets import decrypt, encrypt

CONNECT_ERROR = "Repository saved. GitHub setup failed. Check token repository/admin access, then retry Connect."


def webhook_url(repository):
    base = getattr(settings, "WEBHOOK_PUBLIC_URL", "") or settings.PUBLIC_URL
    return f"{base.rstrip('/')}/webhooks/github/{repository.pk}/"


def public_webhook_configured():
    base = getattr(settings, "WEBHOOK_PUBLIC_URL", "") or settings.PUBLIC_URL
    try:
        parsed = urlsplit(base)
        parsed.port
    except ValueError:
        return False
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in base)
    ):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host and not host.endswith((".localhost", ".local"))


def repository_revision(repository):
    return hashlib.sha256(
        json.dumps(
            [
                (field.attname, field.value_from_object(repository))
                for field in repository._meta.concrete_fields
            ],
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()[:16]


@contextmanager
def repository_lock(repository_id):
    # Same lock covers local edits and remote setup. No DB transaction spans network I/O.
    identity = f"{settings.DATABASES['default']['NAME']}:{repository_id}"
    name = hashlib.sha256(identity.encode()).hexdigest()
    with (Path(tempfile.gettempdir()) / f"reviewdesk-repository-{name}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IntegrationError("Connection is already in progress. Retry shortly.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def connect_repository(repository_id):
    if not public_webhook_configured():
        return False
    try:
        with repository_lock(repository_id):
            repo = Repository.objects.select_related("github_credential", "webhook_credential").get(
                pk=repository_id
            )
            with GitHub(repo) as github:
                metadata = github.get("/")
                if (
                    not isinstance(metadata, dict)
                    or not isinstance(metadata.get("full_name"), str)
                    or metadata["full_name"].lower() != repo.full_name.lower()
                ):
                    raise IntegrationError("Repository access unavailable.")
                secret = decrypt(repo.webhook_credential.encrypted_value)
                labels = github.pages("/labels")
                existing = {row.get("name") for row in labels if isinstance(row, dict)}
                for label, color in [
                    (repo.review_label, "5319e7"),
                    (repo.feedback_label, "fbca04"),
                    (repo.approved_label, "0e8a16"),
                ]:
                    if label not in existing:
                        github.post("/labels", {"name": label, "color": color})
                hooks = github.pages("/hooks")
                matching = next(
                    (
                        hook
                        for hook in hooks
                        if isinstance(hook, dict)
                        and isinstance(hook.get("config"), dict)
                        and hook["config"].get("url") == webhook_url(repo)
                    ),
                    None,
                )
                payload = {
                    "name": "web",
                    "active": True,
                    "events": ["pull_request"],
                    "config": {
                        "url": webhook_url(repo),
                        "content_type": "json",
                        "secret": secret,
                        "insecure_ssl": "0",
                    },
                }
                if matching:
                    hook_id = matching.get("id")
                    if type(hook_id) is not int or hook_id < 1:
                        raise IntegrationError("Webhook configuration unavailable.")
                    result = github.patch(f"/hooks/{hook_id}", payload)
                else:
                    result = github.post("/hooks", payload)
                if (
                    not isinstance(result, dict)
                    or type(result.get("id")) is not int
                    or result["id"] < 1
                ):
                    raise IntegrationError("Webhook configuration unavailable.")
                Repository.objects.filter(pk=repo.pk).update(webhook_id=str(result["id"]))
        return True
    except (IntegrationError, InvalidToken, ValueError, TypeError, RuntimeError):
        return False


def _feedback(request, connected):
    if connected:
        messages.success(request, "GitHub connected. Labels and webhook ready.", extra_tags="toast")
    elif not public_webhook_configured():
        messages.warning(
            request,
            "Repository saved. A public HTTPS webhook URL is needed to receive GitHub events. "
            "Configure WEBHOOK_PUBLIC_URL for your tunnel, then verify the connection.",
            extra_tags="toast",
        )
    else:
        messages.error(request, CONNECT_ERROR, extra_tags="toast")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
@sensitive_post_parameters("github_token")
def repository_edit(request, repository_id=None):
    if not is_retry_owner(request.user):
        return HttpResponseForbidden("Only the configured owner can manage repositories.")
    repository = get_object_or_404(Repository, pk=repository_id) if repository_id else None
    initial = {}
    if not repository:
        source = Repository.objects.filter(enabled=True).order_by("pk").first()
        if source:
            initial = {
                "organization": source.organization_id,
                "reviewer": source.reviewer,
                "github_credential": source.github_credential_id,
                "graph_enabled": False,
            }
        else:
            organization = (
                Organization.objects.filter(membership__user=request.user).first()
                or Organization.objects.order_by("pk").first()
            )
            initial = {"organization": organization, "reviewer": request.user.username}
    version = repository_revision(repository) if repository else ""
    form = RepositorySettingsForm(
        request.POST if request.method == "POST" else None, instance=repository, initial=initial
    )
    error, status = "", 200
    if request.method == "POST":
        try:
            with repository_lock(repository_id or "new"):
                with transaction.atomic():
                    if repository:
                        current = Repository.objects.select_for_update().get(pk=repository.pk)
                        if request.POST.get("version") != repository_revision(current):
                            error, status = (
                                "Repository changed in another tab. Reload before saving.",
                                409,
                            )
                            version = request.POST.get("version", "")
                    if not error and form.is_valid():
                        saved = form.save(commit=False)
                        if form.cleaned_data.get("github_token"):
                            saved.github_credential = Credential.objects.create(
                                name=f"GitHub {saved.full_name[:60]} {secrets.token_hex(4)}",
                                encrypted_value=form.encrypted_token,
                            )
                        if not repository:
                            saved.webhook_credential = Credential.objects.create(
                                name=f"Webhook {secrets.token_hex(12)}",
                                encrypted_value=encrypt(secrets.token_urlsafe(48)),
                            )
                        saved.save()
                    elif not error:
                        status = 400
            if status == 200:
                _feedback(request, connect_repository(saved.pk))
                return redirect("repository_edit", repository_id=saved.pk)
        except IntegrityError:
            error, status = "Repository already exists or changed. Reload before saving.", 409
        except (IntegrationError, ValueError, RuntimeError):
            error, status = (
                "Repository could not be saved. Check encryption configuration or retry shortly.",
                400,
            )
    return render(
        request,
        "core/repository_form.html",
        {
            "form": form,
            "repository": repository,
            "version": version,
            "webhook_url": webhook_url(repository) if repository else "",
            "webhook_ready": public_webhook_configured(),
            "error": error,
        },
        status=status,
    )


@login_required
@never_cache
@require_POST
def repository_connect(request, repository_id):
    if not is_retry_owner(request.user):
        return HttpResponseForbidden("Only the configured owner can manage repositories.")
    get_object_or_404(Repository, pk=repository_id)
    _feedback(request, connect_repository(repository_id))
    return redirect("repository_edit", repository_id=repository_id)


@login_required
@never_cache
@require_POST
def repository_toggle(request, repository_id):
    if not is_retry_owner(request.user):
        return HttpResponseForbidden("Only the configured owner can manage repositories.")
    get_object_or_404(Repository, pk=repository_id)
    if request.POST.get("enabled") not in ("0", "1"):
        return HttpResponseBadRequest("Choose Pause or Resume.")
    try:
        with repository_lock(repository_id), transaction.atomic():
            repository = Repository.objects.select_for_update().get(pk=repository_id)
            repository.enabled = request.POST["enabled"] == "1"
            repository.save(update_fields=["enabled"])
        messages.success(
            request,
            "Repository resumed." if repository.enabled else "Repository paused.",
            extra_tags="toast",
        )
    except IntegrationError:
        messages.error(request, "Connection is in progress. Retry shortly.", extra_tags="toast")
    return redirect("service_settings")

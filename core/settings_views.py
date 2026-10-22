"""Owner-only repository behavior settings, separate from model routing."""

import hashlib
import json
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .capacity import limit
from .models import Repository, ServiceSettings
from .retries import is_retry_owner


def revision(repositories, capacity, cli_saved, cli_effective):
    return hashlib.sha256(
        json.dumps(
            [
                capacity,
                cli_saved,
                cli_effective,
                [
                    (
                        r.pk,
                        r.auto_approve,
                        r.auto_merge,
                        r.merge_method,
                        r.related_item_limit,
                        r.enabled,
                        r.graph_enabled,
                        r.github_credential_id,
                        r.webhook_id,
                    )
                    for r in repositories
                ],
            ]
        ).encode()
    ).hexdigest()[:16]


def related_limit(data, repo):
    if data.get(f"related_unlimited_{repo.pk}") == "on":
        return None
    key = f"related_item_limit_{repo.pk}"
    if key not in data:
        return repo.related_item_limit
    value = data[key].strip()
    if not re.fullmatch(r"[0-9]{1,10}", value) or not 1 <= int(value) <= 2147483647:
        raise ValueError(
            "Related-item limit must be a positive whole number, or select No item limit."
        )
    return int(value)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def service_settings(request):
    if not is_retry_owner(request.user):
        return HttpResponseForbidden("Only the configured owner can change service settings.")
    with transaction.atomic():
        ServiceSettings.objects.get_or_create(pk=1)
        service = ServiceSettings.objects.select_for_update().get(pk=1)
        capacity = service.concurrent_pr_reviews
        capacity_value = request.POST.get("concurrent_pr_reviews", capacity)
        capacity_error = ""
        cli_saved = service.cli_account_concurrency
        cli_effective = cli_saved if cli_saved is not None else limit("CLI_ACCOUNT_CONCURRENCY", 1)
        cli_value = request.POST.get("cli_account_concurrency", cli_effective)
        cli_error = ""
        repositories = list(Repository.objects.select_for_update().order_by("full_name"))
        version = revision(repositories, capacity, cli_saved, cli_effective)
        error = ""
        status = 200
        if request.method == "POST":
            if request.POST.get("version") != version:
                error = "Settings changed in another tab. Reload before saving."
                status = 409
                version = request.POST.get("version", "")
            elif any(
                request.POST.get(f"{field}_{repo.pk}", "") not in ("", "on")
                for repo in repositories
                for field in ("auto_approve", "auto_merge", "related_unlimited")
            ):
                error = "Invalid toggle value."
                status = 400
            elif any(
                request.POST.get(f"merge_method_{repo.pk}", repo.merge_method)
                not in ("squash", "merge", "rebase")
                for repo in repositories
            ):
                error = "Invalid merge method."
                status = 400
            elif any(
                request.POST.get(f"auto_merge_{repo.pk}") == "on"
                and request.POST.get(f"auto_approve_{repo.pk}") != "on"
                for repo in repositories
            ):
                error = "Automatic merging requires automatic approval for the same repository."
                status = 400
            else:
                try:
                    if "concurrent_pr_reviews" in request.POST:
                        if not re.fullmatch(r"[1-8]", capacity_value):
                            capacity_error = "Choose a whole number from 1 to 8."
                            raise ValueError(capacity_error)
                        capacity = int(capacity_value)
                    if "cli_account_concurrency" in request.POST:
                        if not re.fullmatch(r"(?:[1-9]|[12][0-9]|3[0-2])", cli_value):
                            cli_error = "Choose a whole number from 1 to 32."
                            raise ValueError(cli_error)
                        cli_saved = int(cli_value)
                    limits = {repo.pk: related_limit(request.POST, repo) for repo in repositories}
                except ValueError as exc:
                    error = str(exc)
                    status = 400
                else:
                    service.concurrent_pr_reviews = capacity
                    service.cli_account_concurrency = cli_saved
                    service.save(update_fields=["concurrent_pr_reviews", "cli_account_concurrency"])
                    for repo in repositories:
                        repo.auto_approve = request.POST.get(f"auto_approve_{repo.pk}") == "on"
                        repo.auto_merge = request.POST.get(f"auto_merge_{repo.pk}") == "on"
                        repo.merge_method = request.POST.get(
                            f"merge_method_{repo.pk}", repo.merge_method
                        )
                        repo.related_item_limit = limits[repo.pk]
                        repo.save(
                            update_fields=[
                                "auto_approve",
                                "auto_merge",
                                "merge_method",
                                "related_item_limit",
                            ]
                        )
                    messages.success(
                        request,
                        "Settings saved.",
                        extra_tags="toast",
                    )
                    return redirect("service_settings")
    return render(
        request,
        "core/settings.html",
        {
            "repositories": repositories,
            "version": version,
            "error": error,
            "capacity_value": capacity_value,
            "capacity_error": capacity_error,
            "cli_value": cli_value,
            "cli_error": cli_error,
        },
        status=status,
    )

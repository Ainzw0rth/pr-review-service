"""Owner-only repository behavior settings, separate from model routing."""

import hashlib
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import Repository
from .retries import is_retry_owner


def revision(repositories):
    return hashlib.sha256(
        json.dumps([(r.pk, r.auto_approve) for r in repositories]).encode()
    ).hexdigest()[:16]


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def service_settings(request):
    if not is_retry_owner(request.user):
        return HttpResponseForbidden("Only the configured owner can change service settings.")
    with transaction.atomic():
        repositories = list(Repository.objects.select_for_update().order_by("full_name"))
        version = revision(repositories)
        error = ""
        status = 200
        if request.method == "POST":
            if request.POST.get("version") != version:
                error = "Settings changed in another tab. Reload before saving."
                status = 409
            elif any(
                request.POST.get(f"auto_approve_{repo.pk}", "") not in ("", "on")
                for repo in repositories
            ):
                error = "Invalid toggle value."
                status = 400
            else:
                for repo in repositories:
                    repo.auto_approve = request.POST.get(f"auto_approve_{repo.pk}") == "on"
                    repo.save(update_fields=["auto_approve"])
                messages.success(
                    request,
                    "Settings saved. Enabling applies to new jobs; disabling prevents pending approvals. The service never merges.",
                )
                return redirect("service_settings")
    return render(
        request,
        "core/settings.html",
        {"repositories": repositories, "version": version, "error": error},
        status=status,
    )

"""Owner-only workflow editor. No credentials or arbitrary runner flags cross this boundary."""

import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import ModelProbe, Provider
from .retries import is_retry_owner
from .review_config import Conflict, library, save_library, version


def owner_context(request):
    return {"workflow_owner": is_retry_owner(request.user)}


def state():
    rows = library()
    probes = []
    for probe in ModelProbe.objects.order_by("-id")[:20]:
        probes.append(
            {
                "provider_id": probe.provider_id,
                "model": probe.configuration.get("model"),
                "status": probe.status,
                "reason": probe.reason,
            }
        )
    return {
        "version": version(rows),
        "models": [{k: v for k, v in row.items() if k != "credential_id"} for row in rows],
        "probes": probes,
    }


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def workflow_page(request):
    if not is_retry_owner(request.user):
        return HttpResponseForbidden("Only the configured owner can configure workflows.")
    if request.method != "GET":
        return JsonResponse({"error": "Use the workflow save endpoint."}, status=405)
    return render(request, "core/workflow.html")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def workflow_state(request):
    if not is_retry_owner(request.user):
        return HttpResponseForbidden("Only the configured owner can configure workflows.")
    if request.method == "POST":
        try:
            if len(request.body) > 100_000:
                raise ValueError("Workflow payload too large.")
            data = json.loads(request.body)
            if not isinstance(data, dict):
                raise ValueError("Expected a workflow object.")
            save_library(data)
        except Conflict as exc:
            return JsonResponse({"error": str(exc)}, status=409)
        except (ValueError, ValidationError) as exc:
            message = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
            return JsonResponse({"error": message}, status=400)
    return JsonResponse(state())


@login_required
@never_cache
@require_http_methods(["POST"])
@transaction.atomic
def workflow_probe(request, provider_id):
    if not is_retry_owner(request.user):
        return HttpResponseForbidden("Only the configured owner can test models.")
    provider = Provider.objects.select_for_update().filter(pk=provider_id).first()
    if not provider or not provider.kind.endswith("_cli"):
        return JsonResponse({"error": "Select a saved CLI model."}, status=400)
    if ModelProbe.objects.filter(status__in=["queued", "running"]).exists():
        return JsonResponse(
            {"error": "A connection test is already pending. Wait for it to finish."}, status=409
        )
    row = next(row for row in library() if row["id"] == provider_id)
    ModelProbe.objects.create(provider=provider, configuration=row)
    return JsonResponse(
        {
            "message": "Connection test queued. It runs after active reviews and may consume subscription quota."
        },
        status=202,
    )

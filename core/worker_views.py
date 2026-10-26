"""Owner-only fleet controls. Browser payloads never include worker credentials."""

from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import worker_registry as registry
from .models import ExecutionWorker, JobClaim, ServiceSettings, WorkerOwner
from .retries import is_retry_owner


def owner_only(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_retry_owner(request.user):
            return HttpResponseForbidden("Owner access required")
        return view(request, *args, **kwargs)

    return never_cache(wrapped)


def fleet_state():
    from .models import ExecutionAssignment

    now = timezone.now()
    workers = []
    draining = ServiceSettings.objects.filter(pk=1, draining=True).exists()
    owners = WorkerOwner.objects.filter(retired=False)
    claims = list(JobClaim.objects.select_related("pr__repository", "job"))
    fresh = owners.filter(heartbeat_at__gt=now - timedelta(seconds=60)).exists()
    owner_ids = set(owners.values_list("pk", flat=True))
    local_claims = [c for c in claims if c.owner_id in owner_ids]
    workers.append(
        {
            "id": "local",
            "name": "Local worker",
            "availability": "Draining"
            if fresh and draining
            else "Busy"
            if fresh and local_claims
            else "Ready"
            if fresh
            else "Offline",
            "connected": fresh,
            "available": None,
            "active": len(local_claims),
            "problems": [] if fresh else ["No fresh local supervisor heartbeat"],
            "capabilities": "Local supervisor",
            "heartbeat": str(
                owners.order_by("-heartbeat_at").values_list("heartbeat_at", flat=True).first()
                or "Unknown"
            ),
            "memory": "Unknown",
            "disk": "Unknown",
            "version": "Local installation",
            "work": [
                f"{c.pr.repository.full_name} #{c.pr.number} · {c.job.status}" for c in local_claims
            ],
            "local": True,
        }
    )
    assignments = {}
    for assignment in ExecutionAssignment.objects.filter(
        state__in=["queued", "running", "cancelling", "quarantined"]
    ).select_related("job__pr__repository"):
        label = assignment.operation
        if assignment.job_id:
            label = (
                f"{assignment.job.pr.repository.full_name} #{assignment.job.pr.number} · {label}"
            )
        assignments.setdefault(str(assignment.worker_id), []).append(
            f"{label} · {assignment.state}"
        )
    for worker in ExecutionWorker.objects.order_by("created_at"):
        state = registry.availability(worker)
        connected = state in {"ready", "busy", "draining"}
        problems = []
        if not settings.REMOTE_EXECUTION_ENABLED:
            problems.append("Remote execution disabled on coordinator")
        if draining:
            problems.append("Coordinator draining")
        if not worker.enabled:
            problems.append("Scheduling disabled; validate agent before enabling")
        if not worker.authenticated:
            problems.append("Authentication required")
        if worker.protocol_version != 1:
            problems.append("Incompatible protocol version")
        if not set(worker.allowed_capabilities) & set(worker.capabilities):
            problems.append("No approved capability reported")
        if "model" in worker.allowed_capabilities and not worker.account_kinds:
            problems.append("Provider account mapping required")
        if worker.metrics.get("disk_free") is None:
            problems.append("Working disk space unknown")
        elif worker.metrics["disk_free"] < 2 * 1024**3:
            problems.append("Disk pressure")
        if any("quarantined" in a for a in assignments.get(str(worker.pk), [])):
            problems.append("Recovery required")
        from .worker_keepalive import public_status

        keepalive = public_status(worker)
        workers.append(
            {
                "id": str(worker.pk),
                "name": worker.name,
                "availability": state.title(),
                "connected": connected,
                "available": max(0, min(worker.max_slots, worker.slots) - worker.active)
                if connected and not problems and not worker.draining
                else 0,
                "active": worker.active,
                "problems": problems,
                "capabilities": ", ".join(worker.capabilities) or "Unknown",
                "heartbeat": worker.heartbeat_at.isoformat() if worker.heartbeat_at else "Never",
                "memory": f"{worker.metrics['memory_used']:,} / {worker.metrics['memory_total']:,} bytes"
                if all(k in worker.metrics for k in ["memory_used", "memory_total"])
                else "Unknown",
                "disk": f"{worker.metrics['disk_free']:,} bytes"
                if "disk_free" in worker.metrics
                else "Unknown",
                "version": worker.software_version or "Unknown",
                "work": assignments.get(str(worker.pk), []),
                "enabled": worker.enabled,
                "allowed_capabilities": worker.allowed_capabilities,
                "account_kinds": worker.account_kinds,
                "max_slots": worker.max_slots,
                "revoked": bool(worker.revoked_at),
                "keepalive": keepalive,
                "local": False,
            }
        )
    return {
        "workers": workers,
        "connected": sum(w["connected"] for w in workers),
        "available": sum(w["available"] or 0 for w in workers),
        "occupied": sum(w["active"] for w in workers),
        "attention": sum(bool(w["problems"]) or not w["connected"] for w in workers),
        "updated_at": now.isoformat(),
    }


@require_GET
@owner_only
def workers(request):
    return render(request, "core/workers.html", fleet_state())


@require_GET
@owner_only
def workers_state(request):
    return JsonResponse(fleet_state())


@require_POST
@owner_only
@transaction.atomic
def worker_action(request):
    action = request.POST.get("action")
    if action == "pair":
        return render(
            request,
            "core/workers.html",
            {**fleet_state(), "pairing_code": registry.create_enrollment()},
        )
    from uuid import UUID

    try:
        worker_id = UUID(request.POST.get("worker_id", ""))
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({"error": "Invalid worker"}, status=400)
    from .claims import pool

    pool()  # Match dispatcher/registry lock order before mutating worker identity.
    worker = get_object_or_404(ExecutionWorker.objects.select_for_update(), pk=worker_id)
    try:
        if action == "revoke":
            registry.revoke(worker.pk)
        elif worker.revoked_at:
            raise ValueError("Revoked workers cannot be changed. Pair again.")
        elif action in {"drain", "resume"}:
            worker.draining = action == "drain"
            worker.save(update_fields=["draining"])
        elif action == "configure":
            capabilities = request.POST.getlist("capabilities")
            accounts = request.POST.getlist("account_kinds")
            slots = int(request.POST.get("max_slots", ""))
            if (
                not set(capabilities) <= {"model", "test", "graph"}
                or not set(accounts) <= {"codex_cli", "claude_cli"}
                or not 1 <= slots <= 32
            ):
                raise ValueError("Invalid scheduling configuration")
            enabled = request.POST.get("enabled") == "on"
            if enabled and (not capabilities or ("model" in capabilities and not accounts)):
                raise ValueError("Select capabilities and map provider accounts before enabling")
            worker.enabled, worker.allowed_capabilities, worker.account_kinds, worker.max_slots = (
                enabled,
                capabilities,
                accounts,
                slots,
            )
            worker.save(
                update_fields=["enabled", "allowed_capabilities", "account_kinds", "max_slots"]
            )
        elif action in {"check", "keepalive"}:
            from .worker_keepalive import configure, request_check

            if action == "check":
                request_check(worker)
            else:
                data = request.POST.dict()
                data["enabled"] = request.POST.get("enabled") == "on"
                data["interval_minutes"] = int(request.POST.get("interval_minutes", "20"))
                if request.POST.get("port"):
                    data["port"] = int(request.POST["port"])
                configure(worker, data)
        else:
            raise ValueError("Unknown worker action")
    except (ValueError, TypeError):
        return render(
            request,
            "core/workers.html",
            {
                **fleet_state(),
                "error": "Unable to apply action. Check configuration and worker state.",
            },
            status=400,
        )
    messages.success(request, "Worker action applied.")
    return redirect("workers")

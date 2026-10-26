"""Worker enrollment and health. Not an execution-ownership authority."""

import hashlib
import re
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import ExecutionWorker, WorkerEnrollment

HEARTBEAT_SECONDS = 15
STALE_SECONDS = 60


class WorkerAuthError(ValueError):
    pass


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise WorkerAuthError("Invalid worker credential")
    return hashlib.sha256(value.encode()).hexdigest()


def create_enrollment():
    code = secrets.token_urlsafe(32)
    WorkerEnrollment.objects.create(
        digest=_digest(code), expires_at=timezone.now() + timedelta(minutes=10)
    )
    return code


def enrollment_available(code):
    try:
        digest = _digest(code)
    except WorkerAuthError:
        return False
    return WorkerEnrollment.objects.filter(
        digest=digest, consumed_at__isnull=True, expires_at__gt=timezone.now()
    ).exists()


@transaction.atomic
def enroll(code, name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("Worker name must contain 1–80 characters")
    enrollment = (
        WorkerEnrollment.objects.select_for_update()
        .filter(digest=_digest(code), consumed_at__isnull=True, expires_at__gt=timezone.now())
        .first()
    )
    if enrollment is None:
        raise WorkerAuthError("Invalid or expired pairing code")
    token = secrets.token_urlsafe(32)
    worker = ExecutionWorker.objects.create(name=name.strip(), credential_digest=_digest(token))
    enrollment.consumed_at = timezone.now()
    enrollment.save(update_fields=["consumed_at"])
    return worker, token


def authenticate(token):
    worker = ExecutionWorker.objects.filter(
        credential_digest=_digest(token), revoked_at__isnull=True
    ).first()
    if worker is None:
        raise WorkerAuthError("Invalid worker credential")
    return worker


def revoke(worker_id):
    # Assignment fencing must also consult this state once dispatch is enabled.
    ExecutionWorker.objects.filter(pk=worker_id).update(
        revoked_at=timezone.now(), draining=True, authenticated=False
    )


def heartbeat(worker, report):
    required = {"version", "incarnation", "slots", "active", "capabilities", "authenticated"}
    if not isinstance(report, dict) or set(report) - {"metrics"} != required:
        raise ValueError("Invalid heartbeat")
    if type(report["version"]) is not int or not 1 <= report["version"] <= 1000:
        raise ValueError("Invalid protocol version")
    if not isinstance(report["incarnation"], str) or not re.fullmatch(
        r"[a-f0-9]{32}", report["incarnation"]
    ):
        raise ValueError("Invalid incarnation")
    if any(type(report[k]) is not int for k in ("slots", "active")) or not (
        1 <= report["slots"] <= 32 and 0 <= report["active"] <= report["slots"]
    ):
        raise ValueError("Invalid capacity")
    capabilities = report["capabilities"]
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > 3
        or any(not isinstance(c, str) or c not in {"model", "test", "graph"} for c in capabilities)
    ):
        raise ValueError("Invalid capabilities")
    if type(report["authenticated"]) is not bool:
        raise ValueError("Invalid authentication state")
    metrics = report.get("metrics", {})
    if not isinstance(metrics, dict) or set(metrics) - {"memory_used", "memory_total", "disk_free"}:
        raise ValueError("Invalid metrics")
    if any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in metrics.values()):
        raise ValueError("Invalid metrics")
    updated = ExecutionWorker.objects.filter(pk=worker.pk, revoked_at__isnull=True).update(
        heartbeat_at=timezone.now(),
        incarnation=report["incarnation"],
        protocol_version=report["version"],
        slots=report["slots"],
        active=report["active"],
        capabilities=sorted(set(capabilities)),
        authenticated=report["authenticated"],
        metrics=metrics,
    )
    if not updated:
        raise WorkerAuthError("Invalid worker credential")


def availability(worker):
    if worker.revoked_at:
        return "revoked"
    if worker.heartbeat_at is None:
        return "connecting"
    if worker.heartbeat_at <= timezone.now() - timedelta(seconds=STALE_SECONDS):
        return "offline"
    if worker.draining:
        return "draining"
    return "busy" if worker.active else "ready"


def ready(worker):
    return (
        availability(worker) in {"ready", "busy"}
        and worker.protocol_version == 1
        and worker.authenticated
        and bool(worker.capabilities)
        and worker.active < worker.slots
    )

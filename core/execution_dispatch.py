"""Coordinator-owned remote execution; expiry never proves container cleanup."""

import base64
import hashlib
import json
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from .claims import execution_job, owned, pool
from .models import (
    ExecutionAssignment,
    ExecutionOutput,
    ExecutionPlacement,
    ExecutionWorker,
    Job,
    JobClaim,
    PullRequest,
)
from .secrets import decrypt, encrypt

LIVE = ("queued", "running", "cancelling", "quarantined")
MAX_INPUT = 300 * 1024 * 1024
MAX_OUTPUT = 32 * 1024 * 1024
LEASE_SECONDS = 60


class AssignmentConflict(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def slot_blocked(scope, index):
    return ExecutionAssignment.objects.filter(
        scope=scope, slot_index=index, cleanup_confirmed=False
    ).exists()


def pending_for_job(job_id):
    return ExecutionAssignment.objects.filter(job_id=job_id, cleanup_confirmed=False).exists()


def _current(row):
    if row.job_id is None:
        return True
    return JobClaim.objects.filter(
        job_id=row.job_id,
        token=row.claim_token,
        expires_at__gt=timezone.now(),
        owner__retired=False,
        job__status="running",
        job__generation=row.generation,
        job__pr__generation=row.generation,
    ).exists()


def _eligible(worker, operation, payload, *, existing=False):
    return (
        (worker.enabled or existing)
        and not worker.revoked_at
        and (not worker.draining or existing)
        and worker.protocol_version == 1
        and worker.authenticated
        and worker.heartbeat_at is not None
        and worker.heartbeat_at > timezone.now() - timedelta(seconds=60)
        and operation in worker.allowed_capabilities
        and operation in worker.capabilities
        and (operation != "model" or payload.get("kind") in worker.account_kinds)
        and worker.metrics.get("disk_free", 0) >= 2 * 1024**3
    )


@transaction.atomic
def submit(job, operation, payload, slot):
    if (
        not settings.REMOTE_EXECUTION_ENABLED
        or job is None
        or not getattr(job, "claim_token", None)
    ):
        return None
    config = pool()
    if not owned(job):
        return None
    placement = ExecutionPlacement.objects.filter(job_id=job.pk).select_related("worker").first()
    if placement:
        candidates = [placement.worker]
    else:
        if config.draining:
            return None
        # Prefer session locality, then least occupied capacity. All reservations share pool mutex.
        previous = (
            ExecutionAssignment.objects.filter(
                job__pr_id=job.pr_id, operation=operation, cleanup_confirmed=True
            )
            .order_by("-created_at")
            .values_list("worker_id", flat=True)
            .first()
        )
        candidates = list(
            ExecutionWorker.objects.filter(enabled=True, is_local=False, revoked_at__isnull=True)
        )
        candidates.sort(
            key=lambda w: (
                w.pk != previous,
                ExecutionPlacement.objects.filter(worker=w).count(),
                str(w.pk),
            )
        )
    chosen = None
    for worker in candidates:
        if not _eligible(worker, operation, payload, existing=placement is not None):
            continue
        # A PR placement is not an extra execution slot. Do not queue team phases
        # behind an occupied agent while their model active-time budget runs.
        if ExecutionAssignment.objects.filter(
            worker=worker, cleanup_confirmed=False
        ).count() >= min(worker.max_slots, worker.slots):
            continue
        if not placement and ExecutionPlacement.objects.filter(worker=worker).count() >= min(
            worker.max_slots, worker.slots
        ):
            continue
        chosen = worker
        break
    if chosen is None:
        return None
    body = encoded(payload)
    if len(body.encode()) > MAX_INPUT:
        raise ValueError("Execution input too large")
    if not placement:
        ExecutionPlacement.objects.create(job=job, worker=chosen)
    return ExecutionAssignment.objects.create(
        worker=chosen,
        job=job,
        claim_token=str(job.claim_token),
        generation=job.generation,
        incarnation=chosen.incarnation,
        operation=operation,
        scope=slot.scope,
        slot_index=slot.index,
        input_hash=digest(body),
        encrypted_input=encrypt(body),
        expires_at=timezone.now() + timedelta(minutes=35),
    )


def _locked(worker, data):
    try:
        identifier = uuid.UUID(data["id"])
        token = uuid.UUID(data["token"])
        incarnation = data["incarnation"]
    except (KeyError, ValueError, TypeError, AttributeError):
        raise AssignmentConflict("Invalid assignment") from None
    row = ExecutionAssignment.objects.filter(
        pk=identifier, worker_id=worker.pk, token=token, incarnation=incarnation
    ).first()
    if row is None:
        raise AssignmentConflict("Unknown assignment")
    if row.job_id:
        PullRequest.objects.select_for_update().get(pk=row.job.pr_id)
        Job.objects.select_for_update().get(pk=row.job_id)
    return ExecutionAssignment.objects.select_for_update().get(pk=row.pk)


def _live(row):
    return (
        row.state == "running"
        and row.expires_at > timezone.now()
        and ExecutionWorker.objects.filter(
            pk=row.worker_id, incarnation=row.incarnation, revoked_at__isnull=True
        ).exists()
        and _current(row)
    )


@transaction.atomic
def take(worker, incarnation):
    pool()
    worker = ExecutionWorker.objects.select_for_update().get(pk=worker.pk)
    if worker.incarnation != incarnation or worker.revoked_at:
        raise AssignmentConflict("Worker incarnation changed")
    # Redelivery of the same assignment is safe only within the same durable agent incarnation.
    row = (
        ExecutionAssignment.objects.filter(worker=worker, incarnation=incarnation, state="running")
        .order_by("created_at")
        .first()
    )
    if row is None:
        row = (
            ExecutionAssignment.objects.filter(
                worker=worker, incarnation=incarnation, state="queued"
            )
            .order_by("created_at")
            .first()
        )
        if row is None:
            return None
        if not _current(row):
            row.state = "failed"
            row.cleanup_confirmed = True
            row.save()
            return None
        row.state = "running"
        row.expires_at = timezone.now() + timedelta(seconds=LEASE_SECONDS)
        row.save()
    if not _live(row):
        return None
    return {
        "id": str(row.pk),
        "token": str(row.token),
        "operation": row.operation,
        "input_hash": row.input_hash,
        "input_bytes": len(decrypt(row.encrypted_input).encode()),
    }


@transaction.atomic
def input_payload(worker, data):
    pool()
    row = _locked(worker, data)
    if not _live(row):
        raise AssignmentConflict("Assignment is stale")
    return {"payload": json.loads(decrypt(row.encrypted_input))}


@transaction.atomic
def renew(worker, data):
    pool()
    row = _locked(worker, data)
    if not _live(row) or worker.revoked_at:
        return {"continue": False}
    row.expires_at = timezone.now() + timedelta(seconds=LEASE_SECONDS)
    row.save(update_fields=["expires_at", "updated_at"])
    return {"continue": True}


@transaction.atomic
def append_output(worker, data):
    pool()
    row = _locked(worker, data)
    if not _live(row):
        raise AssignmentConflict("Assignment is stale")
    sequence = data.get("sequence")
    if type(sequence) is not int or sequence < 0:
        raise ValueError("Invalid sequence")
    try:
        raw = base64.b64decode(data["data"], validate=True)
    except (KeyError, ValueError, TypeError):
        raise ValueError("Invalid output") from None
    if len(raw) > 24576:
        raise ValueError("Output chunk too large")
    hashed = hashlib.sha256(raw).hexdigest()
    previous = ExecutionOutput.objects.filter(assignment=row, sequence=sequence).first()
    if previous:
        if previous.digest != hashed:
            raise AssignmentConflict("Conflicting output retry")
        return {"sequence": sequence}
    if sequence != row.next_sequence:
        raise AssignmentConflict("Output out of order")
    if row.output_bytes + len(raw) > MAX_OUTPUT:
        raise ValueError("Output limit exceeded")
    ExecutionOutput.objects.create(
        assignment=row, sequence=sequence, digest=hashed, encrypted_data=encrypt(data["data"])
    )
    row.output_bytes += len(raw)
    row.next_sequence += 1
    row.save(update_fields=["output_bytes", "next_sequence", "updated_at"])
    return {"sequence": sequence}


@transaction.atomic
def complete(worker, data):
    pool()
    row = _locked(worker, data)
    result = data.get("result")
    if (
        data.get("cleaned") is not True
        or not isinstance(result, dict)
        or set(result) - {"exit_code", "oom", "error"}
    ):
        raise ValueError("Cleanup acknowledgement required")
    if (
        type(result.get("exit_code")) is not int
        or not -255 <= result["exit_code"] <= 255
        or type(result.get("oom")) is not bool
    ):
        raise ValueError("Invalid result")
    if "error" in result and (not isinstance(result["error"], str) or len(result["error"]) > 160):
        raise ValueError("Invalid error")
    hashed = digest(encoded(result))
    if row.cleanup_confirmed:
        if row.result_digest != hashed:
            raise AssignmentConflict("Conflicting completion")
        return {"accepted": row.state == "completed"}
    accepted = _live(row)
    row.result = result
    row.result_digest = hashed
    row.cleanup_confirmed = True
    row.state = "completed" if accepted else "failed"
    row.encrypted_input = ""
    row.save()
    return {"accepted": accepted}


@transaction.atomic
def reconcile():
    pool()
    now = timezone.now()
    for row in ExecutionAssignment.objects.filter(
        cleanup_confirmed=False, state__in=LIVE
    ).select_related("job"):
        if (
            row.expires_at > now
            and _current(row)
            and ExecutionWorker.objects.filter(
                pk=row.worker_id, incarnation=row.incarnation, revoked_at__isnull=True
            ).exists()
        ):
            continue
        if row.state == "queued":
            row.state = "failed"
            row.cleanup_confirmed = True
            row.encrypted_input = ""
        else:
            row.state = "quarantined"
        row.save()


@transaction.atomic
def cancel(assignment_id):
    pool()
    row = ExecutionAssignment.objects.select_for_update().get(pk=assignment_id)
    if row.cleanup_confirmed:
        return
    if row.state == "queued":
        row.state = "failed"
        row.cleanup_confirmed = True
        row.encrypted_input = ""
    else:
        row.state = "cancelling"
    row.save()


def try_run(operation, payload, slot, cancelled, on_chunk):
    """Called while coordinator holds existing account/resource permit."""
    if not settings.REMOTE_EXECUTION_ENABLED:
        return None
    job = execution_job.get()
    if job is None:
        return None
    from .capacity import CapacityError
    from .providers import ReviewCancelled

    row = submit(job, operation, payload, slot)
    if row is None:
        return None
    cursor = 0
    cancellation_started = None
    try:
        while True:
            close_old_connections()
            try:
                stop = cancelled()
            except Exception:
                cancel(row.pk)
                raise
            if stop and cancellation_started is None:
                cancel(row.pk)
                cancellation_started = time.monotonic()
            for chunk in ExecutionOutput.objects.filter(
                assignment=row, sequence__gte=cursor
            ).order_by("sequence")[:128]:
                if cancellation_started is None:
                    on_chunk(base64.b64decode(decrypt(chunk.encrypted_data), validate=True))
                cursor = chunk.sequence + 1
            row.refresh_from_db()
            if row.cleanup_confirmed:
                # Drain all stored output before delivering terminal state.
                if cursor < row.next_sequence:
                    continue
                ExecutionOutput.objects.filter(assignment=row).delete()
                if cancellation_started is not None:
                    raise ReviewCancelled()
                if row.state != "completed":
                    raise CapacityError()
                return row.result
            if row.state == "quarantined" or (
                cancellation_started is not None and time.monotonic() - cancellation_started > 45
            ):
                raise CapacityError()
            if row.expires_at <= timezone.now():
                reconcile()
            time.sleep(0.25)
    except BaseException:
        cancel(row.pk)
        raise
    finally:
        close_old_connections()

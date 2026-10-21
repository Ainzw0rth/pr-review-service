"""Durable same-host execution ownership. Lease expiry is not proof of death."""

import fcntl
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.db.models.functions import Now
from django.utils import timezone

from .models import Job, JobClaim, ModelAttempt, PullRequest, ServiceSettings, WorkerOwner

LEASE_SECONDS = 90


def execution_lock(token):
    directory = settings.DATA_DIR / "claims"
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / (str(token) + ".lock"), "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


@transaction.atomic
def register_owner():
    owner = WorkerOwner.objects.create()
    owner.execution_lock = execution_lock(owner.pk)
    return owner


def pool():
    ServiceSettings.objects.get_or_create(pk=1)
    config = ServiceSettings.objects.select_for_update().get(pk=1)
    if (settings.DATA_DIR / "worker.drain").exists() and not config.draining:
        config.draining = True
        config.drain_generation += 1
        config.save(update_fields=["draining", "drain_generation"])
    return config


def claim(owner=None):
    owner = owner or register_owner()
    handle = None
    try:
        with transaction.atomic():
            config = pool()
            if not WorkerOwner.objects.filter(pk=owner.pk, retired=False).exists():
                return None
            if (
                config.draining
                or WorkerOwner.objects.filter(probe_active=True).exists()
                or JobClaim.objects.count() >= config.concurrent_pr_reviews
            ):
                return None
            candidates = (
                Job.objects.filter(
                    Q(status="queued")
                    & (Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=timezone.now()))
                    | Q(status="quota", next_retry_at__lte=timezone.now())
                )
                .exclude(pr_id__in=JobClaim.objects.values("pr_id"))
                .order_by("created_at")
            )
            # Admission mutex serializes claimers; skip both lock levels in PR→Job order.
            # Bound queue scanning so a busy head cannot hold global admission indefinitely.
            options = (
                {"skip_locked": True, "of": ("self",)}
                if connection.features.has_select_for_update_skip_locked
                else {}
            )
            for job_id, pr_id in list(candidates.values_list("pk", "pr_id")[:100]):
                pr = PullRequest.objects.select_for_update(**options).filter(pk=pr_id).first()
                if pr is None:
                    continue
                job = (
                    candidates.select_for_update(**options)
                    .filter(pk=job_id)
                    .select_related("pr__repository", "provider")
                    .first()
                )
                if job is None:
                    continue
                if job.generation != pr.generation or job.head_sha != pr.head_sha:
                    Job.objects.filter(pk=job.pk).update(status="superseded")
                    continue
                token = uuid.uuid4()
                handle = execution_lock(token)
                if handle is None:
                    return None
                JobClaim.objects.create(
                    job=job,
                    pr_id=job.pr_id,
                    owner=owner,
                    token=token,
                    expires_at=Now() + timedelta(seconds=LEASE_SECONDS),
                )
                job.status = "running"
                job.started_at = job.started_at or timezone.now()
                job.next_retry_at = None
                job.reason = ""
                job.save(update_fields=["status", "started_at", "next_retry_at", "reason"])
                job.claim_token = token
                job.claim_owner_id = owner.pk
                job.execution_lock = handle
                job.claim_owner = owner
                return job
    except BaseException:
        if handle:
            handle.close()
        raise


def owned(job):
    token = getattr(job, "claim_token", None)
    if token is None:
        # Unclaimed objects remain usable by legacy tests/admin authorities, never impersonate a claim.
        return not JobClaim.objects.filter(job_id=job.pk).exists()
    return JobClaim.objects.filter(
        job_id=job.pk,
        pr_id=job.pr_id,
        token=token,
        owner_id=job.claim_owner_id,
        owner__retired=False,
        expires_at__gt=Now(),
    ).exists()


@contextmanager
def mutation(job, *, terminal=False):
    from .publication import StaleReview

    with transaction.atomic():
        PullRequest.objects.select_for_update().get(pk=job.pr_id)
        row = Job.objects.select_for_update().get(pk=job.pk)
        if not owned(job) or (
            not terminal
            and (
                row.status != "running"
                or row.generation != row.pr.generation
                or row.head_sha != row.pr.head_sha
            )
        ):
            raise StaleReview("Execution ownership lost")
        yield row


def heartbeat(job):
    now = Now()
    if not getattr(job, "claim_token", None):
        return owned(job)
    return bool(
        JobClaim.objects.filter(
            job_id=job.pk,
            token=job.claim_token,
            owner_id=job.claim_owner_id,
            owner__retired=False,
            expires_at__gt=now,
        ).update(heartbeat_at=now, expires_at=now + timedelta(seconds=LEASE_SECONDS))
    )


def release(job):
    token = getattr(job, "claim_token", None)
    if token is None:
        return False
    with transaction.atomic():
        PullRequest.objects.select_for_update().get(pk=job.pr_id)
        row = Job.objects.select_for_update().get(pk=job.pk)
        retained = JobClaim.objects.filter(
            job_id=job.pk, pr_id=job.pr_id, token=token, owner_id=job.claim_owner_id
        )
        if retained.exists() and row.status == "running":
            # Teardown is a separate, narrowly token-checked authority: all execution
            # and finally cleanup already returned. An expired lease must neither
            # revive external-write authority nor strand a claimless running job.
            row.status = "attention"
            row.reason = (
                "Execution ended without confirmed finalization; inspect GitHub before retrying."
            )
            row.save(update_fields=["status", "reason"])
            ModelAttempt.objects.filter(job=row, status="running").update(
                status="interrupted", finished_at=timezone.now()
            )
        deleted, _ = retained.delete()
    handle = getattr(job, "execution_lock", None)
    if handle:
        handle.close()
    return bool(deleted)


def recover_dead_owners(cleanup=None):
    recovered = 0
    for candidate in JobClaim.objects.filter(expires_at__lte=Now()):
        handle = execution_lock(candidate.token)
        if handle is None:
            continue
        try:
            # Resource cleanup must succeed before capacity is released.
            if cleanup is None:
                continue
            cleanup(candidate)
            with transaction.atomic():
                PullRequest.objects.select_for_update().get(pk=candidate.pr_id)
                row = Job.objects.select_for_update().get(pk=candidate.job_id)
                retained = JobClaim.objects.filter(
                    pk=candidate.pk, token=candidate.token, expires_at__lte=Now()
                ).first()
                if retained is None:
                    continue
                if row.status == "running":
                    done = (
                        hasattr(row, "review_checkpoint") and row.review_checkpoint.stage == "done"
                    )
                    row.status = "completed" if done else "attention"
                    row.reason = "Worker interrupted; inspect GitHub and optional integrations before retrying."
                    row.save(update_fields=["status", "reason"])
                ModelAttempt.objects.filter(job=row, status="running").update(
                    status="interrupted", finished_at=timezone.now()
                )
                retained.delete()
                recovered += 1
        finally:
            handle.close()
    return recovered


def fenced(*, terminal=False, job_position=0, silent=False):
    """Fence a short persistence operation (or one publication unit) by its job."""
    from functools import wraps

    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            job = kwargs.get("job") or args[job_position]
            from .publication import StaleReview

            try:
                with mutation(job, terminal=terminal):
                    return function(*args, **kwargs)
            except StaleReview:
                if not silent:
                    raise

        return wrapped

    return decorate


def job_rows(job, *, terminal=False):
    rows = Job.objects.filter(pk=job.pk)
    if not terminal:
        rows = rows.filter(status="running")
    if getattr(job, "claim_token", None):
        rows = rows.filter(
            execution_claim__token=job.claim_token,
            execution_claim__owner_id=job.claim_owner_id,
            execution_claim__expires_at__gt=Now(),
            execution_claim__owner__retired=False,
        )
    elif JobClaim.objects.filter(job_id=job.pk).exists():
        return rows.none()
    return rows


def attempt_rows(job, **filters):
    return ModelAttempt.objects.filter(
        job_id__in=job_rows(job, terminal=True).values("pk"), **filters
    )


# Native child resources inherit this identity, never a freshly loaded Job token.
execution_job = ContextVar("review_execution_job", default=None)


def current_claim_token():
    job = execution_job.get()
    return str(job.claim_token) if job is not None and getattr(job, "claim_token", None) else ""


@fenced(terminal=True)
def receipt(*, job, **kwargs):
    from .models import Receipt

    return Receipt.objects.get_or_create(job=job, **kwargs)


@fenced()
def create_attempt(*, job, **kwargs):
    return ModelAttempt.objects.create(job=job, **kwargs)


@contextmanager
def admission(job, *, terminal=False):
    """Final local admission; an in-flight bounded HTTP call may finish after cancellation."""
    from .publication import StaleReview
    from .workflow import current

    if not owned(job) or (not terminal and not current(job)):
        raise StaleReview("Execution ownership lost")
    yield


def recover_owners(cleanup=None):
    """Retire only incarnations whose lifetime flock proves process quiescence."""
    from .models import ModelProbe

    for owner in WorkerOwner.objects.filter(
        retired=False, heartbeat_at__lte=timezone.now() - timedelta(seconds=LEASE_SECONDS)
    ):
        handle = execution_lock(owner.pk)
        if handle is None:
            continue
        try:
            if JobClaim.objects.filter(owner=owner).exists():
                continue
            if owner.probe_active:
                if cleanup is None:
                    continue
                cleanup(owner)
                ModelProbe.objects.filter(owner=owner, status="running").update(
                    status="failed",
                    reason="Worker interrupted during connection test; test again.",
                    finished_at=timezone.now(),
                )
            WorkerOwner.objects.filter(pk=owner.pk).update(retired=True, probe_active=False)
        finally:
            handle.close()


def current_owner_token():
    job = execution_job.get()
    return str(job.claim_owner_id) if job is not None else supervisor_owner.get()


supervisor_owner = ContextVar("review_supervisor_owner", default="")


def expect_label(*, job=None, **kwargs):
    from .models import ExpectedLabel

    if job is None:  # Event reconciliation/approval repair is an independent authority.
        return ExpectedLabel.objects.create(**kwargs)
    with mutation(job):
        return ExpectedLabel.objects.create(**kwargs)

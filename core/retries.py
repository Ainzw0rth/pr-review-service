"""Explicit dashboard retry requests; no label writes or bypass of commit/cycle guards."""

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Job, PullRequest, Receipt


class RetryRejected(ValueError):
    pass


RETRYABLE = ("attention", "failed", "quota", "cancelled")


def is_retry_owner(user):
    return user.is_active and user.is_superuser and user.pk == settings.DASHBOARD_RETRY_USER_ID


def retry_available(job):
    from .delivery import retry_available as delivery_available

    return delivery_available(job) or (
        job.status in RETRYABLE
        and job.generation == job.pr.generation
        and job.head_sha == job.pr.head_sha
        and job.pr.repository.enabled
        and not Receipt.objects.filter(job=job).exists()
    )


def retry_job(job, actor):
    from .delivery import queue_retry
    from .delivery import retry_available as delivery_available

    if delivery_available(job):
        return queue_retry(job)
    from .github import GitHub
    from .workflow import eligible, read_cycle

    existing = Job.objects.filter(retry_of=job).first()
    if existing:
        return existing
    if not retry_available(job):
        raise RetryRejected("This attempt cannot be retried. Use the latest unpublished attempt.")
    github = GitHub(job.pr.repository)
    try:
        snapshot, _, cycle = read_cycle(github, job.pr.repository, job.pr.number)
    finally:
        github.close()
    with transaction.atomic():
        pr = PullRequest.objects.select_for_update().get(pk=job.pr_id)
        job = (
            Job.objects.select_for_update(of=("self",))
            .select_related("pr__repository")
            .get(pk=job.pk)
        )
        existing = Job.objects.filter(retry_of=job).first()
        if existing:
            return existing
        if not retry_available(job):
            raise RetryRejected("This attempt changed while checking GitHub. No retry queued.")
        if (
            not eligible(job.pr.repository, snapshot)
            or not cycle["eligible"]
            or cycle["key"] != pr.canonical_trigger
            or snapshot["head"]["sha"] != job.head_sha
        ):
            raise RetryRejected(
                "PR must still request your review, carry on review, and match this commit and review cycle. Retoggle the GitHub label for a new commit."
            )
        job.status = "superseded"
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])
        pr.generation += 1
        pr.save(update_fields=["generation"])
        return Job.objects.create(
            pr=pr,
            generation=pr.generation,
            head_sha=pr.head_sha,
            actor=f"{actor} (dashboard retry)"[:100],
            retry_of=job,
        )

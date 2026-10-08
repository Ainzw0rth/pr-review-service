"""Track and retire only formal reviews created by this service; never merge."""

from django.conf import settings
from django.utils import timezone

from .models import ApprovalRecord, Job, Repository


def enabled_for(job):
    return (
        job.auto_approve
        and Repository.objects.filter(pk=job.pr.repository_id, auto_approve=True).exists()
    )


def remember(github, job, tag):
    record, _ = ApprovalRecord.objects.get_or_create(
        job=job,
        defaults={
            "repository": job.pr.repository,
            "number": job.pr.number,
            "head_sha": job.head_sha,
            "marker": tag,
            "actor": github.identity(),
        },
    )
    return record


def _clear_stale_label(github, repository, number):
    from .publication import StaleReview, transition

    fresh = github.pull(number)
    head = fresh["head"]["sha"]
    # A delayed old push must not erase a newer review or human workflow change.
    if Job.objects.filter(
        pr__repository=repository,
        pr__number=number,
        head_sha=head,
        status="completed",
        review_checkpoint__stage="done",
    ).exists():
        return
    if (
        ApprovalRecord.objects.filter(
            repository=repository, number=number, head_sha=head, dismissed_at__isnull=True
        )
        .exclude(review_id="")
        .exists()
    ):
        return
    labels = {item["name"] for item in fresh.get("labels", [])}
    if (
        fresh.get("state") != "open"
        or repository.approved_label not in labels
        or repository.review_label in labels
    ):
        return

    def unchanged():
        latest = github.pull(number)
        labels = {item["name"] for item in latest.get("labels", [])}
        if (
            latest["head"]["sha"] != head
            or repository.review_label in labels
            or repository.approved_label not in labels
        ):
            raise StaleReview("New review or human status superseded stale approval cleanup")

    transition(github, repository, number, repository.feedback_label, unchanged)


def dismiss_stale(github, repository, number):
    if not settings.LIVE_WRITES:
        return
    records = list(
        ApprovalRecord.objects.filter(
            repository=repository, number=number, dismissed_at__isnull=True
        )
    )
    if not records:
        return
    fresh = github.pull(number)
    stale = [row for row in records if row.revoke_requested or row.head_sha != fresh["head"]["sha"]]
    if stale:
        reviews = github.pages(f"/pulls/{number}/reviews")
        for row in stale:
            try:
                review = next(
                    (
                        review
                        for review in reviews
                        if row.marker in (review.get("body") or "")
                        and (review.get("user") or {}).get("login", "").casefold()
                        == row.actor.casefold()
                        and review.get("commit_id") == row.head_sha
                        and (not row.review_id or str(review.get("id")) == row.review_id)
                    ),
                    None,
                )
                if review and review.get("state") == "APPROVED":
                    # Re-read immediately before dismissing; do not dismiss a reverted-to commit.
                    if (
                        not row.revoke_requested
                        and github.pull(number)["head"]["sha"] == row.head_sha
                    ):
                        continue
                    response = github.put(
                        f"/pulls/{number}/reviews/{int(review['id'])}/dismissals",
                        {
                            "message": "The reviewed commit or approval eligibility changed. A new review is required."
                        },
                    )
                    if response.get("state") != "DISMISSED":
                        raise ValueError("Dismissal was not confirmed")
                    row.review_id = str(review["id"])
                    row.dismissed_at = timezone.now()
                elif review and review.get("state") == "DISMISSED":
                    row.dismissed_at = timezone.now()
                if row.dismissed_at and github.pull(number)["head"]["sha"] != row.head_sha:
                    _clear_stale_label(github, repository, number)
                row.error = ""
                row.save(update_fields=["dismissed_at", "review_id", "error"])
            except Exception:
                ApprovalRecord.objects.filter(pk=row.pk).update(
                    error="Stale approval dismissal failed; check GitHub review-dismissal permissions. Will retry."
                )
                raise
    ApprovalRecord.objects.filter(pk__in=[row.pk for row in records]).update(
        checked_at=timezone.now()
    )


def sweep_stale():
    """Repair missed webhook/racing publication cleanup; independent of toggle state."""
    if not settings.LIVE_WRITES:
        return
    from .github import GitHub

    seen = set()
    records = (
        ApprovalRecord.objects.filter(dismissed_at__isnull=True)
        .select_related("repository")
        .order_by("checked_at", "id")[:30]
    )
    for row in records:
        key = (row.repository_id, row.number)
        if key in seen:
            continue
        seen.add(key)
        try:
            with GitHub(row.repository) as github:
                dismiss_stale(github, row.repository, row.number)
        except Exception:
            ApprovalRecord.objects.filter(pk=row.pk).update(
                checked_at=timezone.now(), error="Approval check unavailable; retry pending."
            )


def withdraw(github, job):
    """Persist compensation before trying it, so permission/network failures are retried."""
    if ApprovalRecord.objects.filter(job=job, dismissed_at__isnull=True).update(
        revoke_requested=True
    ):
        try:
            dismiss_stale(github, job.pr.repository, job.pr.number)
        except Exception:
            pass  # Durable intent is retried by maintenance; preserve original publication error.

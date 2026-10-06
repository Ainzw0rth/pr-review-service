from django.conf import settings
from django.utils import timezone

from core.models import ExpectedLabel, Job, Webhook
from core.publication import StaleReview, transition
from core.workflow import canonical_cycle, reconcile


def process_event(event):
    from core.github import GitHub

    if event.processed_at:
        return
    github = GitHub(event.repository)
    try:
        return _process_event(event, github)
    finally:
        github.close()


def _process_event(event, github):
    from core.sheets import sync_status

    repo = event.repository
    payload = event.payload
    number = payload.get("number")
    if not number:
        event.processed_at = timezone.now()
        event.payload = {}
        event.save()
        return
    timeline = github.pages(f"/issues/{number}/timeline")
    snapshot = github.pull(number)
    canonical = canonical_cycle(repo, timeline, snapshot)
    action = payload.get("action")
    reconcile(event, snapshot, canonical=canonical)
    try:
        labels = {label["name"] for label in snapshot.get("labels", [])}
        if action == "synchronize" and repo.approved_label in labels:

            def unchanged_commit():
                fresh = github.pull(number)
                present = {label["name"] for label in fresh.get("labels", [])}
                if (
                    fresh["head"]["sha"] != snapshot["head"]["sha"]
                    or repo.review_label in present
                    or repo.approved_label not in present
                ):
                    raise StaleReview(
                        "New review or human status superseded stale approval cleanup"
                    )

            try:
                unchanged_commit()
                transition(github, repo, number, repo.feedback_label, unchanged_commit)
            except StaleReview:
                pass
        if (
            settings.LIVE_WRITES
            and repo.sheet_config
            and action in ("labeled", "review_requested")
            and canonical["eligible"]
        ):
            job = (
                Job.objects.filter(
                    pr__repository=repo,
                    pr__number=number,
                    pr__canonical_trigger=canonical["key"],
                    status__in=["queued", "running", "quota"],
                )
                .order_by("-created_at")
                .first()
            )
            if job:

                def still_requested():
                    latest = github.pull(number)
                    if (
                        latest["head"]["sha"] != job.head_sha
                        or not canonical_cycle(
                            repo, github.pages(f"/issues/{number}/timeline"), latest
                        )["eligible"]
                    ):
                        raise StaleReview("Task review status is stale")

                sync_status(github, repo, number, "review", before_write=still_requested)
        if action in ("closed", "reopened"):
            sync_status(github, repo, number, "closed" if action == "closed" else "review")
    except Exception:
        # Reconciliation itself is idempotent by canonical cycle. Retain only the
        # event fields needed for retry, not PR descriptions or upstream errors.
        retry_payload = {
            key: payload[key]
            for key in ("action", "number", "label", "sender", "requested_reviewer")
            if key in payload
        }
        retry_payload["pull_request"] = {"head": payload.get("pull_request", {}).get("head", {})}
        Webhook.objects.filter(pk=event.pk).update(
            processed_at=None,
            payload=retry_payload,
            error="Lifecycle synchronization unavailable; retry pending.",
        )
        raise
    ExpectedLabel.objects.filter(expires_at__lt=timezone.now()).delete()

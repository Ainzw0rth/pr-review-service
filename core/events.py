from django.conf import settings
from django.utils import timezone

from core.models import ExpectedLabel, Job, Webhook
from core.publication import StaleReview, transition
from core.workflow import read_cycle, reconcile


def process_event(event):
    from core.github import GitHub

    if event.processed_at:
        return
    github = GitHub(event.repository)
    try:
        return _process_event(event, github)
    finally:
        github.close()


def _process_event(event, github, state=None):
    from core.sheets import sync_status

    repo = event.repository
    payload = event.payload
    number = payload.get("number")
    if not number:
        event.processed_at = timezone.now()
        event.payload = {}
        event.save()
        return
    snapshot, canonical = state or _read_state(github, repo, number)
    action = payload.get("action")
    reconcile(event, snapshot, canonical=canonical)
    try:
        if action == "synchronize":
            from core.approvals import dismiss_stale

            dismiss_stale(github, repo, number)
        labels = {label["name"] for label in snapshot.get("labels", [])}
        reviewed_current_head = Job.objects.filter(
            pr__repository=repo,
            pr__number=number,
            head_sha=snapshot["head"]["sha"],
            status="completed",
            review_checkpoint__stage="done",
        ).exists()
        if action == "synchronize" and repo.approved_label in labels and not reviewed_current_head:

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
                    latest, _, cycle = read_cycle(github, repo, number)
                    if latest["head"]["sha"] != job.head_sha or not cycle["eligible"]:
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


def _read_state(github, repo, number):
    snapshot, _, canonical = read_cycle(github, repo, number)
    return snapshot, canonical


def process_pending():
    """Bounded sweep; retain every event and share only expensive state reads."""
    from collections import defaultdict
    from datetime import timedelta

    from django.db.models import Exists, OuterRef, Q

    from core.github import GitHub, GitHubRateLimited, GitHubUnavailable
    from core.workflow import TimelineMismatch

    groups = defaultdict(list)
    blocked_pr = Webhook.objects.filter(
        repository_id=OuterRef("repository_id"),
        payload__number=OuterRef("payload__number"),
        processed_at__isnull=True,
        retry_at__gt=timezone.now(),
    )
    pending = list(
        Webhook.objects.filter(processed_at__isnull=True)
        .filter(~Exists(blocked_pr))
        .filter(Q(retry_at__isnull=True) | Q(retry_at__lte=timezone.now()))
        .select_related("repository")
        .order_by("created_at", "pk")[:100]
    )
    for event in pending:
        groups[(event.repository_id, event.payload.get("number"))].append(event)
    for (_, number), events in groups.items():
        github = None
        try:
            github = GitHub(events[0].repository)
            state = _read_state(github, events[0].repository, number) if number else None
            for event in events:
                _process_event(event, github, state=state)
        except Exception as exc:
            remaining = Webhook.objects.filter(
                pk__in=[event.pk for event in events], processed_at__isnull=True
            )
            count = max(event.retry_count for event in events) + 1
            retry_at = timezone.now() + timedelta(seconds=min(30 * 2 ** min(count - 1, 6), 1800))
            if isinstance(exc, GitHubRateLimited):
                reason, retry_at = str(exc), max(retry_at, exc.retry_at)
            elif isinstance(exc, GitHubUnavailable):
                reason = str(exc)
            elif isinstance(exc, TimelineMismatch):
                reason = "GitHub history disagrees with current PR state; reconciliation paused, retry scheduled."
            else:
                reason = "Event reconciliation unavailable; retry scheduled."
            remaining.update(error=reason[:200], retry_at=retry_at, retry_count=count)
        finally:
            if github is not None:
                github.close()

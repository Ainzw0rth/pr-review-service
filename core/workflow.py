"""Deterministic lifecycle. Models never decide eligibility or status transitions."""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import ExpectedLabel, Job, PullRequest, Webhook

ACTIVE = ["queued", "running", "quota", "attention"]


def eligible(repo, snapshot):
    return (
        repo.enabled
        and snapshot.get("state") == "open"
        and repo.reviewer.casefold()
        in {x["login"].casefold() for x in snapshot.get("requested_reviewers", [])}
        and repo.review_label in {x["name"] for x in snapshot.get("labels", [])}
    )


def canonical_cycle(repo, timeline, snapshot):
    """Replay trusted GitHub issue events; delivery order is never event order."""
    workflow_labels = {repo.review_label, repo.feedback_label, repo.approved_label}
    labels = {}
    requested = False
    opened = True
    key = ""
    actor = ""
    human_entry = False
    relevant = []
    for item in timeline:
        if not isinstance(item, dict):
            raise ValueError("Malformed timeline")
        action = item.get("event")
        if action in ("labeled", "unlabeled"):
            if item.get("label", {}).get("name") not in workflow_labels:
                continue
        elif action in ("review_requested", "review_request_removed"):
            if (item.get("requested_reviewer") or {}).get(
                "login", ""
            ).casefold() != repo.reviewer.casefold():
                continue
        elif action not in ("closed", "reopened", "merged"):
            continue
        if type(item.get("id")) is not int or item["id"] < 1:
            raise ValueError("Timeline event ID is missing")
        relevant.append(item)
    # All relevant entries use GitHub's issue-event identity, not comment/review IDs.
    relevant.sort(key=lambda item: item["id"])
    for item in relevant:
        action = item["event"]
        was_eligible = (
            opened and requested and max(labels, key=labels.get, default=None) == repo.review_label
        )
        if action == "labeled":
            labels[item["label"]["name"]] = item["id"]
        elif action == "unlabeled":
            labels.pop(item["label"]["name"], None)
        elif action == "review_requested":
            requested = True
        elif action == "review_request_removed":
            requested = False
        elif action in ("closed", "merged"):
            opened = False
        elif action == "reopened":
            opened = True
        now_eligible = (
            opened and requested and max(labels, key=labels.get, default=None) == repo.review_label
        )
        human = (item.get("actor") or {}).get("type") == "User"
        trigger = action == "review_requested" or (
            action == "labeled" and item["label"]["name"] == repo.review_label
        )
        if not was_eligible and now_eligible:
            human_entry = human and trigger
            if human_entry:
                key = str(item["id"])
                actor = item["actor"]["login"]
    present = {label["name"] for label in snapshot.get("labels", [])} & workflow_labels
    fresh_requested = repo.reviewer.casefold() in {
        reviewer["login"].casefold() for reviewer in snapshot.get("requested_reviewers", [])
    }
    if (
        present != set(labels)
        or (fresh_requested and not requested)
        or opened != (snapshot["state"] == "open")
    ):
        raise ValueError("GitHub timeline and current PR state disagree; retry reconciliation")
    return {
        "key": key,
        "actor": actor,
        "eligible": bool(key)
        and human_entry
        and eligible(repo, snapshot)
        and max(labels, key=labels.get, default=None) == repo.review_label,
    }


@transaction.atomic
def reconcile(event, snapshot, *, canonical):
    event = Webhook.objects.select_for_update().select_related("repository").get(pk=event.pk)
    if event.processed_at:
        return
    if (
        not isinstance(canonical, dict)
        or not isinstance(canonical.get("key"), str)
        or (canonical["key"] and not canonical["key"].isdigit())
        or type(canonical.get("eligible")) is not bool
    ):
        raise ValueError("Trusted canonical timeline state required")
    repo = event.repository
    payload = event.payload
    number = payload.get("number") or payload.get("pull_request", {}).get("number")
    if not number or snapshot.get("number") != number:
        raise ValueError("Mismatched PR snapshot")
    pr, _ = PullRequest.objects.get_or_create(
        repository=repo, number=number, defaults={"head_sha": snapshot["head"]["sha"]}
    )
    old_sha = pr.head_sha
    pr.head_sha = snapshot["head"]["sha"]
    pr.is_open = snapshot["state"] == "open"
    pr.requested = repo.reviewer.casefold() in {
        r["login"].casefold() for r in snapshot.get("requested_reviewers", [])
    }
    pr.labels = [r["name"] for r in snapshot.get("labels", [])]
    action = payload.get("action")
    actor = payload.get("sender", {}).get("login", "")
    label = payload.get("label", {}).get("name", "")
    expected = ExpectedLabel.objects.filter(
        repository=repo,
        number=number,
        label=label,
        action=action,
        actor=actor,
        expires_at__gt=timezone.now(),
    ).first()
    own = expected is not None
    if expected:
        expected.delete()
    active = pr.jobs.filter(status__in=ACTIVE)
    if old_sha != pr.head_sha:
        active.update(
            status="superseded",
            reason="New commit; toggle feedback then on review to request another review.",
            finished_at=timezone.now(),
        )
    elif not own and (not eligible(repo, snapshot) or not canonical["eligible"]):
        active.update(
            status="cancelled",
            reason="Review eligibility removed or human status override.",
            finished_at=timezone.now(),
        )
    human = payload.get("sender", {}).get("type") == "User"
    trigger = (action == "labeled" and label == repo.review_label) or (
        action == "review_requested"
        and payload.get("requested_reviewer", {}).get("login", "").casefold()
        == repo.reviewer.casefold()
    )
    event_sha = payload.get("pull_request", {}).get("head", {}).get("sha")
    key = canonical["key"]
    new_cycle = bool(key) and int(key) > int(pr.canonical_trigger or "0")
    if new_cycle:
        # Pushes consume prior requests; an unrelated delayed delivery must not
        # swallow a new human entry whose own webhook has not arrived yet.
        if trigger or action == "synchronize" or not canonical["eligible"]:
            pr.canonical_trigger = key
        active.update(
            status="cancelled",
            reason="A newer human review cycle was observed.",
            finished_at=timezone.now(),
        )
        if (
            trigger
            and human
            and not own
            and eligible(repo, snapshot)
            and canonical["eligible"]
            and event_sha == pr.head_sha
        ):
            active.update(
                status="cancelled",
                reason="Replaced by a new human review request.",
                finished_at=timezone.now(),
            )
            pr.generation += 1
            Job.objects.create(
                pr=pr,
                generation=pr.generation,
                head_sha=pr.head_sha,
                actor=canonical.get("actor") or actor,
            )
    from .assignees import sync_assignees

    sync_assignees(pr, snapshot)
    pr.save()
    event.processed_at = timezone.now()
    event.payload = {}  # Do not retain PR text as dashboard/history data.
    event.error = ""
    event.save(update_fields=["processed_at", "payload", "error"])


@transaction.atomic
def claim():
    if Job.objects.filter(status="running").exists():
        return None
    job = (
        Job.objects.select_related("pr__repository", "provider")
        .filter(Q(status="queued") | Q(status="quota", next_retry_at__lte=timezone.now()))
        .order_by("created_at")
        .first()
    )
    if not job:
        return None
    job.status = "running"
    job.started_at = job.started_at or timezone.now()
    job.next_retry_at = None
    job.reason = ""
    job.save(update_fields=["status", "started_at", "next_retry_at", "reason"])
    return job


def finish(job, status, reason="", retry_at=None):
    # A worker may never overwrite a cancellation made by the admin/event process.
    return Job.objects.filter(pk=job.pk, status="running").update(
        status=status,
        reason=reason[:250],
        next_retry_at=retry_at,
        finished_at=None if status in ["quota", "attention"] else timezone.now(),
    )


def current(job):
    return Job.objects.filter(
        pk=job.pk,
        status="running",
        pr__generation=job.generation,
        pr__head_sha=job.head_sha,
        pr__is_open=True,
        pr__requested=True,
        pr__repository__enabled=True,
    ).exists()

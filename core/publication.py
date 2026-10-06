"""Narrow, revalidated GitHub writes. No model-chosen endpoints."""

import copy
import hashlib
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from core.models import ExpectedLabel, Job, Receipt
from core.workflow import canonical_cycle, current


class StaleReview(Exception):
    pass


def _guard_state(github, job):
    if not current(job):
        raise StaleReview("Attempt no longer active")
    timeline = github.pages(f"/issues/{job.pr.number}/timeline")
    pr = github.pull(job.pr.number)
    # A cancellation may arrive while either network read is in flight.
    if not current(job):
        raise StaleReview("Attempt cancelled during eligibility check")
    try:
        cycle = canonical_cycle(job.pr.repository, timeline, pr)
    except (ValueError, TypeError, KeyError):
        raise StaleReview("Cannot establish current review cycle") from None
    row = Job.objects.select_related("pr").get(pk=job.pk)
    if (
        pr["head"]["sha"] != job.head_sha
        or not cycle["eligible"]
        or cycle["key"] != row.pr.canonical_trigger
    ):
        raise StaleReview("PR no longer in this human review cycle")
    return pr, timeline


def guard(github, job):
    return _guard_state(github, job)[0]


def transition(
    github, repo, number, target, check=lambda: None, after_write=lambda action, label: None
):
    """Only touch our three labels, preserving unrelated human labels."""
    if not settings.LIVE_WRITES:
        return
    actor = github.identity()
    pr = github.pull(number)
    present = {x["name"] for x in pr.get("labels", [])}
    labels = [target] + [
        x for x in [repo.review_label, repo.feedback_label, repo.approved_label] if x != target
    ]
    if target == repo.review_label:
        raise ValueError("Service cannot initiate a review")
    for label in labels:
        if label == target and label not in present:
            check()
            expectation = ExpectedLabel.objects.create(
                repository=repo,
                number=number,
                label=label,
                action="labeled",
                actor=actor,
                expires_at=timezone.now() + timedelta(minutes=5),
            )
            try:
                github.post(f"/issues/{number}/labels", {"labels": [label]})
                after_write("labeled", label)
            except Exception:
                expectation.delete()
                raise
        elif label != target and label in present:
            check()
            expectation = ExpectedLabel.objects.create(
                repository=repo,
                number=number,
                label=label,
                action="unlabeled",
                actor=actor,
                expires_at=timezone.now() + timedelta(minutes=5),
            )
            try:
                github.delete(f"/issues/{number}/labels/{quote(label, safe='')}")
                after_write("unlabeled", label)
            except Exception:
                expectation.delete()
                raise


def marker(job, key):
    return f"<!-- review-desk:{job.id}:{hashlib.sha256(key.encode()).hexdigest()[:24]} -->"


def once(github, job, key, body, endpoint, listing, extra=None):
    guard(github, job)
    if Receipt.objects.filter(job=job, key=key).exists():
        return
    tag = marker(job, key)
    existing = next(
        (
            x
            for x in github.pages(listing)
            if tag in (x.get("body") or "")
            and (x.get("user") or {}).get("login", "").casefold() == github.identity().casefold()
        ),
        None,
    )
    if existing:
        Receipt.objects.get_or_create(job=job, key=key, defaults={"remote_id": str(existing["id"])})
        return
    guard(github, job)
    posted = github.post(
        endpoint,
        {
            **(extra or {}),
            "body": f"**Automated review · Auto Reviewer**\n\n{body}\n\n{tag}",
        },
    )
    Receipt.objects.get_or_create(job=job, key=key, defaults={"remote_id": str(posted["id"])})


def publish(github, job, result, context, checks):
    if not settings.LIVE_WRITES:
        raise RuntimeError("Live publishing is disabled")
    number = job.pr.number
    # Allowed targets are established by trusted context retrieval, not the model.
    related = {p["number"] for p in context.get("related_prs", [])}
    reply_roots = {
        c["databaseId"]: thread["comments"][0]["databaseId"]
        for thread in context.get("threads", [])
        if thread.get("comments")
        for c in thread["comments"]
        if isinstance(c.get("databaseId"), int)
    }
    allowed_comments = set(reply_roots)
    for reply in result["replies"]:
        if reply["comment_id"] not in allowed_comments:
            raise ValueError("Unknown discussion target")
    for comment in result["related_comments"]:
        if comment["number"] not in related or comment["number"] == number:
            raise ValueError("Unknown related PR target")
    files = {f["filename"]: f for f in context.get("files", [])}
    for finding in result["findings"]:
        if finding["path"] not in files or finding["line"] not in files[finding["path"]].get(
            "valid_lines", []
        ):
            raise ValueError("Finding is not on a changed line")
    for i, reply in enumerate(result["replies"]):
        if reply["comment_id"] not in allowed_comments:
            raise ValueError("Unknown discussion target")
        once(
            github,
            job,
            f"reply:{reply['comment_id']}",
            reply["body"],
            f"/pulls/{number}/comments/{reply_roots[reply['comment_id']]}/replies",
            f"/pulls/{number}/comments",
        )
    for comment in result["related_comments"]:
        target = comment["number"]
        if target not in related or target == number:
            raise ValueError("Unknown related PR target")
        once(
            github,
            job,
            f"related:{target}",
            comment["body"],
            f"/issues/{target}/comments",
            f"/issues/{target}/comments",
        )
    # Findings are posted as one commit-pinned review, with summary as the review body.
    files = {f["filename"]: f for f in context.get("files", [])}
    inline = []
    for finding in result["findings"]:
        file = files.get(finding["path"])
        if not file or finding["line"] not in file.get("valid_lines", []):
            raise ValueError("Finding is not on a changed line")
        inline.append(
            {
                "path": finding["path"],
                "line": finding["line"],
                "side": "RIGHT",
                "body": f"**{finding['severity']}** — {finding['body']}",
            }
        )
    blocked = (
        result["blocking"]
        or not result["complete"]
        or not checks["passed"]
        or any(f["severity"] == "blocking" for f in result["findings"])
    )
    body = f"Commit: `{job.head_sha}`\n\n{result['summary']}\n\nChecks: {checks['summary']}\n\n"
    body += (
        f"Changes or clarification required. After addressing feedback, request @{job.pr.repository.reviewer} again if needed, then toggle `{job.pr.repository.feedback_label}` → `{job.pr.repository.review_label}`."
        if blocked
        else "No blocking findings. AI clearance is not a formal human approval; repository merge rules still apply."
    )
    # Use issue summary first: preserves reviewer request while thread replies publish.
    once(github, job, "summary", body, f"/issues/{number}/comments", f"/issues/{number}/comments")
    _, baseline_timeline = _guard_state(github, job)
    tag = marker(job, "review")
    existing = next(
        (
            x
            for x in github.pages(f"/pulls/{number}/reviews")
            if tag in (x.get("body") or "")
            and (x.get("user") or {}).get("login", "").casefold() == github.identity().casefold()
            and x.get("commit_id") == job.head_sha
            and x.get("state") == ("CHANGES_REQUESTED" if blocked else "COMMENTED")
        ),
        None,
    )
    if not existing and (inline or blocked):
        guard(github, job)
        if blocked:
            ExpectedLabel.objects.create(
                repository=job.pr.repository,
                number=number,
                label="",
                action="review_request_removed",
                actor=github.identity(),
                expires_at=timezone.now() + timedelta(minutes=5),
            )
        posted = github.post(
            f"/pulls/{number}/reviews",
            {
                "commit_id": job.head_sha,
                "event": "REQUEST_CHANGES" if blocked else "COMMENT",
                "body": f"Automated review. See summary above.\n\n{tag}",
                "comments": inline,
            },
        )
        Receipt.objects.get_or_create(
            job=job, key="review", defaults={"remote_id": str(posted["id"])}
        )
    elif existing:
        Receipt.objects.get_or_create(
            job=job, key="review", defaults={"remote_id": str(existing["id"])}
        )

    if blocked and Receipt.objects.filter(job=job, key="review").exists():
        Receipt.objects.get_or_create(job=job, key="request_changes")

    # Pin terminal label normalization to the checked timeline. Only this call's
    # exact label writes may be excluded from later eligibility checks.
    own_transitions = []
    baseline_ids = {(item.get("event"), item["id"]) for item in baseline_timeline if "id" in item}
    actor = github.identity().casefold()
    relevant_actions = {
        "labeled",
        "unlabeled",
        "review_requested",
        "review_request_removed",
        "closed",
        "reopened",
        "merged",
    }

    def terminal_guard():
        timeline = github.pages(f"/issues/{number}/timeline")
        fresh = copy.deepcopy(github.pull(number))
        row = Job.objects.select_related("pr__repository").get(pk=job.pk)
        if (
            row.status != "running"
            or row.generation != row.pr.generation
            or row.head_sha != row.pr.head_sha
            or fresh["head"]["sha"] != job.head_sha
            or fresh["state"] != "open"
            or not row.pr.repository.enabled
        ):
            raise StaleReview("Terminal state changed")
        allow_removed = blocked and Receipt.objects.filter(job=job, key="request_changes").exists()
        requested = {item["login"].casefold() for item in fresh.get("requested_reviewers", [])}
        if job.pr.repository.reviewer.casefold() not in requested and not allow_removed:
            raise StaleReview("Human removed requested reviewer")
        observed = []
        own_ids = set()
        for item in timeline:
            if (item.get("event"), item.get("id")) in baseline_ids or item.get(
                "event"
            ) not in relevant_actions:
                continue
            action = item["event"]
            if action in ("labeled", "unlabeled") and item.get("label", {}).get("name") not in {
                job.pr.repository.review_label,
                job.pr.repository.feedback_label,
                job.pr.repository.approved_label,
            }:
                continue
            if (
                action in ("review_requested", "review_request_removed")
                and (item.get("requested_reviewer") or {}).get("login", "").casefold()
                != job.pr.repository.reviewer.casefold()
            ):
                continue
            item_actor = (item.get("actor") or {}).get("login", "").casefold()
            if (
                action == "review_request_removed"
                and allow_removed
                and item_actor == actor
                and (item.get("requested_reviewer") or {}).get("login", "").casefold()
                == job.pr.repository.reviewer.casefold()
            ):
                own_ids.add(item["id"])
                continue
            if action in ("labeled", "unlabeled") and item_actor == actor:
                observed.append((action, item["label"]["name"]))
                own_ids.add(item["id"])
            else:
                raise StaleReview("Human changed review lifecycle during publication")
        if observed != own_transitions:
            raise StaleReview("Unexpected label change during publication")
        labels = {item["name"] for item in fresh.get("labels", [])}
        for action, label in reversed(own_transitions):
            if action == "labeled":
                labels.discard(label)
            else:
                labels.add(label)
        fresh["labels"] = [{"name": label} for label in labels]
        if allow_removed and job.pr.repository.reviewer.casefold() not in requested:
            fresh["requested_reviewers"] = [
                *fresh.get("requested_reviewers", []),
                {"login": job.pr.repository.reviewer},
            ]
        try:
            cycle = canonical_cycle(
                job.pr.repository,
                [item for item in timeline if item.get("id") not in own_ids],
                fresh,
            )
        except (ValueError, TypeError, KeyError):
            raise StaleReview("Cannot establish terminal review cycle") from None
        if not cycle["eligible"] or cycle["key"] != row.pr.canonical_trigger:
            raise StaleReview("Human review cycle changed")

    terminal_guard()
    if not blocked and job.pr.repository.required_checks:
        from core.github import check_required

        if not check_required(github, job.pr.repository, job.head_sha)["passed"]:
            raise ValueError("Required checks changed during review")
    transition(
        github,
        job.pr.repository,
        number,
        job.pr.repository.feedback_label if blocked else job.pr.repository.approved_label,
        terminal_guard,
        lambda action, label: own_transitions.append((action, label)),
    )
    terminal_guard()
    return "feedback" if blocked else "ready"

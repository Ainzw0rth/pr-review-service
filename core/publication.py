"""Narrow, revalidated GitHub writes. No model-chosen endpoints."""

import copy
import hashlib
from contextlib import nullcontext
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from core.delivery import checkpoint_state, safe_error, update_cursor
from core.github import GitHubRateLimited, IntegrationError
from core.models import Job, Receipt
from core.workflow import canonical_cycle, current
from core.workflow_schema import GROUNDED_REVISIONS

from .claims import admission, expect_label, mutation, owned
from .claims import receipt as persist_receipt


def _progress(job, category, status, count=None):
    """Never restart a category already confirmed by this attempt."""
    if job.workflow_revision not in GROUNDED_REVISIONS:
        return
    if job.activity.filter(code__in=[f"{category}_done", f"{category}_unused"]).exists():
        return
    if status == "started" and job.activity.filter(code=f"{category}_started").exists():
        return
    from core.activity import record

    record(job, f"{category}_{status}", count=count)


class LabelCleanupError(Exception):
    """Review delivered, but final workflow labels could not be normalized."""


class StaleReview(Exception):
    # Only application-owned messages may be shown on the dashboard.
    REASONS = frozenset(
        {
            "Attempt no longer active",
            "Attempt cancelled during eligibility check",
            "Cannot establish current review cycle",
            "PR no longer in this human review cycle",
            "Discussion target changed",
            "Discussion reopened after resolution",
            "Discussion changed since evidence collection",
            "Terminal state changed",
            "Human removed requested reviewer",
            "Human changed review lifecycle during publication",
            "Unexpected label change during publication",
            "Cannot establish terminal review cycle",
            "Human review cycle changed",
            "Saved review expired",
            "Saved review is no longer applicable",
            "Review requirements or discussion changed since analysis",
            "Published review is no longer available",
        }
    )

    @property
    def public_reason(self):
        message = str(self)
        return message if message in self.REASONS else "Review eligibility changed"


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
    github,
    repo,
    number,
    target,
    check=lambda: None,
    after_write=lambda action, label: None,
    *,
    job=None,
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
            with admission(job) if job is not None else nullcontext():
                check()
                expectation = expect_label(
                    job=job,
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
            with admission(job) if job is not None else nullcontext():
                check()
                expectation = expect_label(
                    job=job,
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
        persist_receipt(job=job, key=key, defaults={"remote_id": str(existing["id"])})
        return
    guard(github, job)
    with admission(job):
        posted = github.post(
            endpoint,
            {
                **(extra or {}),
                "body": f"**Automated review · Auto Reviewer**\n\n{body}\n\n{tag}",
            },
        )
        persist_receipt(job=job, key=key, defaults={"remote_id": str(posted["id"])})


RESOLVE_THREAD = """mutation($id:ID!) {
 resolveReviewThread(input:{threadId:$id}) { thread { id isResolved } }
}"""


def thread_replies(result, context):
    """Require exactly one explicit assessment per captured current-PR thread."""
    expected = context.get("threads", [])
    roots = {}
    for thread in expected:
        if not thread.get("id") or not thread.get("comments") or thread.get("isResolved"):
            raise ValueError("Invalid discussion context")
        for comment in thread["comments"]:
            roots[comment["databaseId"]] = thread
    assessed, replies = set(), []
    for reply in result["replies"]:
        thread = roots.get(reply["comment_id"])
        if not thread or thread["id"] in assessed or type(reply.get("resolve")) is not bool:
            raise ValueError("Unknown or duplicate discussion assessment")
        if "action" in reply:
            if (
                reply["action"] not in {"reply", "not_needed"}
                or not isinstance(reply.get("reason"), str)
                or not reply["reason"].strip()
                or type(reply.get("blocking")) is not bool
                or (reply["action"] == "not_needed" and reply["resolve"])
            ):
                raise ValueError("Invalid discussion action")
        if reply["resolve"] and (not result["complete"] or not context.get("complete")):
            raise ValueError("Incomplete evidence cannot resolve a discussion")
        assessed.add(thread["id"])
        replies.append((thread, reply))
    if len(assessed) != len(expected):
        raise ValueError("Every open discussion requires an assessment")
    return replies


def _reply_body(job, reply, can_resolve):
    body = reply["body"]
    if reply["resolve"]:
        body = f"Verified against `{job.head_sha}`. {body}"
        if not can_resolve:
            body += "\n\nThe service lacks permission to resolve this thread; please resolve this thread manually."
    return body


def _fresh_thread(github, job, original, reply):
    from core.context import review_thread

    guard(github, job)
    fresh = review_thread(github, original["id"])
    pr = fresh["pullRequest"]
    if (
        fresh["id"] != original["id"]
        or pr["number"] != job.pr.number
        or pr["repository"]["nameWithOwner"].casefold() != job.pr.repository.full_name.casefold()
        or pr["headRefOid"] != job.head_sha
    ):
        raise StaleReview("Discussion target changed")
    if fresh["isResolved"]:
        return fresh
    if Receipt.objects.filter(job=job, key=f"resolve:{original['id']}").exists():
        raise StaleReview("Discussion reopened after resolution")
    root = original["comments"][0]["databaseId"]
    receipt = Receipt.objects.filter(job=job, key=f"reply:{root}").first()

    def snapshot(comments):
        return [
            (c.get("databaseId"), c.get("body"), c.get("updatedAt"), c.get("author"))
            for c in comments
        ]

    comments = fresh["comments"]
    if not receipt and reply.get("action") != "not_needed":
        body = _reply_body(job, reply, fresh.get("viewerCanResolve"))
        expected = (
            f"**Automated review · Auto Reviewer**\n\n{body}\n\n{marker(job, f'reply:{root}')}"
        )
        own = next(
            (
                c
                for c in fresh["comments"]
                if c.get("body") == expected
                and (c.get("author") or {}).get("login", "").casefold()
                == github.identity().casefold()
            ),
            None,
        )
        if own:
            receipt, _ = persist_receipt(
                job=job, key=f"reply:{root}", defaults={"remote_id": str(own["databaseId"])}
            )
    if receipt:
        body = _reply_body(job, reply, fresh.get("viewerCanResolve"))
        own_body = (
            f"**Automated review · Auto Reviewer**\n\n{body}\n\n{marker(job, f'reply:{root}')}"
        )
        # Ignore only this attempt's own persisted reply, never another participant's activity.
        comments = [
            c
            for c in comments
            if not (
                str(c.get("databaseId")) == receipt.remote_id
                and c.get("body") == own_body
                and (c.get("author") or {}).get("login", "").casefold()
                == github.identity().casefold()
            )
        ]
    if snapshot(comments) != snapshot(original["comments"]):
        raise StaleReview("Discussion changed since evidence collection")
    guard(github, job)
    return fresh


def resolution_confirmed(github, job, original, reply, fresh):
    """Reconcile an ambiguous mutation only against this attempt's unchanged reply."""
    pr = fresh.get("pullRequest", {})
    if (
        not reply["resolve"]
        or not fresh.get("isResolved")
        or fresh.get("id") != original["id"]
        or pr.get("number") != job.pr.number
        or pr.get("headRefOid") != job.head_sha
        or pr.get("repository", {}).get("nameWithOwner", "").casefold()
        != job.pr.repository.full_name.casefold()
        or (fresh.get("resolvedBy") or {}).get("login", "").casefold()
        != github.identity().casefold()
    ):
        return False
    root = original["comments"][0]["databaseId"]
    receipt = Receipt.objects.filter(job=job, key=f"reply:{root}").first()
    if not receipt:
        return False
    body = f"**Automated review · Auto Reviewer**\n\n{_reply_body(job, reply, True)}\n\n{marker(job, f'reply:{root}')}"
    own = [
        c
        for c in fresh["comments"]
        if str(c.get("databaseId")) == receipt.remote_id
        and c.get("body") == body
        and (c.get("author") or {}).get("login", "").casefold() == github.identity().casefold()
    ]

    def values(comments):
        return [
            (c.get("databaseId"), c.get("body"), c.get("updatedAt"), c.get("author"))
            for c in comments
        ]

    return len(own) == 1 and values([c for c in fresh["comments"] if c not in own]) == values(
        original["comments"]
    )


def publish_threads(github, job, replies):
    # Preflight all discussions before publication, then check each again at write time.
    for thread, reply in replies:
        _fresh_thread(github, job, thread, reply)
    for thread, reply in replies:
        fresh = _fresh_thread(github, job, thread, reply)
        if fresh["isResolved"] or reply.get("action") == "not_needed":
            continue
        root = thread["comments"][0]["databaseId"]
        body = _reply_body(job, reply, fresh.get("viewerCanResolve"))
        last = fresh["comments"][-1]
        same_reply = (
            not reply["resolve"]
            and (last.get("author") or {}).get("login", "").casefold()
            == github.identity().casefold()
            and last.get("body", "").startswith(
                f"**Automated review · Auto Reviewer**\n\n{body}\n\n<!-- review-desk:"
            )
        )
        if not same_reply:
            once(
                github,
                job,
                f"reply:{root}",
                body,
                f"/pulls/{job.pr.number}/comments/{root}/replies",
                f"/pulls/{job.pr.number}/comments",
            )
        if not reply["resolve"]:
            continue
        fresh = _fresh_thread(github, job, thread, reply)
        if fresh["isResolved"] or not fresh.get("viewerCanResolve"):
            continue
        guard(github, job)
        with admission(job):
            try:
                data = github.graphql(RESOLVE_THREAD, {"id": thread["id"]})
                resolved = (data.get("resolveReviewThread") or {}).get("thread") or {}
                if resolved.get("id") != thread["id"] or resolved.get("isResolved") is not True:
                    raise IntegrationError("GitHub did not confirm discussion resolution.")
            except GitHubRateLimited:
                raise
            except IntegrationError:
                from .context import review_thread

                fresh = review_thread(github, thread["id"])
                if not resolution_confirmed(github, job, thread, reply, fresh):
                    raise
                guard(github, job)
            persist_receipt(
                job=job, key=f"resolve:{thread['id']}", defaults={"remote_id": thread["id"]}
            )


def publish(github, job, result, context, checks):
    if not settings.LIVE_WRITES:
        raise RuntimeError("Live publishing is disabled")
    number = job.pr.number
    stage, state = checkpoint_state(job)
    if stage in ("review", "labels"):
        inline = [
            {
                "path": f["path"],
                "line": f["line"],
                "side": "RIGHT",
                "body": f"**{f['severity']}** — {f['body']}",
            }
            for f in result["findings"]
        ]
        return _publish_review(github, job, inline, state["blocked"], state["baseline"])
    # Allowed targets are established by trusted context retrieval, not the model.
    related_prs = _related_targets(job, context.get("related_prs", []))
    related_issues = (
        _related_targets(job, context.get("issues", []))
        if job.workflow_revision in GROUNDED_REVISIONS
        else set()
    )
    related = related_prs | related_issues
    replies = thread_replies(result, context)
    for comment in result["related_comments"]:
        if comment["number"] not in related or comment["number"] == number:
            raise ValueError("Unknown related PR target")
    files = {f["filename"]: f for f in context.get("files", [])}
    for finding in result["findings"]:
        if finding["path"] not in files or finding["line"] not in files[finding["path"]].get(
            "valid_lines", []
        ):
            raise ValueError("Finding is not on a changed line")
    if any(reply.get("action", "reply") == "reply" for _, reply in replies):
        _progress(job, "discussion", "started")
    publish_threads(github, job, replies)
    count = Receipt.objects.filter(
        job=job,
        key__in=[f"reply:{thread['comments'][0]['databaseId']}" for thread, _ in replies],
    ).count()
    _progress(job, "discussion", "done" if count else "unused", count)
    for category, targets in (("related_pr", related_prs), ("related_issue", related_issues)):
        comments = [c for c in result["related_comments"] if c["number"] in targets]
        if comments:
            _progress(job, category, "started")
        for comment in comments:
            target = comment["number"]
            once(
                github,
                job,
                f"related:{target}",
                comment["body"],
                f"/issues/{target}/comments",
                f"/issues/{target}/comments",
            )
        count = Receipt.objects.filter(
            job=job, key__in=[f"related:{c['number']}" for c in comments]
        ).count()
        _progress(job, category, "done" if count else "unused", count)
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
        or not checks.get(
            "local_passed", checks["passed"] if job.pr.repository.test_command else True
        )
        or any(f["severity"] == "blocking" for f in result["findings"])
        or any(reply.get("blocking", False) for _, reply in replies)
    )
    body = f"Commit: `{job.head_sha}`\n\n{result['summary']}\n\nChecks: {checks['summary']}\n\n"
    body += (
        f"Changes or clarification required. After addressing feedback, request @{job.pr.repository.reviewer} again if needed, then toggle `{job.pr.repository.feedback_label}` → `{job.pr.repository.review_label}`."
        if blocked
        else "No blocking findings. Formal GitHub approval follows the repository auto-approve setting. Merging follows the separate opt-in auto-merge setting; repository merge rules still apply."
    )
    if not checks["passed"]:
        body += "\n\nCI is advisory for this code review; ensure all CI checks pass before merging."
    # Use issue summary first: preserves reviewer request while thread replies publish.
    _progress(job, "summary", "started")
    once(github, job, "summary", body, f"/issues/{number}/comments", f"/issues/{number}/comments")
    _progress(job, "summary", "done", 1)
    _, baseline_timeline = _guard_state(github, job)
    update_cursor(
        job,
        "review",
        blocked=blocked,
        baseline=[{"event": x.get("event"), "id": x["id"]} for x in baseline_timeline if "id" in x],
        label_transitions=[],
    )
    return _publish_review(github, job, inline, blocked, baseline_timeline)


def _related_targets(job, items):
    """Context retrieval enrolls same-repository targets; reject conflicting metadata."""
    from urllib.parse import urlparse

    targets = set()
    repo = job.pr.repository.full_name.casefold()
    for item in items:
        number = item["number"]
        if type(number) is not int or number <= 0:
            raise ValueError("Invalid related target")
        repository = item.get("repository") or (item.get("base") or {}).get("repo") or {}
        if repository.get("full_name", repo).casefold() != repo:
            raise ValueError("Unknown related repository")
        if item.get("repository_url"):
            if urlparse(item["repository_url"]).path.casefold() != f"/repos/{repo}":
                raise ValueError("Unknown related repository")
        targets.add(number)
    return targets


def review_event(job, blocked):
    from core.approvals import enabled_for

    _, state = checkpoint_state(job)
    return state.get("review_event") or (
        "REQUEST_CHANGES" if blocked else "APPROVE" if enabled_for(job) else "COMMENT"
    )


def find_review(github, job, blocked):
    number = job.pr.number
    tag = marker(job, "review")
    return next(
        (
            x
            for x in github.pages(f"/pulls/{number}/reviews")
            if tag in (x.get("body") or "")
            and (x.get("user") or {}).get("login", "").casefold() == github.identity().casefold()
            and x.get("commit_id") == job.head_sha
            and x.get("state")
            == {
                "REQUEST_CHANGES": "CHANGES_REQUESTED",
                "APPROVE": "APPROVED",
                "COMMENT": "COMMENTED",
            }[review_event(job, blocked)]
        ),
        None,
    )


def _publish_review(github, job, inline, blocked, baseline_timeline):
    number = job.pr.number
    tag = marker(job, "review")
    from core.approvals import dismiss_stale, enabled_for, remember, withdraw
    from core.github import IntegrationError

    event = review_event(job, blocked)
    existing = find_review(github, job, blocked)
    stage, _ = checkpoint_state(job)
    if stage == "labels" and (inline or blocked or event == "APPROVE") and not existing:
        raise StaleReview("Published review is no longer available")
    if not existing and event == "APPROVE" and not enabled_for(job):
        event = "COMMENT"
    update_cursor(job, stage or "review", review_event=event)
    needs_review = bool(inline or blocked or event == "APPROVE")
    if inline:
        _progress(job, "inline", "started")
    else:
        _progress(job, "inline", "unused", 0)
    if stage == "labels" and needs_review and not existing:
        raise StaleReview("Published review is no longer available")
    if not existing and needs_review:
        guard(github, job)
        if event == "APPROVE":
            fresh = github.pull(number)
            if fresh.get("auto_merge"):
                raise IntegrationError(
                    "GitHub auto-merge is enabled on this PR; disable it before automatic approval."
                )
            author = (fresh.get("user") or {}).get("login")
            if not author:
                raise IntegrationError("GitHub PR author could not be verified.")
            if author.casefold() == github.identity().casefold():
                raise IntegrationError(
                    "Connected GitHub account cannot approve its own PR; another reviewer is required."
                )
            remember(github, job, tag)
            guard(github, job)
        with admission(job):
            if blocked or event == "APPROVE":
                expect_label(
                    job=job,
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
                    "event": event,
                    "body": f"Automated review. See summary above. CI is advisory; ensure all CI checks pass before merging.\n\n{tag}",
                    "comments": inline,
                },
            )
            persist_receipt(job=job, key="review", defaults={"remote_id": str(posted["id"])})
    elif existing:
        persist_receipt(job=job, key="review", defaults={"remote_id": str(existing["id"])})

    if inline and Receipt.objects.filter(job=job, key="review").exclude(remote_id="").exists():
        _progress(job, "inline", "done", len(inline))

    if event == "APPROVE" and Receipt.objects.filter(job=job, key="review").exists():
        record = remember(github, job, tag)
        with mutation(job):
            record.review_id = Receipt.objects.get(job=job, key="review").remote_id
            record.save(update_fields=["review_id"])
        # Catch a push racing the approval POST; the webhook/sweeper also repairs this.
        dismiss_stale(github, job.pr.repository, number)

    if blocked and Receipt.objects.filter(job=job, key="review").exists():
        persist_receipt(job=job, key="request_changes")

    update_cursor(job, "labels")
    try:
        return terminal_transition(
            github, job, blocked=blocked, baseline_timeline=baseline_timeline
        )
    except GitHubRateLimited:
        raise
    except StaleReview:
        if event == "APPROVE":
            withdraw(github, job)
        raise
    except Exception as exc:
        raise LabelCleanupError(safe_error(exc)) from None


def fail_to_feedback(github, job):
    _, baseline_timeline = _guard_state(github, job)
    return terminal_transition(github, job, blocked=True, baseline_timeline=baseline_timeline)


def terminal_transition(github, job, *, blocked, baseline_timeline):
    number = job.pr.number
    # Pin terminal label normalization to the checked timeline. Only this call's
    # exact label writes may be excluded from later eligibility checks.
    _, state = checkpoint_state(job)
    own_transitions = [tuple(x) for x in state.get("label_transitions", [])]
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
        "review_dismissed",
    }

    def terminal_guard():
        if not owned(job):
            raise StaleReview("Execution ownership lost")
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
        # COMMENT and REQUEST_CHANGES both consume a pending review request.
        # Only this job's successfully persisted review permits that implicit
        # change. Explicit lifecycle events below still invalidate publication.
        allow_removed = Receipt.objects.filter(job=job, key="review").exclude(remote_id="").exists()
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

    def record_transition(action, label):
        own_transitions.append((action, label))
        update_cursor(job, "labels", label_transitions=own_transitions)

    terminal_guard()
    _progress(job, "labels", "started")
    transition(
        github,
        job.pr.repository,
        number,
        job.pr.repository.feedback_label if blocked else job.pr.repository.approved_label,
        terminal_guard,
        record_transition,
        job=job,
    )
    terminal_guard()
    _progress(job, "labels", "done", len(own_transitions))
    return "feedback" if blocked else "ready"

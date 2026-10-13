"""Encrypted analysis checkpoints and bounded, receipt-aware publication retries."""

import hashlib
import json
import re
import time
from datetime import timedelta

from django.utils import timezone

from .models import Job, ReviewCheckpoint
from .secrets import decrypt, encrypt


class DeliveryPaused(Exception):
    pass


def save_review(job, result, context, checks):
    payload = {"result": result, "context": context, "checks": checks, "head_sha": job.head_sha}
    ReviewCheckpoint.objects.create(job=job, encrypted_payload=encrypt(json.dumps(payload)))


def checkpoint_state(job):
    checkpoint = ReviewCheckpoint.objects.filter(job=job).first()
    return (checkpoint.stage, checkpoint.state) if checkpoint else ("", {})


def update_cursor(job, stage, **state):
    checkpoint = ReviewCheckpoint.objects.filter(job=job).first()
    if checkpoint:
        checkpoint.stage = stage
        checkpoint.state.update(state)
        checkpoint.save(update_fields=["stage", "state"])


def safe_error(exc):
    message = str(exc)
    if re.fullmatch(r"GitHub request failed \(HTTP [1-5][0-9]{2}\)\.", message):
        return message
    if message in {
        "GitHub request unavailable or malformed.",
        "GitHub returned malformed data.",
        "GitHub discussion context unavailable.",
        "GitHub did not confirm discussion resolution.",
        "Required checks changed during review",
        "GitHub PR author could not be verified.",
        "GitHub auto-merge is enabled on this PR; disable it before automatic approval.",
        "Connected GitHub account cannot approve its own PR; another reviewer is required.",
    }:
        return message
    return "GitHub delivery could not be confirmed."


def transient(exc):
    message = safe_error(exc)
    return (
        bool(re.fullmatch(r"GitHub request failed \(HTTP 5[0-9]{2}\)\.", message))
        or message == "GitHub request unavailable or malformed."
    )


def load_review(job):
    from .publication import StaleReview

    checkpoint = ReviewCheckpoint.objects.get(job=job)
    if (
        checkpoint.created_at < timezone.now() - timedelta(days=90)
        or not checkpoint.encrypted_payload
    ):
        raise StaleReview("Saved review expired")
    data = json.loads(decrypt(checkpoint.encrypted_payload))
    if data.get("head_sha") != job.head_sha or checkpoint.stage == "done":
        raise StaleReview("Saved review is no longer applicable")
    return checkpoint, data


def deliver(github, job, publisher, progress):
    from .github import IntegrationError
    from .publication import LabelCleanupError, find_review

    checkpoint, data = load_review(job)
    for attempt in range(3):
        checkpoint.refresh_from_db()
        checkpoint.delivery_attempts += 1
        checkpoint.save(update_fields=["delivery_attempts"])
        try:
            if checkpoint.delivery_attempts > 1:
                if checkpoint.stage in ("review", "labels"):
                    from .models import Receipt

                    existing = find_review(github, job, checkpoint.state["blocked"])
                    if existing:
                        Receipt.objects.get_or_create(
                            job=job, key="review", defaults={"remote_id": str(existing["id"])}
                        )
                verify_inputs(github, job, data["context"], data["result"])
            outcome = publisher(github, job, data["result"], data["context"], data["checks"])
            # Keep a concise record, not a permanent copy of all source files.
            record = {
                "head_sha": job.head_sha,
                "result": data["result"],
                "outcome": outcome,
                "evidence_sha256": hashlib.sha256(
                    json.dumps(data["context"], sort_keys=True).encode()
                ).hexdigest(),
            }
            checkpoint.encrypted_payload = encrypt(json.dumps(record))
            checkpoint.stage = "done"
            checkpoint.save(update_fields=["encrypted_payload", "stage"])
            return outcome
        except (IntegrationError, LabelCleanupError) as exc:
            reason = safe_error(exc)
            if attempt < 2 and transient(exc):
                progress("delivery_retry")
                time.sleep(2 * (attempt + 1))
                continue
            raise DeliveryPaused(
                f"{reason} Saved analysis retained; retry delivery without another model call."
            ) from None
    raise AssertionError("Unreachable delivery loop")


def retry_available(job):
    return (
        job.status in ("attention", "failed")
        and job.generation == job.pr.generation
        and job.head_sha == job.pr.head_sha
        and job.pr.repository.enabled
        and ReviewCheckpoint.objects.filter(
            job=job,
            stage__in=["ready", "review", "labels"],
            created_at__gte=timezone.now() - timedelta(days=90),
        )
        .exclude(encrypted_payload="")
        .exists()
    )


def queue_retry(job):
    from django.db import transaction

    from .retries import RetryRejected

    with transaction.atomic():
        row = Job.objects.select_for_update().select_related("pr__repository").get(pk=job.pk)
        if not retry_available(row):
            raise RetryRejected(
                "Saved delivery is unavailable or superseded. Request a new review."
            )
        # Same attempt/markers/receipts. Worker rechecks GitHub before any write.
        row.status = "queued"
        row.reason = "Saved review queued for delivery; no model rerun."
        row.finished_at = None
        row.save(update_fields=["status", "reason", "finished_at"])
        return row


def verify_inputs(github, job, context, result):
    """Refresh mutable requirements. Source/doc contents remain pinned to the SHA."""
    from .publication import StaleReview

    def changed():
        raise StaleReview("Review requirements or discussion changed since analysis")

    fresh = github.pull(job.pr.number)
    if fresh["head"]["sha"] != job.head_sha:
        changed()
    old = context.get("pr", {})
    if old and any(fresh.get(k) != old.get(k) for k in ("title", "body")):
        changed()
    if "instructions" in context:
        job.pr.repository.refresh_from_db()
        if job.pr.repository.instructions != context["instructions"]:
            changed()

    def comments(values):
        rows = []
        for c in values:
            body = c.get("body") or ""
            # Discard only unedited comments from this attempt, authored by this account.
            if (
                f"<!-- review-desk:{job.pk}:" in body
                and (c.get("user") or {}).get("login", "").casefold()
                == github.identity().casefold()
                and c.get("created_at") == c.get("updated_at")
            ):
                continue
            rows.append((c.get("id"), body, c.get("updated_at")))
        return rows

    if "comments" in context and comments(
        github.pages(f"/issues/{job.pr.number}/comments")
    ) != comments(context["comments"]):
        changed()
    for issue in context.get("issues", []):
        latest = github.get(f"/issues/{issue['number']}")
        if any(latest.get(k) != issue.get(k) for k in ("title", "body", "state")):
            changed()
        if comments(github.pages(f"/issues/{issue['number']}/comments")) != comments(
            issue.get("comments", [])
        ):
            changed()
    for related in context.get("related_prs", []):
        latest = github.pull(related["number"])
        if (
            any(latest.get(k) != related.get(k) for k in ("title", "body", "state", "merged"))
            or latest["head"]["sha"] != related["head"]["sha"]
        ):
            changed()
        if comments(github.pages(f"/issues/{related['number']}/comments")) != comments(
            related.get("comments", [])
        ):
            changed()
    if context.get("tasks") and job.pr.repository.sheet_credential:
        from .sheets import task_context

        if task_context(job.pr.repository, [t["id"] for t in context["tasks"]]) != context["tasks"]:
            changed()
    if "threads" in context:
        from .context import review_thread, threads
        from .models import Receipt
        from .publication import _reply_body, marker, resolution_confirmed, thread_replies

        assessed = thread_replies(result, context)
        original_ids = {t["id"] for t in context["threads"]}
        actor = github.identity().casefold()
        for original, reply in assessed:
            latest = review_thread(github, original["id"])
            if latest["pullRequest"]["headRefOid"] != job.head_sha:
                changed()
            resolved_by_job = Receipt.objects.filter(
                job=job, key=f"resolve:{original['id']}"
            ).exists()
            if (
                latest["isResolved"]
                and not resolved_by_job
                and resolution_confirmed(github, job, original, reply, latest)
            ):
                Receipt.objects.get_or_create(
                    job=job, key=f"resolve:{original['id']}", defaults={"remote_id": original["id"]}
                )
                resolved_by_job = True
            if latest["isResolved"] != resolved_by_job:
                changed()
            root = original["comments"][0]["databaseId"]
            expected = f"**Automated review · Auto Reviewer**\n\n{_reply_body(job, reply, latest.get('viewerCanResolve') or resolved_by_job)}\n\n{marker(job, f'reply:{root}')}"
            filtered = [
                c
                for c in latest["comments"]
                if not (
                    c.get("body") == expected
                    and (c.get("author") or {}).get("login", "").casefold() == actor
                )
            ]

            def comparable(rows):
                return [
                    (c.get("databaseId"), c.get("body"), c.get("updatedAt"), c.get("author"))
                    for c in rows
                ]

            if comparable(filtered) != comparable(original["comments"]):
                changed()
        finding_bodies = {f"**{f['severity']}** — {f['body']}" for f in result["findings"]}
        posted = Receipt.objects.filter(job=job, key="review").exists()
        for t in threads(github, job.pr.repository.full_name, job.pr.number):
            if t["id"] in original_ids:
                continue
            rows = t["comments"]
            if not (
                posted
                and len(rows) == 1
                and rows[0].get("body") in finding_bodies
                and (rows[0].get("author") or {}).get("login", "").casefold() == actor
            ):
                changed()
    for related in context.get("related_prs", []):
        if "threads" in related:
            from .context import threads

            if (
                threads(github, job.pr.repository.full_name, related["number"])
                != related["threads"]
            ):
                changed()

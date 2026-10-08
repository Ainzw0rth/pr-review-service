"""Approval-only Telegram alerts using the existing Hermes bot identity.

No inference, incoming update polling, or agent sessions. Ambiguous sends are not
replayed: Telegram sendMessage has no application idempotency key.
"""

import fcntl
import json
import logging
import re
from datetime import timedelta

import httpx
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import ApprovalNotification, Credential, Job
from .secrets import decrypt


class _HideTelegramRequest(logging.Filter):
    def filter(self, record):
        # httpx INFO logs include the URL, and Telegram authenticates in that URL.
        return "api.telegram.org/bot" not in record.getMessage()


logging.getLogger("httpx").addFilter(_HideTelegramRequest())


def _configuration():
    if not settings.TELEGRAM_APPROVAL_ENABLED:
        return None
    since = parse_datetime(settings.TELEGRAM_APPROVAL_SINCE)
    if not since or timezone.is_naive(since) or not settings.TELEGRAM_APPROVAL_REPOSITORIES:
        return None
    credential = Credential.objects.get(pk=settings.TELEGRAM_APPROVAL_CREDENTIAL_ID)
    data = json.loads(decrypt(credential.encrypted_value))
    token, chat = data.get("token"), data.get("chat_id")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Invalid Telegram credential")
    if not isinstance(chat, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", chat):
        raise ValueError("Telegram approval destination must be a private chat")
    return token, chat, since


def _queue(since):
    jobs = (
        Job.objects.filter(
            status="completed",
            finished_at__gte=since,
            pr__repository__full_name__in=settings.TELEGRAM_APPROVAL_REPOSITORIES,
            review_checkpoint__stage="done",
            approval_notification__isnull=True,
        )
        .select_related("review_checkpoint")
        .order_by("finished_at")[:100]
    )
    for job in jobs:
        try:
            saved = json.loads(decrypt(job.review_checkpoint.encrypted_payload))
            ready = saved.get("outcome") == "ready" and saved.get("head_sha") == job.head_sha
            state, reason = ("pending", "") if ready else ("ignored", "Not an AI-approved outcome.")
        except Exception:
            state, reason = "failed", "Saved approval could not be verified."
        ApprovalNotification.objects.get_or_create(
            job=job, defaults={"status": state, "reason": reason}
        )


def _message(job):
    from .models import ApprovalRecord

    formal = ApprovalRecord.objects.filter(job=job).exclude(review_id="").exists()
    final_note = (
        "Formal GitHub approval submitted. Merge remains manual."
        if formal
        else "Approved workflow status published. Human approval/merge is still required."
    )
    at = timezone.localtime(job.finished_at).strftime("%d %b %Y, %H:%M WIB")
    return (
        f"✅ AI review approved\n{job.pr.repository.full_name} #{job.pr.number}\n"
        f"Commit: {job.head_sha[:8]} · Attempt {job.generation}\n{at}\n\n"
        f"{job.pr.url}\n{settings.PUBLIC_URL.rstrip('/')}/jobs/{job.pk}/\n\n"
        f"{final_note}"
    )


def _retry(row, delay, reason):
    row.status = "pending" if row.attempts < 8 else "failed"
    row.reason = reason
    row.next_attempt_at = timezone.now() + timedelta(seconds=max(60, min(delay, 86400)))


def flush_approvals():
    """Maintenance-only. A shared-volume lock serializes queueing and network delivery."""
    try:
        config = _configuration()
    except Exception:
        return {"error": "configuration"}
    if not config:
        return {"sent": 0}
    token, chat, since = config
    with (settings.DATA_DIR / "telegram-approvals.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"sent": 0}
        # A prior process may have died after Telegram accepted the message.
        ApprovalNotification.objects.filter(status="sending").update(
            status="uncertain",
            reason="Sender interrupted; delivery unconfirmed. Not automatically resent.",
        )
        _queue(since)
        if ApprovalNotification.objects.filter(
            status="pending",
            attempts__gt=0,
            next_attempt_at__gt=timezone.now(),
            job__finished_at__gte=since,
            job__pr__repository__full_name__in=settings.TELEGRAM_APPROVAL_REPOSITORIES,
        ).exists():
            return {"sent": 0}
        pending = (
            ApprovalNotification.objects.filter(
                status="pending",
                next_attempt_at__lte=timezone.now(),
                job__finished_at__gte=since,
                job__pr__repository__full_name__in=settings.TELEGRAM_APPROVAL_REPOSITORIES,
            )
            .select_related("job__pr__repository")
            .order_by("created_at")[:20]
        )
        sent = 0
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            for row in pending:
                row.status = "sending"
                row.attempts += 1
                row.save(update_fields=["status", "attempts"])
                try:
                    response = client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={
                            "chat_id": chat,
                            "text": _message(row.job),
                            "link_preview_options": {"is_disabled": True},
                        },
                    )
                    data = response.json()
                    if response.status_code == 200 and data.get("ok") is True:
                        result = data.get("result") or {}
                        message_id = result.get("message_id")
                        if (
                            type(message_id) is not int
                            or str((result.get("chat") or {}).get("id")) != chat
                        ):
                            raise ValueError("Invalid Telegram receipt")
                        row.status = "sent"
                        row.sent_at = timezone.now()
                        row.message_id = str(message_id)
                        row.reason = ""
                        sent += 1
                    elif response.status_code == 429 and data.get("ok") is False:
                        delay = (data.get("parameters") or {}).get("retry_after", 60)
                        _retry(
                            row,
                            delay if type(delay) is int else 60,
                            "Telegram rate limit; waiting before retry.",
                        )
                    elif 400 <= response.status_code < 500 and data.get("ok") is False:
                        row.status = "failed"
                        row.reason = "Telegram rejected notification. Check bot credential and private chat access."
                    else:
                        row.status = "uncertain"
                        row.reason = "Telegram delivery unconfirmed. Not automatically resent."
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                    _retry(
                        row,
                        60 * 2 ** min(row.attempts, 6),
                        "Telegram connection unavailable; retry scheduled.",
                    )
                except Exception:
                    # Never store/log HTTP exception text: request URLs contain the bot token.
                    row.status = "uncertain"
                    row.reason = "Telegram delivery unconfirmed. Not automatically resent."
                row.save(
                    update_fields=["status", "reason", "message_id", "sent_at", "next_attempt_at"]
                )
                if row.status == "pending":
                    break  # Respect provider throttling across the whole bot destination.
        return {"sent": sent}

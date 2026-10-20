"""Opt-in final-summary journal. Fixed protocol, encrypted outbox, no model-chosen destination."""

import json
import re
import uuid
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .claims import fenced
from .models import Credential, JournalEntry
from .secrets import decrypt, encrypt

MAX_SUMMARY_BYTES = 65536
FAILURE = "Journal delivery failed; check connection, credential and receipt. Will retry."


def enabled(repository):
    return settings.JOURNAL_ENABLED and repository in settings.JOURNAL_REPOSITORIES


def enqueue(event, repository, job=None):
    if not enabled(repository):
        return None
    if (
        event.get("schemaVersion") != 1
        or event.get("project") != repository
        or set(event)
        != {"schemaVersion", "eventId", "sessionId", "project", "source", "endedAt", "summary"}
        or str(uuid.UUID(event["eventId"])) != event["eventId"]
        or not isinstance(event["summary"], str)
        or not event["summary"].strip()
        or "\0" in event["summary"]
        or len(event["summary"].encode()) > MAX_SUMMARY_BYTES
    ):
        raise ValueError("Invalid journal event")
    for key, limit in [("sessionId", 200), ("project", 200), ("source", 80)]:
        value = event[key]
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("Invalid journal metadata")
    ended = parse_datetime(event["endedAt"])
    if ended is None or timezone.is_naive(ended):
        raise ValueError("Invalid journal timestamp")
    entry, _ = JournalEntry.objects.get_or_create(
        id=event["eventId"],
        defaults={
            "job": job,
            "repository": repository,
            "encrypted_payload": encrypt(json.dumps(event, ensure_ascii=False)),
        },
    )
    return entry


@fenced(terminal=True)
def queue_job(job, result=None, outcome=None):
    job.refresh_from_db()
    repository = job.pr.repository.full_name
    if not enabled(repository) or job.status in {"queued", "running"}:
        return None
    attempt = job.model_attempts.last()
    event_id = uuid.uuid5(job.id, f"{attempt.pk if attempt else 'no-model'}:{job.status}")
    lines = [
        f"Auto Reviewer — {repository} PR #{job.pr.number}",
        f"PR: {job.pr.url}",
        f"Job: {settings.PUBLIC_URL.rstrip('/')}/jobs/{job.id}/",
        f"Commit: `{job.head_sha}`",
        f"Run status: {job.get_status_display()}",
    ]
    if attempt:
        lines.append(f"Model: {attempt.provider_name} / {attempt.model}; attempt: {attempt.status}")
    if job.reason:
        lines.append(f"Operational note: {job.reason}")
    # Never journal rejected/incomplete model text or claim failed publication succeeded.
    if job.status == "completed" and result is not None:
        lines.extend(
            [
                f"Published workflow outcome: {outcome or 'see GitHub'} (not formal human approval)",
                f"Findings: {len(result['findings'])}; thread assessments: {len(result['replies'])}",
                "",
                "Final review summary:",
                result["summary"],
                "",
                "Individual findings and discussion details remain on GitHub.",
            ]
        )
    else:
        lines.append(
            "No accepted final review summary is included. Check GitHub for any partial publication."
        )
    return enqueue(
        {
            "schemaVersion": 1,
            "eventId": str(event_id),
            "sessionId": f"auto-reviewer:{job.id}",
            "project": repository,
            "source": "auto-reviewer",
            "endedAt": timezone.now().isoformat(),
            "summary": "\n".join(lines),
        },
        repository,
        job,
    )


def flush():
    if not settings.JOURNAL_ENABLED:
        return {"sent": 0, "failed": 0}
    entries = list(
        JournalEntry.objects.filter(
            sent_at__isnull=True, repository__in=settings.JOURNAL_REPOSITORIES
        ).order_by("created_at")[:8]
    )
    if not entries:
        return {"sent": 0, "failed": 0}
    try:
        url = urlsplit(settings.JOURNAL_ENDPOINT)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path != "/api/v1/sessions"
        ):
            raise ValueError("Invalid journal endpoint")
        credential = Credential.objects.get(pk=settings.JOURNAL_CREDENTIAL_ID)
        token = decrypt(credential.encrypted_value).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,256}", token):
            raise ValueError("Invalid journal credential")
    except Exception:
        JournalEntry.objects.filter(pk__in=[e.pk for e in entries]).update(last_error=FAILURE)
        return {"sent": 0, "failed": len(entries)}
    sent, failed = 0, 0
    with httpx.Client(timeout=httpx.Timeout(5), follow_redirects=False, trust_env=False) as client:
        for entry in entries:
            JournalEntry.objects.filter(pk=entry.pk).update(attempts=F("attempts") + 1)
            try:
                event = json.loads(decrypt(entry.encrypted_payload))
                if event["eventId"] != str(entry.id) or event["project"] != entry.repository:
                    raise ValueError("Journal identity mismatch")
                with client.stream(
                    "POST",
                    settings.JOURNAL_ENDPOINT,
                    headers={"Authorization": f"Bearer {token}"},
                    json=event,
                ) as response:
                    if response.status_code not in (200, 201):
                        raise ValueError("Journal rejected delivery")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 4096:
                            raise ValueError("Invalid journal receipt")
                receipt = json.loads(raw)
                accepted = parse_datetime(receipt.get("acceptedAt", ""))
                if (
                    receipt.get("eventId") != str(entry.id)
                    or receipt.get("status") != "accepted"
                    or accepted is None
                    or timezone.is_naive(accepted)
                ):
                    raise ValueError("Invalid journal receipt")
                JournalEntry.objects.filter(pk=entry.pk).update(
                    sent_at=timezone.now(),
                    encrypted_payload="",
                    last_error="",
                )
                sent += 1
            except Exception:
                JournalEntry.objects.filter(pk=entry.pk).update(last_error=FAILURE)
                failed += 1
    return {"sent": sent, "failed": failed}

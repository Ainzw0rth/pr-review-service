"""GitHub rate classification and durable token-hash backoff; no response content stored."""

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from django.db import transaction
from django.utils import timezone as django_timezone

from .models import GitHubCooldown

ERROR_BYTES = 16 * 1024
RATE_MESSAGES = ("secondary rate limit", "api rate limit exceeded", "abuse detection")


def scope(token):
    return hashlib.sha256(token.encode()).hexdigest()


def active(token_scope):
    return GitHubCooldown.objects.filter(
        scope=token_scope, retry_at__gt=django_timezone.now()
    ).first()


def remember(token_scope, retry_at, kind):
    with transaction.atomic():
        GitHubCooldown.objects.get_or_create(
            scope=token_scope, defaults={"retry_at": retry_at, "kind": kind}
        )
        row = GitHubCooldown.objects.select_for_update().get(scope=token_scope)
        if retry_at > row.retry_at:
            row.retry_at, row.kind = retry_at, kind
            row.save(update_fields=["retry_at", "kind"])
        return row


def _reset(value, now):
    try:
        result = datetime.fromtimestamp(int(value), timezone.utc) + timedelta(seconds=5)
        return result if result > now else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _retry_after(value, now):
    if not value:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            result = parsedate_to_datetime(value)
            if result.tzinfo is None:
                result = result.replace(tzinfo=timezone.utc)
            result = result.astimezone(timezone.utc) + timedelta(seconds=1)
            return result if result > now else None
        except (ValueError, TypeError, OverflowError):
            return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    try:
        return now + timedelta(seconds=seconds + 1)
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc)


def bounded_error(response):
    raw = bytearray()
    for chunk in response.iter_bytes(chunk_size=ERROR_BYTES):
        remaining = ERROR_BYTES - len(raw)
        raw.extend(chunk[:remaining])
        if len(raw) >= ERROR_BYTES:
            break
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return ""
    message = value.get("message") if isinstance(value, dict) else None
    return message.lower() if isinstance(message, str) else ""


def classify(status, headers, message="", *, now=None):
    """403 alone is permission denied; 429, explicit headers, or known messages are limits."""
    if status not in (403, 429):
        return None
    now = now or django_timezone.now()
    primary = headers.get("x-ratelimit-remaining") == "0" or "api rate limit exceeded" in message
    if not (
        status == 429
        or primary
        or "retry-after" in headers
        or any(fragment in message for fragment in RATE_MESSAGES)
    ):
        return None
    kind = "primary" if primary else "secondary"
    deadlines = []
    retry_after = _retry_after(headers.get("retry-after"), now)
    if retry_after:
        deadlines.append(retry_after)
    if primary:
        reset = _reset(headers.get("x-ratelimit-reset"), now)
        if reset:
            deadlines.append(reset)
        else:
            deadlines.append(now + timedelta(seconds=60))
    if not deadlines:
        deadlines.append(now + timedelta(seconds=60))
    return max(deadlines), kind

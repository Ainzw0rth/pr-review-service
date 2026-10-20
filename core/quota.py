"""Subscription availability; never infer account limits from token counts."""

import json
import math
import os
import subprocess
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

from .models import Provider, SubscriptionQuota

KINDS = {"claude_cli": "Anthropic · Claude", "codex_cli": "OpenAI · Codex"}
WINDOWS = {
    "five_hour": "5-hour window",
    "seven_day": "Weekly window",
    "seven_day_opus": "Weekly Opus window",
    "primary": "Primary window",
    "secondary": "Secondary window",
}


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    if not 1_500_000_000 <= value <= 4_102_444_800:
        return None
    return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)


def store_snapshot(kind, data):
    if kind not in KINDS:
        return
    now = timezone.now()
    row, _ = SubscriptionQuota.objects.get_or_create(kind=kind)
    windows = []
    for value in data.get("windows", [])[:5] if isinstance(data.get("windows"), list) else []:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("name"), str)
            or value["name"] not in WINDOWS
        ):
            continue
        used = value.get("used_percent")
        reset = timestamp(value.get("resets_at"))
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            continue
        windows.append(
            {
                "name": value["name"],
                "used_percent": used,
                "resets_at": int(reset.timestamp()) if reset else None,
            }
        )
    row.checked_at = now
    if windows:
        row.windows, row.observed_at, row.error = windows, now, ""
        exhausted = [
            timestamp(w["resets_at"]) or now + timedelta(minutes=30)
            for w in windows
            if w["used_percent"] >= 100
            and (timestamp(w["resets_at"]) is None or timestamp(w["resets_at"]) > now)
        ]
        row.blocked_until = max(exhausted) if exhausted else None
    else:
        error = data.get("error")
        row.error = (
            error
            if isinstance(error, str) and error in {"auth", "unavailable", "unsupported"}
            else "unavailable"
        )
    row.save()


def blocked_until(provider):
    if provider.kind not in KINDS:
        return None
    row = SubscriptionQuota.objects.filter(kind=provider.kind).first()
    return (
        row.blocked_until
        if row and row.blocked_until and row.blocked_until > timezone.now()
        else None
    )


def record_exhausted(provider, retry_at=None):
    if provider.kind not in KINDS:
        return None
    now = timezone.now()
    until = retry_at if retry_at and retry_at > now else now + timedelta(minutes=30)
    row, _ = SubscriptionQuota.objects.get_or_create(kind=provider.kind)
    row.blocked_until = max(until, row.blocked_until) if row.blocked_until else until
    row.error = "exhausted"
    row.save(update_fields=["blocked_until", "error"])
    return row.blocked_until


def cards():
    now = timezone.now()
    rows = {r.kind: r for r in SubscriptionQuota.objects.all()}
    result = []
    for kind, title in KINDS.items():
        row = rows.get(kind)
        exhausted = bool(row and row.blocked_until and row.blocked_until > now)
        stale = (
            not row
            or not row.observed_at
            or now - row.observed_at > timedelta(minutes=10)
            or bool(row.error)
        )
        windows = []
        for w in row.windows if row else []:
            reset = timestamp(w["resets_at"])
            windows.append(
                {
                    "label": WINDOWS[w["name"]],
                    "remaining": round(100 - w["used_percent"], 1),
                    "resets_at": reset,
                    "stale": stale or bool(reset and reset <= now),
                }
            )
        result.append(
            {
                "kind": kind,
                "title": title,
                "windows": windows,
                "state": "exhausted"
                if exhausted
                else "unknown"
                if stale or not windows or any(w["stale"] for w in windows)
                else "available",
                "checked_at": row.checked_at if row else None,
                "observed_at": row.observed_at if row else None,
                "retry_at": row.blocked_until if exhausted else None,
            }
        )
    return result


def refresh_due():
    """Worker-only polling: no model calls, no Docker access in the web process."""
    if not settings.CLI_RUNNER_IMAGE:
        return
    from .capacity import CapacityBusy, CapacityError, Container, cli_scope, limit, permit
    from .cli import container_arguments, require_auth_coordination
    from .runner import _docker

    now = timezone.now()
    for kind in (
        Provider.objects.filter(enabled=True, kind__in=KINDS)
        .values_list("kind", flat=True)
        .distinct()
    ):
        row = SubscriptionQuota.objects.filter(kind=kind).first()
        if row and row.checked_at and now - row.checked_at < timedelta(minutes=5):
            continue
        volume, scope = cli_scope(kind)
        data = {"error": "unavailable"}
        try:
            with permit(
                scope, limit("CLI_ACCOUNT_CONCURRENCY", 1), lambda: False, wait=False
            ) as slot:
                if limit("CLI_ACCOUNT_CONCURRENCY", 1) > 1:
                    require_auth_coordination()
                with Container(slot, _docker, volume=volume) as container:
                    _docker(["volume", "inspect", volume])
                    identifier = container.create(
                        container_arguments(kind, slot.name, settings.CLI_RUNNER_IMAGE, volume)
                    )
                    try:
                        process = subprocess.run(
                            [
                                "docker",
                                "--host=unix:///var/run/docker.sock",
                                "start",
                                "-a",
                                "-i",
                                identifier,
                            ],
                            input=json.dumps({"action": "quota", "kind": kind}),
                            text=True,
                            capture_output=True,
                            timeout=40,
                            check=False,
                            env={
                                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                "HOME": "/nonexistent",
                            },
                        )
                        if process.returncode == 0 and len(process.stdout) <= 8192:
                            parsed = json.loads(process.stdout)
                            if isinstance(parsed, dict):
                                data = parsed
                    except (subprocess.SubprocessError, ValueError, OSError):
                        pass
        except CapacityBusy:
            continue  # Do not change checked_at or availability for a busy account.
        except CapacityError:
            raise
        except Exception:
            pass  # Static unavailable snapshot; never persist subprocess output.
        store_snapshot(kind, data)

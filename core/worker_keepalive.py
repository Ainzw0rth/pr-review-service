"""Owner-configured SSH connectivity checks; never worker readiness evidence."""

import base64
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.secrets import decrypt, encrypt
from core.worker_keepalive_models import WorkerKeepalive

COMMAND_TIMEOUT = 30
LEASE_SECONDS = 120


def _validate(config):
    host = config.get("host", "")
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ValueError("A valid SSH host is required.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
            for part in host.split(".")
        ):
            raise ValueError("Invalid SSH host.") from None
    # Scoped IPv6 addresses embed interface identifiers; do not accept them.
    if "%" in host:
        raise ValueError("Invalid SSH host.")
    user = config.get("user", "")
    if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@+-]{0,511}", user):
        raise ValueError("Invalid SSH user.")
    port = config.get("port", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid SSH port.")
    key = config.get("known_host_key", "")
    if not isinstance(key, str) or len(key) > 8192:
        raise ValueError("A verified SSH public host key is required.")
    parts = key.split(" ")
    if len(parts) != 2 or parts[0] not in {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
    }:
        raise ValueError("Supply the verified host key type and base64 key.")
    try:
        raw = base64.b64decode(parts[1], validate=True)
        length = int.from_bytes(raw[:4], "big")
        if raw[4 : 4 + length] != parts[0].encode() or len(raw) <= 4 + length:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Invalid SSH public host key.") from None


def configure(worker, data):
    if not isinstance(data, dict) or type(data.get("enabled", False)) is not bool:
        raise ValueError("Enabled must be a boolean.")
    minutes = data.get("interval_minutes", 20)
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        raise ValueError("Interval must be 1–1440 minutes.")
    with transaction.atomic():
        # Serialize first-time configuration too.
        type(worker).objects.select_for_update().get(pk=worker.pk)
        row, _ = WorkerKeepalive.objects.get_or_create(worker=worker)
        config = json.loads(decrypt(row.encrypted_config)) if row.encrypted_config else {}
        for field in ("host", "port", "user", "known_host_key"):
            if data.get(field) not in (None, ""):
                config[field] = data[field]
        config.setdefault("port", 22)
        if data.get("enabled", False) or config:
            _validate(config)
        row.encrypted_config = encrypt(json.dumps(config)) if config else ""
        row.enabled = data.get("enabled", False)
        row.interval_seconds = minutes * 60
        row.next_due_at = timezone.now() if row.enabled else None
        row.save()
    return public_status(worker)


def public_status(worker):
    row = WorkerKeepalive.objects.filter(worker=worker).first()
    if row is None:
        return {
            "enabled": False,
            "interval_minutes": 20,
            "status": "never",
            "error": "",
            "last_attempt_at": None,
            "last_success_at": None,
            "next_due_at": None,
        }
    return {
        "enabled": row.enabled,
        "interval_minutes": row.interval_seconds // 60,
        "status": row.status,
        "error": row.error,
        **{
            name: getattr(row, name).isoformat() if getattr(row, name) else None
            for name in ("last_attempt_at", "last_success_at", "next_due_at")
        },
    }


def request_check(worker):
    with transaction.atomic():
        row = WorkerKeepalive.objects.select_for_update().filter(worker=worker).first()
        if row is None or not row.enabled:
            raise ValueError("Enable and configure keepalive first.")
        now = timezone.now()
        if not row.lease_expires_at or row.lease_expires_at <= now:
            row.next_due_at = now
            row.save(update_fields=["next_due_at"])
    return public_status(worker)


def _ssh_check(config):
    _validate(config)
    with tempfile.TemporaryDirectory(prefix="worker-keepalive-") as directory:
        os.chmod(directory, 0o700)
        known_hosts = Path(directory) / "known_hosts"
        known_hosts.write_text("keepalive-host " + config["known_host_key"] + "\n")
        ssh_config = Path(directory) / "config"
        ssh_config.write_text(
            f"Host keepalive-target\n HostName {config['host']}\n User {config['user']}\n Port {config['port']}\n"
            f" UserKnownHostsFile {known_hosts}\n GlobalKnownHostsFile /dev/null\n"
            " HostKeyAlias keepalive-host\n StrictHostKeyChecking yes\n BatchMode yes\n"
            " ConnectTimeout 10\n ConnectionAttempts 1\n ClearAllForwardings yes\n"
            " ForwardAgent no\n ForwardX11 no\n PermitLocalCommand no\n"
            " IdentityAgent none\n IdentityFile none\n IdentitiesOnly yes\n"
            " PasswordAuthentication no\n KbdInteractiveAuthentication no\n"
            " RequestTTY no\n LogLevel ERROR\n"
        )
        for path in (known_hosts, ssh_config):
            path.chmod(0o600)
        try:
            result = subprocess.run(
                ["ssh", "-F", str(ssh_config), "-T", "keepalive-target", "true"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "timeout", "SSH check timed out."
        except OSError:
            return "unavailable", "SSH client unavailable."
        if result.returncode == 0:
            return "success", ""
        # Never persist or log raw diagnostics: host/user may contain credentials.
        error = result.stderr.lower()
        if b"host key verification failed" in error or b"host identification has changed" in error:
            return "host_key_mismatch", "SSH host key verification failed."
        if b"permission denied" in error or b"authentication failed" in error:
            return "authentication_failed", "SSH authentication failed; verify credentials."
        return "failed", "SSH connectivity check failed."


def run_due_checks(*, limit=20):
    completed = 0
    for _ in range(limit):
        now = timezone.now()
        eligible = WorkerKeepalive.objects.filter(enabled=True, next_due_at__lte=now).filter(
            Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)
        )
        candidate = eligible.order_by("next_due_at").values_list("pk", flat=True).first()
        if candidate is None:
            break
        token = uuid.uuid4().hex
        # Compare-and-set works on both PostgreSQL and SQLite; no stale read claim.
        if not eligible.filter(pk=candidate).update(
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
            last_attempt_at=now,
            status="running",
            error="",
        ):
            continue
        row = WorkerKeepalive.objects.get(pk=candidate)
        try:
            status, error = _ssh_check(json.loads(decrypt(row.encrypted_config)))
        except Exception:
            status, error = "configuration_error", "Unable to use private SSH configuration."
        finished = timezone.now()
        values = dict(
            status=status,
            error=error,
            lease_token="",
            lease_expires_at=None,
            next_due_at=finished + timedelta(seconds=row.interval_seconds),
        )
        if status == "success":
            values["last_success_at"] = finished
        WorkerKeepalive.objects.filter(pk=candidate, lease_token=token).update(**values)
        completed += 1
    return completed

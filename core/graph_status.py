"""Read-only cache projection. The locked state marker remains authoritative."""

import fcntl
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

from .graph import VERSION

LABELS = {
    "disabled": "Not enabled",
    "missing": "Not built",
    "invalid": "Rebuild needed",
    "updating": "Building / updating",
    "ready": "Ready",
    "unavailable": "Status unavailable",
}


def status(repository):
    def result(state, **extra):
        return {"repository": repository, "state": state, "label": LABELS[state], **extra}

    if repository not in settings.GRAPH_REPOSITORIES:
        return result("disabled")
    root = settings.GRAPH_STATUS_DIR
    if not root or not Path(root).is_dir():
        return result("unavailable")
    cache = Path(root) / hashlib.sha256(repository.lower().encode()).hexdigest()
    try:
        if not cache.exists():
            return result("missing")
        with (cache / "lock").open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return result("updating")
            marker = cache / "state.json"
            if not marker.exists():
                return result("invalid" if (cache / "graph.db").exists() else "missing")
            with marker.open() as stream:
                data = json.loads(stream.read(8 * 1024 * 1024))
            if (
                not isinstance(data, dict)
                or data.get("version") != VERSION
                or not isinstance(data.get("hashes"), dict)
                or not isinstance(data.get("sha"), str)
                or not re.fullmatch(r"[a-f0-9]{40}", data["sha"])
                or not (cache / "graph.db").is_file()
                or not (cache / "source").is_dir()
            ):
                return result("invalid")
            summary = data.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            metrics = {
                key: value
                for key in ("indexed_files", "nodes", "edges")
                if type(value := summary.get(key)) is int and 0 <= value <= 100_000_000
            }
            return result(
                "ready",
                sha=data["sha"],
                updated_at=datetime.fromtimestamp(marker.stat().st_mtime, timezone.utc),
                **metrics,
            )
    except FileNotFoundError:
        return result("unavailable")
    except (ValueError, TypeError):
        return result("invalid")
    except OSError:
        return result("unavailable")


def cards(user):
    from .models import Repository

    repos = Repository.objects.filter(enabled=True)
    if not user.is_superuser:
        repos = repos.filter(organization__membership__user=user)
    return [
        status(name) for name in repos.order_by("full_name").values_list("full_name", flat=True)
    ]

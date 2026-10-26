"""PR-scoped CLI history handles. Called on the worker thread, never provider threads."""

import uuid

from .claims import fenced
from .models import CLISession
from .phase_checkpoints import digest
from .providers import review_contract


@fenced()
def prepare_sessions(job, provider, tasks):
    sessions = {}
    if provider.kind not in {"codex_cli", "claude_cli"}:
        return sessions
    for task in tasks:
        contract = digest(
            [provider.kind, provider.model, provider.effort, review_contract(task)[:2]]
        )
        row, _ = CLISession.objects.get_or_create(
            pr_id=job.pr_id, provider_id=provider.pk, task=task, defaults={"contract": contract}
        )
        if row.contract != contract:
            row.contract = contract
            row.storage_key = uuid.uuid4()
            row.session_id = None
            row.save()
        sessions[task] = {
            "key": str(row.storage_key),
            "id": str(row.session_id) if row.session_id else None,
        }
    return sessions


@fenced()
def save_session(job, sessions, data):
    if not isinstance(data, dict):
        return
    task = data.get("task")
    expected = sessions.get(task) if isinstance(task, str) else None
    if not expected:
        return
    previous = data.get("previous_key", data.get("key"))
    if previous != expected["key"]:
        return
    if "previous_key" in data and (not expected.get("id") or data.get("key") == previous):
        return
    try:
        session_id = uuid.UUID(data["id"])
        storage_key = uuid.UUID(data["key"])
        if str(session_id) != data["id"] or str(storage_key) != data["key"]:
            return
    except (ValueError, TypeError, AttributeError, KeyError):
        return
    from django.utils import timezone

    updated = CLISession.objects.filter(
        pr_id=job.pr_id, storage_key=expected["key"], task=task
    ).update(storage_key=storage_key, session_id=session_id, updated_at=timezone.now())
    if updated:
        expected.update(key=str(storage_key), id=str(session_id))

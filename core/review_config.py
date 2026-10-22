"""Editable model library and immutable per-job execution configuration."""

import hashlib
import json
import re

from django.db import transaction

from .models import Provider, ServiceSettings
from .review_agents import configured, validate_agents

FIELDS = ("id", "name", "kind", "model", "enabled", "effort", "credential_id")


def library():
    return list(Provider.objects.values(*FIELDS))


def version(rows, agents=None):
    payload = {"models": rows, "agents": configured() if agents is None else agents}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@transaction.atomic
def capture():
    ServiceSettings.objects.get_or_create(pk=1)
    configuration = ServiceSettings.objects.select_for_update().get(pk=1)
    rows = library()
    agents = configuration.review_agents
    return version(rows, agents), [row for row in rows if row["enabled"]], agents


def snapshot():
    revision, rows, _ = capture()
    return revision, rows


def providers_for_job(job):
    if not job.workflow_version:
        # Existing jobs keep their recorded plan; fill missing legacy connection metadata.
        _, current = snapshot()
        lookup = {row["id"]: row for row in library()}
        rows = (
            [{**lookup.get(row["id"], {}), **row} for row in job.provider_plan]
            if job.provider_plan
            else current
        )
        job.provider_plan = rows
        job.workflow_version = version(rows)
        type(job).objects.filter(pk=job.pk).update(
            provider_plan=rows, workflow_version=job.workflow_version
        )
    return [Provider(**row) for row in job.provider_plan]


class Conflict(ValueError):
    pass


@transaction.atomic
def save_library(data):
    configuration, _ = ServiceSettings.objects.get_or_create(pk=1)
    configuration = ServiceSettings.objects.select_for_update().get(pk=1)
    rows = list(Provider.objects.select_for_update())
    current = library()
    if data.get("version") != version(current):
        raise Conflict("Workflow changed in another tab. Reload before saving.")
    agents = validate_agents(data.get("agents", configuration.review_agents))
    incoming = data.get("models")
    if not isinstance(incoming, list) or not 1 <= len(incoming) <= 100:
        raise ValueError("Provide 1–100 model configurations.")
    existing = {row.pk: row for row in rows}
    seen = set()
    validated = []
    for index, row in enumerate(incoming):
        if not isinstance(row, dict):
            raise ValueError("Invalid model configuration.")
        pk = row.get("id")
        if pk is not None and (type(pk) is not int or pk not in existing or pk in seen):
            raise ValueError("Unknown or duplicate model.")
        seen.add(pk)
        provider = existing.get(pk) or Provider()
        for field in ("name", "kind", "model", "effort"):
            value = row.get(field, "")
            if not isinstance(value, str):
                raise ValueError("Model settings must be text.")
            setattr(provider, field, value.strip())
        if type(row.get("enabled")) is not bool:
            raise ValueError("Enabled must be true or false.")
        provider.enabled = row["enabled"]
        provider.order = index
        if not provider.name or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", provider.model
        ):
            raise ValueError(
                "Provide a name and valid model ID (letters, digits, dots, colons, underscores, hyphens)."
            )
        if provider.effort and (
            provider.kind != "codex_cli"
            or provider.effort not in {"low", "medium", "high", "xhigh"}
        ):
            raise ValueError(
                "Effort overrides are supported for Codex CLI only: low, medium, high, xhigh."
            )
        # Credential selection is intentionally left in the existing private credential UI.
        if pk is None and provider.kind not in {"codex_cli", "claude_cli"}:
            raise ValueError(
                "Add API connections through Administration with their credential first."
            )
        provider.full_clean()
        validated.append(provider)
    if set(existing) != seen - {None}:
        raise ValueError(
            "Keep existing models in the library; disable instead of deleting history."
        )
    if not any(row.enabled for row in validated):
        raise ValueError("The workflow needs at least one enabled model.")
    for row in validated:
        row.save()

    configuration.review_agents = agents
    configuration.save(update_fields=["review_agents"])

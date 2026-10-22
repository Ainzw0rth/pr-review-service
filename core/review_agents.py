"""Validated owner configuration and immutable specialist identities."""

import re
import uuid
from contextvars import ContextVar

BUILTINS = {"code": "Code", "security": "Security", "tests": "Tests"}
ACTIVE_AGENTS = ContextVar("review_agents", default=None)


def defaults():
    return [
        dict(key=key, name=name, instructions="", enabled=True) for key, name in BUILTINS.items()
    ]


def validate_agents(value):
    if not isinstance(value, list) or not 3 <= len(value) <= 12:
        raise ValueError("Keep the three built-in specialists; at most 12 specialists total.")
    result, seen = [], set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("Invalid specialist configuration.")
        key = row.get("key")
        if key is None:
            key = "agent_" + uuid.uuid4().hex[:12]
        if (
            not isinstance(key, str)
            or (key not in BUILTINS and not re.fullmatch(r"agent_[0-9a-f]{12}", key))
            or key in seen
        ):
            raise ValueError("Unknown or duplicate specialist key.")
        seen.add(key)
        name, instructions, enabled = row.get("name"), row.get("instructions"), row.get("enabled")
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 80
            or not isinstance(instructions, str)
            or len(instructions) > 8000
            or type(enabled) is not bool
        ):
            raise ValueError(
                "Specialists need a name (1–80 characters), instructions (at most 8000), and enabled boolean."
            )
        if key in BUILTINS:
            if name != BUILTINS[key] or instructions:
                raise ValueError("Built-in specialist names and policies cannot change.")
        elif not instructions.strip():
            raise ValueError("Custom specialist instructions must contain 1–8000 characters.")
        result.append(
            dict(key=key, name=name.strip(), instructions=instructions.strip(), enabled=enabled)
        )
    if not set(BUILTINS) <= seen or not 1 <= sum(row["enabled"] for row in result) <= 8:
        raise ValueError("Keep all built-ins and enable 1–8 specialists.")
    return result


def selected(agents=None):
    return [
        row
        for row in (agents if agents is not None else ACTIVE_AGENTS.get() or defaults())
        if row["enabled"]
    ]


def roles():
    return {row["key"]: ("Test" if row["key"] == "tests" else row["name"]) for row in selected()}


def configured():
    from .models import ServiceSettings

    row = ServiceSettings.objects.filter(pk=1).first()
    return row.review_agents if row else defaults()


def job_roles(job):
    return {row["key"]: row["name"] for row in selected(job.review_agents)}

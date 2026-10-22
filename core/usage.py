"""Provider-reported counters only; never infer quota percentages or dollar charges."""

import re

FIELDS = ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens")


def usage_counters(usage):
    if not isinstance(usage, dict):
        return {}
    values = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cached_input_tokens": usage.get(
            "cached_input_tokens", usage.get("cache_read_input_tokens")
        ),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
    }
    details = usage.get("input_tokens_details")
    if values["cached_input_tokens"] is None and isinstance(details, dict):
        values["cached_input_tokens"] = details.get("cached_tokens")
    return {k: v for k, v in values.items() if type(v) is int and 0 <= v <= 1_000_000_000}


def public_usage(diagnostics):
    phases = diagnostics.get("phase_usage")
    if isinstance(phases, list) and phases:
        passes = []
        for phase in phases[:10]:
            if not isinstance(phase, dict):
                continue
            name = phase.get("phase")
            if not isinstance(name, str):
                continue
            if name not in {
                "intent",
                "review",
                "code",
                "security",
                "tests",
                "synthesis",
            } and not (isinstance(name, str) and re.fullmatch(r"agent_[0-9a-f]{12}", name)):
                continue
            values = phase.get("passes")
            for index, value in enumerate(values[:2] if isinstance(values, list) else []):
                passes.append(
                    {**usage_counters(value), "phase": phase["phase"], "recovery_pass": index}
                )
        totals = {
            key: sum(p[key] for p in passes) if passes and all(key in p for p in passes) else None
            for key in FIELDS
        }
        return {
            "totals": totals,
            "passes": [
                {
                    **{key: p.get(key) for key in FIELDS},
                    "phase": p["phase"],
                    "recovery_pass": p["recovery_pass"],
                }
                for p in passes
            ],
            "session_count": len(passes),
        }
    passes = diagnostics.get("usage_passes")
    if not isinstance(passes, list) or not passes:
        passes = [diagnostics]
    passes = [usage_counters(p) for p in passes[:2] if isinstance(p, dict)]
    # Partial counters are not complete attempt totals. Keep unknown distinct from zero.
    totals = {
        key: sum(p[key] for p in passes) if passes and all(key in p for p in passes) else None
        for key in FIELDS
    }
    return {
        "totals": totals,
        "passes": [{key: p.get(key) for key in FIELDS} for p in passes],
        "session_count": len(passes),
    }

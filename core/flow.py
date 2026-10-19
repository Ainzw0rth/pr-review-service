"""Read-only progress projection. Never turn planned work into claimed work."""

from .models import Provider
from .providers import REFUSAL_CATEGORIES, failure_description
from .usage import public_usage
from .workflow_schema import LEGACY_STEPS


def next_action(job, attempt, attempts, plan):
    """Describe recorded routing first; never promise a fallback after a stopped job."""
    if attempt.status in ("running", "success"):
        return ""
    later = next(
        (item for item in attempts if item.pk > attempt.pk and item.status != "skipped"), None
    )
    if later:
        return f"Continued with {later.provider_name}. See its attempt below."
    if job.status in ("cancelled", "superseded"):
        return "Run cancelled or superseded. No further model calls for this attempt."
    if job.status == "quota" and attempt.status == "quota":
        if attempt.provider and attempt.provider.kind.endswith("_cli"):
            return "No usable account remains. Waiting for the next availability check."
        return "Waiting to retry this provider; API quota does not switch models."
    if attempt.status == "skipped" and job.status == "quota":
        return "Account skipped without a model call. Waiting for quota availability."
    if job.status == "running" and attempt.status in (
        "refused",
        "unavailable",
        "incomplete",
        "failed",
        "quota",
        "skipped",
    ):
        index = next(
            (
                i
                for i, p in enumerate(plan)
                if p["id"] == attempt.provider_id and p["model"] == attempt.model
            ),
            None,
        )
        if index is not None and index + 1 < len(plan):
            return f"Next configured model: {plan[index + 1]['name']} (not started yet)."
        return "No remaining model in this route; administrator attention required."
    if attempt.status in ("refused", "unavailable", "incomplete", "failed"):
        return "Run stopped. No further model call recorded; inspect the operational note before retrying."
    return "Automatic retry paused. Resolve the cause, then the owner can request a fresh review."


def snapshot(job, events):
    all_events = events
    starts = [i for i, event in enumerate(events) if event.code == "starting"]
    if starts:
        events = events[starts[-1] :]
    if any(event.code == "delivery_resume" for event in events):
        events = all_events
    codes = {event.code for event in events}
    planned = job.workflow_steps if job.workflow_revision else None

    def includes(key, legacy):
        return key in planned if planned is not None else legacy

    def count(code):
        return next((event.count for event in reversed(events) if event.code == code), None)

    def node(key, title, start, done, *, unit=None, detail="", count_code=None):
        entered = next((e for e in events if e.code in start), None)
        finished = next((e for e in events if e.code in done), None)
        state = "done" if finished else "running" if entered else "pending"
        if state == "running" and job.status != "running":
            state = "cancelled" if job.status in ("cancelled", "superseded") else "attention"
        value = count(count_code or key) if unit else None
        return {
            "id": key,
            "title": title,
            "state": state,
            "count": value,
            "unit": unit,
            "detail": detail,
            "started_at": entered.created_at if entered else None,
            "finished_at": finished.created_at if finished else None,
        }

    context_done = {"context_ready"}
    stages = [
        node(
            "files",
            "Changed files",
            {"context", "files"},
            {"threads", "related", "docs_started", "docs"} | context_done,
            unit="files",
            detail="Commit-pinned source & diffs",
        ),
        node(
            "discussion",
            "Discussions",
            {"threads", "related"},
            {"docs_started", "docs"} | context_done,
            detail="Threads, linked issues & PRs",
        ),
        node(
            "docs",
            "Documentation",
            {"docs_started", "docs"},
            context_done | {"graph_started"},
            unit="documents",
            detail="Relevant docs & supporting source",
        ),
        node(
            "checks",
            "Required CI",
            {"checks"},
            {"checks_passed", "checks_blocked"},
            detail="Evidence for this commit",
        ),
    ]
    if includes("graph", "graph_started" in codes):
        builds = [event for event in events if event.code in {"graph_full", "graph_incremental"}]
        graph_detail = "Build or update base/head graphs; load impacted source"
        if builds:
            modes = (
                "Full build + incremental update"
                if {e.code for e in builds} == {"graph_full", "graph_incremental"}
                else "Full build"
                if builds[0].code == "graph_full"
                else "Incremental update"
            )
            graph_detail = (
                f"{modes} · {sum(e.count or 0 for e in builds)} files parsed across snapshots"
            )
            if count("graph_indexed") is not None:
                graph_detail += f" · {count('graph_indexed')} indexed at head"
            if count("graph_impacted") is not None:
                graph_detail += f" · {count('graph_impacted')} impacted paths"
        elif "graph_ready" in codes:
            graph_detail = "Base/head graphs updated; loading impacted source"
        elif "graph_started" in codes:
            graph_detail = "Fetching snapshots, updating dependency graphs and selecting impact"
        stages.insert(
            3,
            node(
                "graph",
                "Dependency graph",
                {"graph_started"},
                context_done,
                detail=graph_detail,
            ),
        )
    if includes("intent", False):
        # Each provider runs its own phase; older success cannot finish a later start.
        intent_events = [
            event for event in events if event.code in {"intent_started", "intent_ready"}
        ]
        intent = node(
            "intent",
            "Grounded intent",
            {"intent_started"},
            {"intent_ready"},
            detail="Requirements, inferred intent & unknowns from fetched evidence",
        )
        latest_start = next(
            (event for event in reversed(intent_events) if event.code == "intent_started"), None
        )
        if latest_start:
            intent["started_at"] = latest_start.created_at
        if intent_events and intent_events[-1].code == "intent_ready":
            intent["finished_at"] = intent_events[-1].created_at
        if intent_events and intent_events[-1].code == "intent_started":
            intent["state"] = (
                "running"
                if job.status == "running"
                else "cancelled"
                if job.status in {"cancelled", "superseded"}
                else "attention"
            )
            intent["started_at"] = intent_events[-1].created_at
            intent["finished_at"] = None
        stages.insert(next(i for i, stage in enumerate(stages) if stage["id"] == "checks"), intent)
    latest_ci = next(
        (
            event
            for event in reversed(events)
            if event.code in {"checks_waiting", "checks_passed", "checks_blocked"}
        ),
        None,
    )
    if latest_ci:
        stages[-1]["finished_at"] = (
            latest_ci.created_at if latest_ci.code != "checks_waiting" else None
        )
    if latest_ci and latest_ci.code == "checks_blocked":
        stages[-1]["state"] = "blocked"
        stages[-1]["detail"] = "Missing, pending or unsuccessful"
    elif latest_ci and latest_ci.code == "checks_waiting":
        stages[-1]["detail"] = "Waiting for CI before AI review"
    if includes("tests", bool(codes & {"tests", "tests_passed", "tests_failed"})):
        tests = node(
            "tests",
            "Isolated tests",
            {"tests"},
            {"tests_passed", "tests_failed"},
            detail="Disposable test environment",
        )
        if "tests_failed" in codes:
            tests["state"] = "blocked"
        stages.append(tests)
    stages.append(
        node(
            "publication",
            "GitHub",
            {"publishing"},
            {"published"},
            detail="Comments, threads & workflow labels",
        )
    )
    for key, title, prefix in [
        ("pr_summary", "PR summary", "summary"),
        ("inline_findings", "Inline findings", "inline"),
        ("discussion_replies", "Discussion replies", "discussion"),
        ("related_pr_comments", "Related-PR comments", "related_pr"),
        ("related_issue_comments", "Related-issue comments", "related_issue"),
        ("labels", "Workflow labels", "labels"),
    ]:
        if not includes(key, False):
            continue
        done_code, unused_code = f"{prefix}_done", f"{prefix}_unused"
        stage = node(
            key,
            title,
            {f"{prefix}_started"},
            {done_code, unused_code},
            unit="confirmed writes",
            count_code=done_code,
            detail="Recorded delivery confirmations; content stays on GitHub",
        )
        confirmed = [event for event in events if event.code in {done_code, unused_code}]
        if confirmed:
            # Confirmed receipts survive retries; starts alone never regress them.
            latest = confirmed[-1]
            stage["state"] = "not_needed" if latest.code == unused_code else "done"
            stage["count"] = latest.count
            stage["finished_at"] = latest.created_at
        stages.append(stage)
    if includes("sheet", bool(codes & {"sheet", "sheet_attention"})):
        sheet = node("sheet", "Timeline", {"sheet"}, set(), detail="Optional issue-status sync")
        if "sheet_attention" in codes:
            sheet["state"] = "attention"
        elif "sheet" in codes and "ended" in codes and job.status == "completed":
            sheet["state"] = "done"
        stages.append(sheet)

    by_id = {stage["id"]: stage for stage in stages}
    order = planned if planned is not None else LEGACY_STEPS
    stages = [by_id[key] for key in order if key in by_id]

    attempts = list(job.model_attempts.order_by("id"))
    legacy = not attempts and "model" in {e.code for e in all_events}
    plan = job.provider_plan
    if not plan and not legacy and not job.workflow_version:
        plan = list(Provider.objects.filter(enabled=True).values("id", "name", "model", "kind"))
    groups = []
    for provider in plan:
        groups.append(
            {
                "provider_id": provider["id"],
                "title": provider["name"],
                "model": provider["model"],
                "state": "pending",
                "backend": provider.get("kind", ""),
                "attempts": [],
            }
        )
    for attempt in attempts:
        group = next(
            (
                g
                for g in groups
                if g["provider_id"] == attempt.provider_id and g["model"] == attempt.model
            ),
            None,
        )
        if group is None:
            group = {
                "provider_id": attempt.provider_id,
                "title": attempt.provider_name,
                "model": attempt.model,
                "state": "pending",
                "attempts": [],
            }
            groups.append(group)
        state = attempt.status
        if state == "running" and job.status in ("cancelled", "superseded"):
            state = "cancelling"
        category = attempt.diagnostics.get("refusal_category")
        if not isinstance(category, str) or category not in REFUSAL_CATEGORIES:
            category = None
        transport = attempt.diagnostics.get("transport", "")
        if transport in {"claude_cli", "codex_cli"}:
            group["backend"] = transport
        event_count = attempt.diagnostics.get("events_received")
        if type(event_count) is not int or not 0 <= event_count <= 1000000:
            event_count = None
        evidence_read = attempt.diagnostics.get("evidence_read")
        evidence_total = attempt.diagnostics.get("evidence_total")
        if not (
            type(evidence_read) is int
            and type(evidence_total) is int
            and 0 <= evidence_read <= evidence_total <= 1_000_000
        ):
            evidence_read = evidence_total = None
        phase = attempt.diagnostics.get("phase")
        if not isinstance(phase, str) or phase not in {"intent", "review"}:
            phase = None
        group["attempts"].append(
            {
                "id": attempt.pk,
                "state": state,
                "phase": phase,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
                "category": category,
                "events_received": event_count,
                "recovery_pass": 1 if attempt.diagnostics.get("recovery_pass") == 1 else 0,
                "usage": None if state == "skipped" else public_usage(attempt.diagnostics),
                "evidence_read": evidence_read,
                "evidence_total": evidence_total,
                "failure_reason": failure_description(attempt.diagnostics, state),
                "next_action": next_action(job, attempt, attempts, plan),
                "retry_at": job.next_retry_at
                if state == "quota" and job.status == "quota" and attempt == attempts[-1]
                else None,
            }
        )
        group["state"] = state
    if legacy and job.provider_id:
        state = (
            "success"
            if "result" in codes
            else "refused"
            if "refus" in job.reason.lower()
            else "attention"
        )
        groups = [
            {
                "provider_id": job.provider_id,
                "title": job.provider.name,
                "model": job.provider.model,
                "state": state,
                "attempts": [],
            }
        ]
    for group in groups:
        if group["state"] == "pending" and job.status in (
            "completed",
            "attention",
            "failed",
            "cancelled",
            "superseded",
        ):
            group["state"] = "unused"
    model_detail = (
        "Saved analysis reused for delivery; no additional model call."
        if "delivery_resume" in codes
        else "Each model extracts intent first, then reviews code after the intent summary is validated."
        if includes("intent", False)
        else "Each attempt shows its recorded failure cause and what happened next."
    )
    if not attempts and not legacy and "context_failed" in codes:
        model_detail = "No model was called. Evidence gathering failed before AI review; see the operational note."
    return {
        "revision": job.workflow_revision or None,
        "stages": stages,
        "models": groups,
        "legacy": legacy,
        "findings": count("findings"),
        "model_detail": model_detail,
    }

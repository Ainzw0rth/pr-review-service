"""Read-only progress projection. Never turn planned work into claimed work."""

from .models import Provider


def snapshot(job, events):
    all_events = events
    starts = [i for i, event in enumerate(events) if event.code == "starting"]
    if starts:
        events = events[starts[-1] :]
    codes = {event.code for event in events}

    def count(code):
        return next((event.count for event in reversed(events) if event.code == code), None)

    def node(key, title, start, done, *, unit=None, detail=""):
        entered = next((e for e in events if e.code in start), None)
        finished = next((e for e in events if e.code in done), None)
        state = "done" if finished else "running" if entered else "pending"
        if state == "running" and job.status != "running":
            state = "cancelled" if job.status in ("cancelled", "superseded") else "attention"
        value = count(key) if unit else None
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
            context_done,
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
    if "checks_blocked" in codes:
        stages[-1]["state"] = "blocked"
        stages[-1]["detail"] = "Missing, pending or unsuccessful"
    if job.pr.repository.test_command or "tests" in codes:
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
    if job.pr.repository.sheet_config:
        sheet = node("sheet", "Timeline", {"sheet"}, set(), detail="Optional issue-status sync")
        if "sheet_attention" in codes:
            sheet["state"] = "attention"
        elif "sheet" in codes and "ended" in codes and job.status == "completed":
            sheet["state"] = "done"
        stages.append(sheet)

    attempts = list(job.model_attempts.order_by("id"))
    legacy = not attempts and "model" in {e.code for e in all_events}
    plan = job.provider_plan
    if not plan and not legacy:
        plan = list(Provider.objects.filter(enabled=True).values("id", "name", "model"))
    groups = []
    for provider in plan:
        groups.append(
            {
                "provider_id": provider["id"],
                "title": provider["name"],
                "model": provider["model"],
                "state": "pending",
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
        if category not in (
            "cyber",
            "bio",
            "frontier_llm",
            "reasoning_extraction",
            "general_harms",
        ):
            category = None
        group["attempts"].append(
            {
                "id": attempt.pk,
                "state": state,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
                "category": category,
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
    return {"stages": stages, "models": groups, "legacy": legacy, "findings": count("findings")}

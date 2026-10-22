"""Bounded specialist fan-out; provider threads never touch Django or public state."""

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy

from .capacity import ActiveBudget, CapacityError, cli_limit
from .providers import ProviderError, ReviewCancelled, resolve_capacity_scope
from .review_team import ROLES, finalize, validate_report

PHASE_TIMEOUT_SECONDS = 1800


def run_team(
    provider,
    context,
    cancelled,
    *,
    call,
    on_response=None,
    on_activity=None,
    progress=None,
    checkpoints=None,
):
    """Return a publishable v2-shaped result and a private adjudication audit.

    Fan out at most three ways when account concurrency permits it. CLI accounts
    default to serial execution. All stateful callbacks run on this thread.
    """
    phases = list(ROLES)
    reports = {}
    diagnostics = {}

    def response(phase, data):
        diagnostics[phase] = data
        if on_response:
            on_response(phase, data)

    def activity(phase, code, text):
        if on_activity:
            on_activity(phase, code, text)

    def emit(phase, state):
        if progress:
            progress(f"{phase}_{state}")

    def check():
        if cancelled():
            raise ReviewCancelled()
        return False

    def accept(phase, value):
        reports[phase] = validate_report(value, context)
        if checkpoints is not None:
            check()
            checkpoints.save(phase, reports[phase], provider, context["intent"])
        emit(phase, "ready" if reports[phase]["complete"] else "incomplete")

    if checkpoints is not None:
        for phase in phases:
            check()
            saved = checkpoints.load(phase, context["intent"])
            if saved is not None:
                reports[phase] = saved
                emit(phase, "started")
                if progress:
                    progress("phase_reused")
                emit(phase, "ready")
    pending = [phase for phase in phases if phase not in reports]

    def serial(phase, evidence):
        budget = ActiveBudget(PHASE_TIMEOUT_SECONDS)
        check()
        emit(phase, "started")
        response(phase, {})
        value = call(
            provider,
            deepcopy(evidence),
            check,
            budget=budget,
            on_capacity=lambda waiting: emit(phase, "waiting" if waiting else "resumed"),
            task=f"review_{phase}",
            on_response=lambda data: response(phase, data),
            on_activity=lambda code, text: activity(phase, code, text),
        )
        budget.check()
        check()
        return value

    if provider.kind.endswith("_cli") and cli_limit() == 1:
        for phase in pending:
            accept(phase, serial(phase, context))
    elif pending:
        stop = threading.Event()
        events = queue.Queue(maxsize=100)
        terminal_errors = []
        latest_reports = {}
        completed_results = {}
        state_lock = threading.Lock()
        budgets = {phase: ActiveBudget(PHASE_TIMEOUT_SECONDS) for phase in pending}

        def send(event):
            # Backpressure bounds diagnostic buffering. Cancellation releases producers.
            while not stop.is_set():
                try:
                    events.put(event, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def report(phase, data):
            snapshot = deepcopy(data)
            with state_lock:
                latest_reports[phase] = snapshot
            send(("response", phase, snapshot))

        def run(phase):
            if stop.is_set():
                return
            try:
                value = call(
                    provider,
                    deepcopy(context),
                    stop.is_set,
                    budget=budgets[phase],
                    on_capacity=lambda waiting: send(("capacity", phase, waiting)),
                    task=f"review_{phase}",
                    on_response=lambda data: report(phase, data),
                    on_activity=lambda code, text: send(("activity", phase, (code, text))),
                )
                with state_lock:
                    completed_results[phase] = value
                send(("result", phase, value))
            except Exception as exc:
                with state_lock:
                    terminal_errors.append((phase, exc))
                send(("error", phase, exc))

        # Materialize credentials on the controller before the model instance is shared.
        # Provider.credential is a Django descriptor; lazy access in threads would query DB.
        if getattr(provider, "credential_id", None):
            provider.credential
            provider._capacity_scope = resolve_capacity_scope(provider)
        try:
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="pr-review") as pool:
                try:
                    for phase in pending:
                        check()
                        emit(phase, "started")
                        response(phase, {})
                        pool.submit(copy_context().run, run, phase)
                    failed = set()
                    while len(reports) + len(failed) < len(phases):
                        check()
                        for name, budget in budgets.items():
                            if name not in reports and name not in failed:
                                budget.check()
                        try:
                            kind, phase, data = events.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        if kind == "error":
                            response(phase, diagnostics.get(phase, {}))
                            if (
                                checkpoints is not None
                                and isinstance(data, ProviderError)
                                and data.kind not in {"auth", "quota"}
                            ):
                                failed.add(phase)
                                continue
                            raise data
                        if kind == "response":
                            response(phase, data)
                        elif kind == "capacity":
                            emit(phase, "waiting" if data else "resumed")
                        elif kind == "activity":
                            activity(phase, *data)
                        elif kind == "result":
                            try:
                                accept(phase, data)
                            except ProviderError as exc:
                                if checkpoints is None:
                                    raise
                                reports.pop(phase, None)
                                terminal_errors.append((phase, exc))
                                failed.add(phase)
                    if terminal_errors:
                        phase, error = terminal_errors[0]
                        raise error
                finally:
                    stop.set()
        except Exception as caught:
            capacity_error = next(
                (error for _, error in terminal_errors if isinstance(error, CapacityError)), None
            )
            if capacity_error:
                raise capacity_error from None
            if not isinstance(caught, ProviderError):
                raise
            # Joined peers may have completed just before cancellation. Persist only
            # fully validated results, on the controller, under execution fencing.
            if checkpoints is not None:
                for name, value in completed_results.items():
                    if name not in reports:
                        try:
                            accept(name, value)
                        except ProviderError:
                            pass
            # Threads are joined now. Preserve observed account errors even if a peer
            # failed first. The worker applies each transport's quota/fallback policy.
            for name in phases:
                if name in latest_reports:
                    response(name, latest_reports[name])
            for fatal_kind in ("auth", "quota"):
                fatal = next(
                    (
                        (p, e)
                        for p, e in terminal_errors
                        if isinstance(e, ProviderError) and e.kind == fatal_kind
                    ),
                    None,
                )
                if fatal:
                    name, error = fatal
                    response(name, latest_reports.get(name, {}))
                    raise error from None
            response(phase, latest_reports.get(phase, diagnostics.get(phase, {})))
            raise
    # Stable report order makes synthesis input, audits and provider fallbacks reproducible.
    reports = {phase: reports[phase] for phase in phases}
    evidence = {**context, "reviewer_reports": reports}
    value = serial("synthesis", evidence)
    result = finalize(value, reports, context)
    emit("synthesis", "ready" if result["complete"] else "incomplete")
    audit = {"reports": reports, "assessments": value["assessments"]}
    if checkpoints is not None:
        audit["phase_sources"] = dict(checkpoints.sources)
    return result, audit

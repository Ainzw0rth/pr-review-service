"""Bounded specialist fan-out; provider threads never touch Django or public state."""

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from .providers import ProviderError, ReviewCancelled
from .review_team import ROLES, finalize, validate_report

PHASE_TIMEOUT_SECONDS = 900


def run_team(
    provider,
    context,
    cancelled,
    *,
    call,
    on_response=None,
    on_activity=None,
    progress=None,
):
    """Return a publishable v2-shaped result and a private adjudication audit.

    API calls fan out at most three ways. CLI calls share a writable subscription
    auth volume, so run sequentially. All stateful callbacks run on this thread.
    """
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

    def check(deadline=None):
        if deadline is not None and time.monotonic() >= deadline:
            raise ProviderError("invalid", detail="review_timeout")
        if cancelled():
            raise ReviewCancelled()
        return False

    def serial(phase, evidence):
        deadline = time.monotonic() + PHASE_TIMEOUT_SECONDS
        check(deadline)
        emit(phase, "started")
        response(phase, {})
        value = call(
            provider,
            deepcopy(evidence),
            lambda: check(deadline),
            task=f"review_{phase}",
            on_response=lambda data: response(phase, data),
            on_activity=lambda code, text: activity(phase, code, text),
        )
        check(deadline)
        return value

    if provider.kind.endswith("_cli"):
        for phase in ROLES:
            reports[phase] = validate_report(serial(phase, context), context)
            emit(phase, "ready")
    else:
        stop = threading.Event()
        events = queue.Queue(maxsize=100)
        terminal_errors = []
        latest_reports = {}
        state_lock = threading.Lock()
        deadline = time.monotonic() + PHASE_TIMEOUT_SECONDS

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
            try:
                value = call(
                    provider,
                    deepcopy(context),
                    stop.is_set,
                    task=f"review_{phase}",
                    on_response=lambda data: report(phase, data),
                    on_activity=lambda code, text: send(("activity", phase, (code, text))),
                )
                send(("result", phase, value))
            except Exception as exc:
                with state_lock:
                    terminal_errors.append((phase, exc))
                send(("error", phase, exc))

        # Materialize credentials on the controller before the model instance is shared.
        # Provider.credential is a Django descriptor; lazy access in threads would query DB.
        if getattr(provider, "credential_id", None):
            provider.credential
        try:
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="pr-review") as pool:
                try:
                    for phase in ROLES:
                        check(deadline)
                        emit(phase, "started")
                        response(phase, {})
                        pool.submit(run, phase)
                    while len(reports) < len(ROLES):
                        check(deadline)
                        try:
                            kind, phase, data = events.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        if kind == "error":
                            response(phase, diagnostics.get(phase, {}))
                            raise data
                        if kind == "response":
                            response(phase, data)
                        elif kind == "activity":
                            activity(phase, *data)
                        elif kind == "result":
                            reports[phase] = validate_report(data, context)
                            emit(phase, "ready")
                finally:
                    stop.set()
        except ProviderError:
            # Threads are joined now. Preserve observed account errors even if a peer
            # failed first: API quota must wait, auth must stop, neither may fall forward.
            for name in ROLES:
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
            raise
    # Stable report order makes synthesis input, audits and provider fallbacks reproducible.
    reports = {phase: reports[phase] for phase in ROLES}
    evidence = {**context, "reviewer_reports": reports}
    value = serial("synthesis", evidence)
    result = finalize(value, reports, context)
    emit("synthesis", "ready")
    return result, {"reports": reports, "assessments": value["assessments"]}

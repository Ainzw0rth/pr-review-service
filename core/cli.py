"""Subscription CLIs in disposable containers; raw events never enter the dashboard."""

import json
import os
import re
import selectors
import subprocess
import tempfile
import time

from django.conf import settings

from .cli_queue import admit_cli
from .providers import (
    CLAUDE_TERMINAL_REASONS,
    MAX_RESPONSE_BYTES,
    REFUSAL_CATEGORIES,
    ProviderError,
    ReviewCancelled,
    review_contract,
    validation_diagnostics,
)


class CLIStream:
    def __init__(self, kind, context=None, on_activity=None, *, task="review"):
        from .transcript import Transcript

        self.transcript = Transcript(kind, context or {}, on_activity)
        self.kind = kind
        self.validator = review_contract(task)[2]
        self.diagnostics = {"transport": kind, "events_received": 0, "last_event": "starting"}
        self.output = None
        self.complete = False
        self.error = None
        self.coverage = False
        self.missing_evidence = []
        self.session_id = None
        self.evidence_ids = {
            identifier
            for section, value in (context or {}).items()
            for identifier in (
                [f"{section}_{index}" for index in range(len(value))]
                if isinstance(value, list)
                else [section]
            )
        }

    def line(self, value):
        if len(value) > MAX_RESPONSE_BYTES:
            raise ProviderError("invalid", detail="response_too_large")
        try:
            event = json.loads(value)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ProviderError("invalid", detail="cli_protocol") from None
        self.feed(event)

    def fail(self, data):
        if not isinstance(data, dict):
            data = {}
        code = data.get("code") or data.get("type")
        message = data.get("message", "")
        message = message.lower()[:64000] if isinstance(message, str) else ""
        if code in ("cli_stream_malformed", "cli_stream_truncated", "cli_stream_too_large"):
            self.diagnostics["protocol_error"] = code
            error = ProviderError(
                "invalid",
                detail="response_too_large" if code == "cli_stream_too_large" else "cli_protocol",
            )
        elif code in ("rate_limit", "rate_limit_error", "usage_limit_reached"):
            error = ProviderError("quota", detail="cli_quota")
        elif code == "auth_refresh_unavailable":
            error = ProviderError("unavailable")
        elif code == "auth_missing":
            error = ProviderError("auth", detail="cli_auth")
        elif code in ("content_policy_violation", "content_filter", "refusal"):
            error = ProviderError("attention")
            self.diagnostics.update(refusal_detected=True, refusal_signal=code)
        elif any(
            s in message
            for s in (
                "usage limit",
                "rate limit",
                "quota exceeded",
                "credit balance",
                "insufficient credits",
            )
        ):
            error = ProviderError("quota", detail="cli_quota")
        elif any(
            s in message
            for s in (
                "not logged in",
                "authentication",
                "login required",
                "refresh token",
                "unauthorized",
                "please run /login",
            )
        ):
            error = ProviderError("auth", detail="cli_auth")
        elif any(
            s in message
            for s in (
                "model not found",
                "model is not supported",
                "model does not exist",
                "not available for",
            )
        ):
            error = ProviderError("invalid", detail="model_not_found")
        elif (
            "maximum length" in message
            or "context window" in message
            or "input_too_large" in message
        ):
            error = ProviderError("invalid", detail="context_too_large")
        elif any(
            s in message
            for s in (
                "connection",
                "timed out",
                "overloaded",
                "503",
                "502",
                "routing discovery failed",
                "reconnecting",
            )
        ):
            error = ProviderError("unavailable")
        else:
            error = ProviderError("invalid", detail="cli_protocol")
        # A trailing generic process error must not erase a specific provider error.
        if self.error is None or self.error.detail == "cli_protocol":
            self.error = error
        self.diagnostics.update(
            last_event="failed", failure_kind=self.error.kind, failure_detail=self.error.detail
        )

    def feed(self, event):
        if not isinstance(event, dict):
            raise ProviderError("invalid", detail="cli_protocol")
        self.transcript.feed(event)
        self.diagnostics["events_received"] += 1
        kind = event.get("type")
        if (self.kind == "codex_cli" and kind == "thread.started") or (
            self.kind == "claude_cli" and kind == "system" and event.get("subtype") == "init"
        ):
            identifier = event.get("thread_id" if self.kind == "codex_cli" else "session_id")
            if isinstance(identifier, str) and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", identifier
            ):
                self.session_id = identifier
        elif kind == "runner_session" and type(event.get("resumed")) is bool:
            self.diagnostics["cli_session_resumed"] = event["resumed"]
        elif kind == "runner_exit":
            code, signal = event.get("code"), event.get("signal")
            if type(code) is int and 0 <= code <= 255:
                self.diagnostics["cli_exit_code"] = code
            if signal in ("SIGTERM", "SIGKILL", "SIGINT", "SIGABRT", "SIGSEGV", "SIGPIPE"):
                self.diagnostics["cli_exit_signal"] = signal
        elif kind == "runner_coverage":
            read, total = event.get("read"), event.get("total")
            if type(read) is int and type(total) is int and 0 <= read <= total <= 1_000_000:
                self.coverage = read == total
                self.diagnostics.update(evidence_read=read, evidence_total=total)
                missing = event.get("missing", [])
                if (
                    not isinstance(missing, list)
                    or len(missing) > total
                    or any(
                        not isinstance(item, str) or item not in self.evidence_ids
                        for item in missing
                    )
                    or len(set(missing)) != len(missing)
                ):
                    raise ProviderError("invalid", detail="cli_protocol")
                self.missing_evidence = missing
            else:
                raise ProviderError("invalid", detail="cli_protocol")
        elif (
            kind == "error"
            and isinstance(event.get("message"), str)
            and event["message"].startswith("Reconnecting...")
        ):
            self.diagnostics["last_event"] = "reconnecting"
        elif kind in ("error", "runner_error", "turn.failed"):
            self.fail(event.get("error", event))
        elif kind == "assistant" and event.get("error") in ("rate_limit", "rate_limit_error"):
            self.fail({"code": event["error"]})
        elif kind == "stream_event":
            body = event.get("event", {})
            delta = body.get("delta", {}) if isinstance(body, dict) else {}
            if isinstance(delta, dict) and delta.get("stop_reason") == "refusal":
                self.error = ProviderError("attention")
                self.diagnostics.update(
                    refusal_detected=True,
                    last_event="declined",
                    refusal_signal="stop_reason_refusal",
                )
                details = delta.get("stop_details")
                category = details.get("category") if isinstance(details, dict) else None
                if isinstance(category, str) and category in REFUSAL_CATEGORIES:
                    self.diagnostics["refusal_category"] = category
            elif isinstance(body, dict) and body.get("type") == "content_block_delta":
                self.diagnostics["last_event"] = "receiving"
        elif kind in ("item.started", "item.completed"):
            item = event.get("item", {})
            if not isinstance(item, dict):
                raise ProviderError("invalid", detail="cli_protocol")
            if (
                item.get("type") == "mcp_tool_call"
                and item.get("server") == "review_context"
                and item.get("tool") == "read_evidence"
            ):
                self.diagnostics["last_event"] = "reading_evidence"
            elif item.get("type") in (
                "command_execution",
                "mcp_tool_call",
                "web_search",
                "file_change",
            ):
                self.error = ProviderError("invalid", detail="cli_tools")
            elif item.get("type") == "agent_message" and kind == "item.completed":
                self.output = item.get("text")
                self.diagnostics["last_event"] = "receiving"
        elif kind == "turn.completed":
            self.complete = True
            self.diagnostics["last_event"] = "completed"
            self.usage(event.get("usage"))
        elif kind == "result" and self.kind == "claude_cli":
            self.usage(event.get("usage"))
            subtype = event.get("subtype")
            if isinstance(subtype, str) and subtype in CLAUDE_TERMINAL_REASONS:
                self.diagnostics["cli_terminal_subtype"] = subtype
            if event.get("is_error") or event.get("subtype") != "success":
                self.fail({"message": event.get("result", ""), "code": event.get("subtype")})
            else:
                self.output = event.get("structured_output")
                self.complete = True
                self.diagnostics["last_event"] = "completed"

    def usage(self, usage):
        from .usage import usage_counters

        self.diagnostics.update(usage_counters(usage))

    def result(self, exit_code):
        if self.error:
            raise self.error
        if exit_code or not self.complete:
            raise ProviderError("invalid", detail="cli_protocol")
        try:
            value = json.loads(self.output) if isinstance(self.output, str) else self.output
            return self.validator(value)
        except ProviderError as exc:
            details = validation_diagnostics(exc)
            self.diagnostics.update(details)
            raise ProviderError("invalid", detail="output_invalid", **details) from None
        except (ValueError, TypeError, RecursionError):
            self.diagnostics["validation_error"] = "invalid_json"
            raise ProviderError(
                "invalid", detail="output_invalid", validation_error="invalid_json"
            ) from None


def container_arguments(kind, name, image, volume):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ProviderError("invalid", detail="cli_setup")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", volume):
        raise ProviderError("invalid", detail="cli_setup")
    return [
        "create",
        "--name",
        name,
        "--pull=never",
        "--label=auto-reviewer.cli=true",
        "--user=1000:1000",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=128",
        "--memory=2g",
        "--memory-swap=2g",
        "--cpus=2",
        "--ulimit=nofile=1024:1024",
        "--log-driver=none",
        "--ipc=private",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        "--mount",
        f"type=volume,source={volume},target=/auth",
        "--interactive",
        image,
    ]


def review_cli(
    provider,
    context,
    cancelled,
    *,
    on_response=None,
    on_activity=None,
    task="review",
    on_capacity=None,
    budget=None,
):
    if task == "intent":
        # Keep citation/tool IDs identical here, in the API envelope and in the later review.
        context = {
            "head_sha": context.get("head_sha"),
            "complete": context.get("complete", False),
            **{entry["id"]: entry["value"] for entry in context.get("evidence", [])},
        }
    recovery = None
    usage_passes = []
    for recovery_pass in range(2):
        usage_passes.append({})

        def report(data):
            from .usage import usage_counters

            usage_passes[recovery_pass].update(usage_counters(data))
            if on_response:
                on_response(
                    {
                        **data,
                        "recovery_pass": recovery_pass,
                        "usage_passes": [dict(p) for p in usage_passes],
                    }
                )

        try:
            return _review_cli_once(
                provider,
                context,
                cancelled,
                on_response=report,
                on_activity=on_activity,
                recovery=recovery,
                on_capacity=on_capacity,
                budget=budget,
                **({"task": task} if task != "review" else {}),
            )
        except ProviderError as exc:
            if exc.detail != "cli_coverage" or recovery_pass == 1:
                raise
            if cancelled():
                raise ReviewCancelled() from None
            recovery = getattr(exc, "missing_evidence", [])
            if on_activity:
                on_activity(
                    "cli_output_status",
                    "Evidence incomplete. Starting one recovery pass in a fresh CLI session; "
                    "missing items are prioritized and full evidence coverage is required again.",
                )


def require_auth_coordination(*, sessions=False):
    """Fail closed before any concurrent session can use an old runner image."""
    from .runner import RunnerError, _docker

    try:
        info = json.loads(_docker(["image", "inspect", settings.CLI_RUNNER_IMAGE]))
        labels = info[0]["Config"].get("Labels", {})
        if labels.get("auto-reviewer.auth-coordination") != "v1":
            raise ValueError
        if sessions and labels.get("auto-reviewer.sessions") != "v1":
            raise ValueError
    except (RunnerError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError("invalid", detail="cli_setup") from None


def _review_cli_once(provider, context, cancelled, *, on_capacity=None, budget=None, **kwargs):
    from contextlib import nullcontext

    from .capacity import Container, cli_scope
    from .runner import _docker

    volume, scope = cli_scope(provider.kind)
    priority = 0 if kwargs.get("task") == "review_synthesis" else 1
    with admit_cli(scope, cancelled, on_wait=on_capacity, priority=priority) as slot:
        if getattr(provider, "cli_sessions", None):
            require_auth_coordination(sessions=True)
        elif slot.admission_limit is None or slot.admission_limit > 1:
            require_auth_coordination()
        container = Container(slot, _docker, volume=volume)
        container.cleanup()  # Admission housekeeping is not model active time.
        with budget.active() if budget else nullcontext():

            def active_cancelled():
                if budget:
                    budget.check()
                return cancelled()

            try:
                return _review_cli_active(
                    provider,
                    context,
                    active_cancelled,
                    container=container,
                    volume=volume,
                    **kwargs,
                )
            finally:
                container.cleanup()  # Count cleanup; never release before verified absence.


def _review_cli_active(
    provider,
    context,
    cancelled,
    *,
    on_response=None,
    on_activity=None,
    recovery=None,
    task="review",
    container,
    volume,
):
    from .providers import _wire_schema
    from .runner import RunnerError, _docker

    if cancelled():
        raise ReviewCancelled()
    image = settings.CLI_RUNNER_IMAGE
    name = container.slot.name
    args = container_arguments(provider.kind, name, image, volume)
    schema, system, _ = review_contract(task)
    parser = CLIStream(provider.kind, context, on_activity, task=task)
    session = getattr(provider, "cli_sessions", {}).get(task)
    if session and recovery is not None:
        session = {**session, "id": None}
    process = None
    try:
        try:
            _docker(["volume", "inspect", volume])
        except RunnerError:
            raise ProviderError("auth", detail="cli_auth") from None
        name = container.create(args)
        payload = json.dumps(
            {
                "kind": provider.kind,
                "model": provider.model,
                "effort": getattr(provider, "effort", ""),
                "context": context,
                "schema": _wire_schema(schema),
                "system": system,
                "recovery": recovery,
                "session": session,
            }
        )
        with tempfile.TemporaryFile() as request:
            request.write(payload.encode())
            request.seek(0)
            process = subprocess.Popen(
                ["docker", "--host=unix:///var/run/docker.sock", "start", "-ai", name],
                stdin=request,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"},
            )
            os.set_blocking(process.stdout.fileno(), False)
            pending = bytearray()
            last_report = 0
            last_diagnostics = None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if cancelled():
                        raise ReviewCancelled()
                    for key, _ in selector.select(timeout=0.5):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            break
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if line.strip():
                                parser.line(line)
                                if parser.error and parser.error.kind == "attention":
                                    raise parser.error
                        if len(pending) > MAX_RESPONSE_BYTES:
                            raise ProviderError("invalid", detail="response_too_large")
                    now = time.monotonic()
                    if (
                        on_response
                        and now - last_report >= 1
                        and parser.diagnostics != last_diagnostics
                    ):
                        last_diagnostics = dict(parser.diagnostics)
                        on_response(last_diagnostics)
                        last_report = now
                if pending.strip():
                    parser.line(pending)
            while process.poll() is None:
                if cancelled():
                    raise ReviewCancelled()
                time.sleep(0.1)
            if cancelled():
                raise ReviewCancelled()
            state = json.loads(_docker(["inspect", "--format={{json .State}}", name]))
            if state.get("OOMKilled"):
                raise ProviderError("invalid", detail="cli_memory")
            result = parser.result(state.get("ExitCode", 1) or process.returncode)
            if not parser.coverage:
                error = ProviderError("invalid", detail="cli_coverage")
                error.missing_evidence = parser.missing_evidence
                raise error
            if session:
                if parser.session_id is None:
                    raise ProviderError("invalid", detail="cli_protocol")
                parser.diagnostics["cli_session"] = {
                    "key": session["key"],
                    "id": parser.session_id,
                    "task": task,
                }
            return result
    except RunnerError:
        raise ProviderError("unavailable", detail="cli_setup") from None
    finally:
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
        if on_response:
            on_response(dict(parser.diagnostics))


def cleanup_cli_orphans():
    """Legacy bulk cleanup is unsafe with concurrent workers; drain migration required."""
    from .capacity import CapacityError

    raise CapacityError()

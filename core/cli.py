"""Subscription CLIs in disposable containers; raw events never enter the dashboard."""

import json
import os
import re
import selectors
import subprocess
import tempfile
import time
import uuid

from django.conf import settings

from .providers import MAX_RESPONSE_BYTES, ProviderError, ReviewCancelled, validate_result


class CLIStream:
    def __init__(self, kind, context=None, on_activity=None):
        from .transcript import Transcript

        self.transcript = Transcript(kind, context or {}, on_activity)
        self.kind = kind
        self.diagnostics = {"transport": kind, "events_received": 0, "last_event": "starting"}
        self.output = None
        self.complete = False
        self.error = None
        self.coverage = False

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
        if code == "auth_missing":
            error = ProviderError("auth", detail="cli_auth")
        elif code in ("content_policy_violation", "content_filter", "refusal"):
            error = ProviderError("attention")
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
        if kind == "runner_coverage":
            read, total = event.get("read"), event.get("total")
            if type(read) is int and type(total) is int and 0 <= read <= total <= 1_000_000:
                self.coverage = read == total
                self.diagnostics.update(evidence_read=read, evidence_total=total)
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
        elif kind == "stream_event":
            body = event.get("event", {})
            delta = body.get("delta", {}) if isinstance(body, dict) else {}
            if isinstance(delta, dict) and delta.get("stop_reason") == "refusal":
                self.error = ProviderError("attention")
                self.diagnostics.update(refusal_detected=True, last_event="declined")
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
            if event.get("is_error") or event.get("subtype") != "success":
                self.fail({"message": event.get("result", ""), "code": event.get("subtype")})
            else:
                self.output = event.get("structured_output")
                self.complete = True
                self.diagnostics["last_event"] = "completed"

    def usage(self, usage):
        if isinstance(usage, dict):
            for key in ("input_tokens", "output_tokens"):
                value = usage.get(key)
                if type(value) is int and 0 <= value <= 1_000_000_000:
                    self.diagnostics[key] = value

    def result(self, exit_code):
        if self.error:
            raise self.error
        if exit_code or not self.complete:
            raise ProviderError("invalid", detail="cli_protocol")
        try:
            value = json.loads(self.output) if isinstance(self.output, str) else self.output
            return validate_result(value)
        except (ValueError, TypeError, RecursionError, ProviderError):
            raise ProviderError("invalid", detail="output_invalid") from None


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


def review_cli(provider, context, cancelled, *, on_response=None, on_activity=None):
    from .providers import SCHEMA, SYSTEM, _wire_schema
    from .runner import RunnerError, _docker

    if cancelled():
        raise ReviewCancelled()
    image = settings.CLI_RUNNER_IMAGE
    volume = (
        settings.CLI_CODEX_AUTH_VOLUME
        if provider.kind == "codex_cli"
        else settings.CLI_CLAUDE_AUTH_VOLUME
    )
    name = "pr-review-cli-" + uuid.uuid4().hex
    args = container_arguments(provider.kind, name, image, volume)
    parser = CLIStream(provider.kind, context, on_activity)
    process = None
    created = False
    try:
        try:
            _docker(["volume", "inspect", volume])
        except RunnerError:
            raise ProviderError("auth", detail="cli_auth") from None
        _docker(args)
        created = True
        payload = json.dumps(
            {
                "kind": provider.kind,
                "model": provider.model,
                "context": context,
                "schema": _wire_schema(SCHEMA),
                "system": SYSTEM,
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
                raise ProviderError("invalid", detail="cli_coverage")
            return result
    except RunnerError:
        raise ProviderError("unavailable", detail="cli_setup") from None
    finally:
        if created:
            try:
                _docker(["rm", "-f", name], timeout=10)
            except RunnerError:
                pass
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
        if on_response:
            on_response(dict(parser.diagnostics))


def cleanup_cli_orphans():
    from .runner import RunnerError, _docker

    identifiers = _docker(["ps", "-aq", "--filter=label=auto-reviewer.cli=true"]).split()
    if any(not re.fullmatch(r"[a-f0-9]{12,64}", value) for value in identifiers):
        raise RunnerError()
    if identifiers:
        _docker(["rm", "-f", *identifiers])

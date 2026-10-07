import json
from unittest import TestCase

from core.providers import ProviderError
from core.tests.test_providers import RESULT


class CLIStreamTests(TestCase):
    def test_claude_assistant_rate_limit_survives_generic_final_error(self):
        p = self.parser("claude_cli")
        p.feed({"type": "assistant", "error": "rate_limit", "message": {"content": []}})
        p.feed(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "Limit reached; resets later",
            }
        )
        p.feed({"type": "runner_error", "code": "cli_failed", "message": ""})
        with self.assertRaises(ProviderError) as caught:
            p.result(1)
        self.assertEqual(caught.exception.kind, "quota")
        self.assertEqual(caught.exception.detail, "cli_quota")

    def test_policy_signal_and_category_survive_without_raw_error_text(self):
        p = self.parser()
        p.feed(
            {
                "type": "turn.failed",
                "error": {
                    "code": "content_policy_violation",
                    "message": "private source secret",
                },
            }
        )
        self.assertEqual(p.diagnostics.get("refusal_signal"), "content_policy_violation")
        self.assertNotIn("private source", json.dumps(p.diagnostics))
        p = self.parser("claude_cli")
        p.feed(
            {
                "type": "stream_event",
                "event": {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": "refusal",
                        "stop_details": {"category": "cyber"},
                    },
                },
            }
        )
        self.assertEqual(p.diagnostics.get("refusal_category"), "cyber")
        self.assertEqual(p.diagnostics.get("refusal_signal"), "stop_reason_refusal")

    def parser(self, kind="codex_cli"):
        from core.cli import CLIStream

        return CLIStream(kind)

    def test_codex_requires_completed_turn_valid_json_and_successful_exit(self):
        p = self.parser()
        p.feed(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(RESULT)},
            }
        )
        with self.assertRaises(ProviderError):
            p.result(0)
        p.feed({"type": "turn.completed", "usage": {"input_tokens": 123, "output_tokens": 42}})
        self.assertEqual(p.result(0), RESULT)
        self.assertEqual(p.diagnostics["input_tokens"], 123)
        with self.assertRaises(ProviderError):
            p.result(1)

    def test_claude_structured_result(self):
        p = self.parser("claude_cli")
        p.feed(
            {"type": "result", "subtype": "success", "is_error": False, "structured_output": RESULT}
        )
        self.assertEqual(p.result(0), RESULT)

    def test_quota_and_auth_are_not_refusals(self):
        for message, kind in [
            ("You have hit your usage limit", "quota"),
            ("Not logged in. Please run /login", "auth"),
            ("model not found", "invalid"),
        ]:
            p = self.parser()
            p.feed({"type": "turn.failed", "error": {"message": message}})
            with self.assertRaises(ProviderError) as caught:
                p.result(1)
            self.assertEqual(caught.exception.kind, kind)
            self.assertNotIn(message, json.dumps(p.diagnostics))

    def test_refusal_is_only_classified_from_protocol_not_findings(self):
        p = self.parser("claude_cli")
        p.feed(
            {
                "type": "stream_event",
                "event": {"type": "message_delta", "delta": {"stop_reason": "refusal"}},
            }
        )
        with self.assertRaises(ProviderError) as caught:
            p.result(0)
        self.assertEqual(caught.exception.kind, "attention")
        p = self.parser()
        result = {**RESULT, "summary": "Discuss content_policy_violation in source."}
        p.feed(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(result)},
            }
        )
        p.feed({"type": "turn.completed"})
        self.assertEqual(p.result(0), result)

    def test_progress_never_contains_model_text_or_reasoning(self):
        p = self.parser()
        p.feed(
            {"type": "item.completed", "item": {"type": "reasoning", "text": "secret-reasoning"}}
        )
        self.assertEqual(p.diagnostics["events_received"], 1)
        self.assertNotIn("secret", json.dumps(p.diagnostics))

    def test_malformed_or_oversized_event_fails_closed(self):
        p = self.parser()
        with self.assertRaises(ProviderError):
            p.feed([])
        with self.assertRaises(ProviderError):
            p.line(b"x" * 2_000_001)

    def test_tools_are_not_accepted_as_success(self):
        p = self.parser()
        p.feed(
            {
                "type": "item.started",
                "item": {"type": "command_execution", "command": "cat /auth/auth.json"},
            }
        )
        with self.assertRaises(ProviderError):
            p.result(0)
        self.assertNotIn("/auth", json.dumps(p.diagnostics))

    def test_container_has_no_host_credentials_or_docker_socket(self):
        from core.cli import container_arguments

        args = container_arguments("codex_cli", "test", "sha256:" + "a" * 64, "private-auth")
        self.assertIn("--read-only", args)
        self.assertIn("--user=1000:1000", args)
        self.assertIn("type=volume,source=private-auth,target=/auth", args)
        self.assertNotIn("/var/run/docker.sock", " ".join(args))
        self.assertNotIn("/root", " ".join(args))
        self.assertNotIn("--privileged", args)
        with self.assertRaises(ProviderError):
            container_arguments("codex_cli", "test", "latest", "private-auth")

    def test_cli_auth_missing_has_actionable_error(self):
        p = self.parser()
        p.feed({"type": "runner_error", "code": "auth_missing"})
        with self.assertRaises(ProviderError) as caught:
            p.result(1)
        self.assertEqual(caught.exception.kind, "auth")
        self.assertEqual(caught.exception.detail, "cli_auth")

    def test_recovered_transport_retry_does_not_discard_valid_result(self):
        p = self.parser()
        p.feed({"type": "error", "message": "Reconnecting... 1/5 (connection lost)"})
        p.feed(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(RESULT)},
            }
        )
        p.feed({"type": "turn.completed"})
        self.assertEqual(p.result(0), RESULT)

    def test_cancellation_kills_process_and_removes_container(self):
        import subprocess
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch

        from django.test import override_settings

        from core.cli import review_cli
        from core.providers import ReviewCancelled

        child = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(60)"], stdout=subprocess.PIPE
        )
        checks = iter([False, True])
        with (
            override_settings(
                CLI_RUNNER_IMAGE="sha256:" + "a" * 64, CLI_CODEX_AUTH_VOLUME="private-auth"
            ),
            patch("core.runner._docker", return_value="[]") as docker,
            patch("core.cli.subprocess.Popen", return_value=child),
            self.assertRaises(ReviewCancelled),
        ):
            review_cli(
                SimpleNamespace(kind="codex_cli", model="gpt-6.1-sol"), {}, lambda: next(checks)
            )
        self.assertIsNotNone(child.poll())
        self.assertTrue(any(call.args[0][:2] == ["rm", "-f"] for call in docker.call_args_list))

    def test_explicit_refusal_stops_cli_without_waiting_for_more_turns(self):
        import subprocess
        import sys
        import time
        from types import SimpleNamespace
        from unittest.mock import patch

        from django.test import override_settings

        from core.cli import review_cli

        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                'import time;print(\'{"type":"runner_error","code":"refusal"}\',flush=True);time.sleep(2)',
            ],
            stdout=subprocess.PIPE,
        )
        started = time.monotonic()
        with (
            override_settings(
                CLI_RUNNER_IMAGE="sha256:" + "a" * 64, CLI_CODEX_AUTH_VOLUME="private-auth"
            ),
            patch("core.runner._docker", return_value='{"ExitCode":0}'),
            patch("core.cli.subprocess.Popen", return_value=child),
            self.assertRaises(ProviderError) as caught,
        ):
            review_cli(SimpleNamespace(kind="codex_cli", model="test"), {}, lambda: False)
        self.assertEqual(caught.exception.kind, "attention")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNotNone(child.poll())

    def test_evidence_tool_is_allowed_but_arbitrary_mcp_is_rejected(self):
        p = self.parser()
        p.feed(
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "review_context",
                    "tool": "read_evidence",
                },
            }
        )
        self.assertIsNone(p.error)
        p.feed(
            {
                "type": "item.completed",
                "item": {"type": "mcp_tool_call", "server": "other", "tool": "read_evidence"},
            }
        )
        self.assertEqual(p.error.detail, "cli_tools")

    def test_coverage_is_explicit_and_bounded(self):
        p = self.parser()
        p.feed({"type": "runner_coverage", "read": 2, "total": 3})
        self.assertFalse(p.coverage)
        p.feed({"type": "runner_coverage", "read": 3, "total": 3})
        self.assertTrue(p.coverage)
        with self.assertRaises(ProviderError):
            p.feed({"type": "runner_coverage", "read": 4, "total": 3})

    def test_cli_dispatch_never_decrypts_api_credentials(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from core.providers import review

        p = SimpleNamespace(kind="codex_cli", model="gpt-6.1-sol", credential=None)
        with (
            patch("core.cli.review_cli", return_value=RESULT) as cli,
            patch("core.providers.decrypt") as decrypt,
        ):
            self.assertEqual(review(p, {}, lambda: False), RESULT)
        cli.assert_called_once()
        decrypt.assert_not_called()

    def test_cli_provider_does_not_accept_an_api_key(self):
        from django.core.exceptions import ValidationError

        from core.models import Provider

        Provider(kind="codex_cli", model="gpt-6.1-sol").clean()
        with self.assertRaises(ValidationError):
            Provider(kind="codex_cli", credential_id=1).clean()
        with self.assertRaises(ValidationError):
            Provider(kind="openai").clean()


class CLIActivityTests(TestCase):
    def test_progress_is_allowlisted_deduplicated_and_no_model_text(self):
        from core.activity import cli_progress

        calls = []
        state = {}

        def progress(code, **kw):
            calls.append((code, kw.get("count")))

        data = {
            "transport": "codex_cli",
            "last_event": "reading_evidence",
            "evidence_read": 4,
            "reasoning": "private thoughts",
            "events_received": 123,
        }
        cli_progress(progress, data, state)
        cli_progress(progress, data, state)
        self.assertEqual(calls, [("cli_reading", None), ("cli_evidence", 4)])
        cli_progress(
            progress, {**data, "last_event": "private thoughts", "evidence_read": "secret"}, state
        )
        self.assertEqual(len(calls), 2)

"""Remote transport preserves native validators, admission, callbacks and fallback."""

import base64
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from core import cli, graph, runner
from core.providers import ProviderError, ReviewCancelled
from core.tests.docker_fake import DockerFake
from core.tests.test_providers import RESULT
from core.tests.test_runner import archive


@override_settings(REMOTE_EXECUTION_ENABLED=True, CLI_CODEX_AUTH_VOLUME="remote-auth")
class RemoteExecutionIntegrationTests(SimpleTestCase):
    def setUp(self):
        self.held = False
        self.slot = SimpleNamespace(
            name="remote-test-slot", scope="scope", index=0, admission_limit=1
        )
        self.provider = SimpleNamespace(kind="codex_cli", model="model", effort="high")
        self.terminal = {"exit_code": 0, "oom": False}
        self.seen = []

    @contextmanager
    def admitted(self, *args, **kwargs):
        self.held = True
        try:
            yield self.slot
        finally:
            self.held = False

    def stream(self, events, terminal=None):
        def dispatch(operation, payload, slot, cancelled, on_chunk):
            self.assertTrue(self.held)
            self.assertIs(slot, self.slot)
            self.seen.append((operation, payload))
            raw = b"\n".join(json.dumps(event).encode() for event in events)
            # Cut in the middle of lines; final event deliberately has no newline.
            for offset in range(0, len(raw), 17):
                on_chunk(raw[offset : offset + 17])
            return terminal or self.terminal

        return dispatch

    def events(self, coverage=True):
        return [
            {"type": "thread.started", "thread_id": "12345678-1234-1234-1234-123456789abc"},
            {
                "type": "runner_coverage",
                "read": int(coverage),
                "total": 1,
                "missing": [] if coverage else ["evidence"],
            },
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(RESULT)},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}},
        ]

    def invoke_cli(self, dispatch, **kwargs):
        with (
            patch("core.cli.admit_cli", self.admitted),
            patch("core.runner._docker", side_effect=DockerFake()) as docker,
            patch("core.execution_dispatch.try_run", side_effect=dispatch),
            patch("core.cli.subprocess.Popen") as local,
        ):
            result = cli.review_cli(self.provider, {"evidence": "data"}, lambda: False, **kwargs)
        local.assert_not_called()
        self.assertFalse(
            any(call.args[0][0] in ("create", "image", "volume") for call in docker.call_args_list)
        )
        self.assertFalse(self.held)
        return result

    def test_remote_cli_preserves_validation_usage_and_session_metadata(self):
        self.provider.cli_sessions = {"review": {"key": "session-key", "id": None}}
        reports = []
        result = self.invoke_cli(self.stream(self.events()), on_response=reports.append)
        self.assertEqual(result, RESULT)
        operation, payload = self.seen[0]
        self.assertEqual(operation, "model")
        self.assertEqual(payload["effort"], "high")
        self.assertEqual(payload["context"], {"evidence": "data"})
        self.assertEqual(
            reports[-1]["cli_session"],
            {"key": "session-key", "id": "12345678-1234-1234-1234-123456789abc", "task": "review"},
        )
        self.assertEqual(reports[-1]["evidence_read"], 1)

    def test_remote_missing_evidence_runs_existing_single_fresh_recovery(self):
        self.provider.cli_sessions = {"review": {"key": "session-key", "id": "old"}}
        dispatches = [self.stream(self.events(False)), self.stream(self.events())]

        def dispatch(*args):
            return dispatches.pop(0)(*args)

        self.assertEqual(self.invoke_cli(dispatch), RESULT)
        self.assertIsNone(self.seen[0][1]["recovery"])
        self.assertEqual(self.seen[1][1]["recovery"], ["evidence"])
        self.assertIsNone(self.seen[1][1]["session"]["id"])

    def test_remote_refusal_raises_immediately_and_reports_diagnostics(self):
        reports = []
        events = [{"type": "runner_error", "code": "content_policy_violation"}]
        with self.assertRaises(ProviderError) as caught:
            self.invoke_cli(self.stream(events), on_response=reports.append)
        self.assertEqual(caught.exception.kind, "attention")
        self.assertTrue(reports[-1]["refusal_detected"])

    def test_remote_arbitrary_tool_call_still_rejected(self):
        events = [{"type": "item.completed", "item": {"type": "command_execution"}}] + self.events()
        with self.assertRaises(ProviderError) as caught:
            self.invoke_cli(self.stream(events))
        self.assertEqual(caught.exception.detail, "cli_tools")

    def test_remote_oom_maps_to_existing_error(self):
        with self.assertRaises(ProviderError) as caught:
            self.invoke_cli(self.stream(self.events(), {"exit_code": 137, "oom": True}))
        self.assertEqual(caught.exception.detail, "cli_memory")

    def test_no_worker_uses_existing_local_cli_and_auth_gate(self):
        with (
            patch("core.cli.admit_cli", self.admitted),
            patch("core.runner._docker", side_effect=DockerFake()),
            patch("core.execution_dispatch.try_run", return_value=None),
            patch("core.cli._require_local_cli") as gate,
            patch("core.cli._review_cli_active", return_value=RESULT) as local,
        ):
            self.assertEqual(cli.review_cli(self.provider, {}, lambda: False), RESULT)
        gate.assert_called_once_with(self.provider, self.slot)
        local.assert_called_once()

    def test_remote_graph_preserves_payload_and_native_head_path_validation(self):
        request = {"repository": "org/repo", "snapshots": [], "changed": ["a.py"]}
        value = {
            "version": graph.VERSION,
            "repository": "org/repo",
            "head_sha": "a" * 40,
            "paths": ["a.py"],
            "depth": 2,
            "snapshots": [],
        }
        with (
            patch("core.capacity.permit", self.admitted),
            patch("core.runner._docker", side_effect=DockerFake()),
            patch("core.execution_dispatch.try_run", side_effect=self.stream([value])),
            patch("core.graph.subprocess.Popen") as local,
        ):
            result = graph.execute(request)
        self.assertEqual(graph.validate(result, "org/repo", "a" * 40), value)
        self.assertEqual(self.seen, [("graph", request)])
        local.assert_not_called()
        from core.github import IntegrationError

        with self.assertRaises(IntegrationError):
            graph.validate(result, "org/repo", "b" * 40)

    def test_remote_tests_keep_test_failure_distinct_from_runner_failure(self):
        repository = SimpleNamespace(
            test_image="example/test@sha256:" + "a" * 64, test_command=["false"]
        )
        source = archive()
        result = {"passed": False, "summary": "Isolated local tests failed or exhausted resources."}
        with (
            patch("core.capacity.permit", self.admitted),
            patch("core.runner._docker", side_effect=DockerFake()),
            patch("core.execution_dispatch.try_run", side_effect=self.stream([result])),
            patch("core.runner.subprocess.Popen") as local,
        ):
            self.assertEqual(runner.run_tests(repository, source, lambda: False), result)
        self.assertEqual(base64.b64decode(self.seen[0][1]["archive"]), source)
        self.assertEqual(self.seen[0][1]["command"], ["false"])
        local.assert_not_called()

    def test_remote_tests_reject_malformed_result(self):
        repository = SimpleNamespace(
            test_image="example/test@sha256:" + "a" * 64, test_command=["true"]
        )
        with (
            patch("core.capacity.permit", self.admitted),
            patch("core.runner._docker", side_effect=DockerFake()),
            patch(
                "core.execution_dispatch.try_run",
                side_effect=self.stream([{"passed": "yes", "summary": "bad"}]),
            ),
            self.assertRaises(runner.RunnerError),
        ):
            runner.run_tests(repository, archive(), lambda: False)

    def test_remote_dispatch_cancellation_never_falls_back(self):
        with (
            patch("core.cli.admit_cli", self.admitted),
            patch("core.runner._docker", side_effect=DockerFake()),
            patch("core.execution_dispatch.try_run", side_effect=ReviewCancelled()),
            patch("core.cli._review_cli_active") as local,
            self.assertRaises(ReviewCancelled),
        ):
            cli.review_cli(self.provider, {}, lambda: False)
        local.assert_not_called()

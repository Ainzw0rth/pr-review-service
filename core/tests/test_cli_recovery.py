from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from core.cli import CLIStream, review_cli
from core.providers import ProviderError, ReviewCancelled
from core.tests.test_providers import RESULT


class CLIRecoveryTests(TestCase):
    provider = SimpleNamespace(kind="claude_cli", model="configured")

    def incomplete(self):
        error = ProviderError("invalid", detail="cli_coverage")
        error.missing_evidence = ["files_0"]
        return error

    def test_one_recovery_pass_prioritizes_missing_evidence(self):
        events = []
        with patch("core.cli._review_cli_once", side_effect=[self.incomplete(), RESULT]) as run:
            result = review_cli(
                self.provider,
                {"files": [{}]},
                lambda: False,
                on_activity=lambda *args: events.append(args),
            )
        self.assertEqual(result, RESULT)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs["recovery"], ["files_0"])
        self.assertIn("recovery pass", events[0][1])

    def test_recovery_is_bounded_and_remains_incomplete(self):
        with patch("core.cli._review_cli_once", side_effect=self.incomplete()) as run:
            with self.assertRaises(ProviderError) as caught:
                review_cli(self.provider, {}, lambda: False)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(caught.exception.detail, "cli_coverage")

    def test_other_errors_do_not_trigger_recovery(self):
        for error in [
            ProviderError("quota"),
            ProviderError("auth"),
            ProviderError("attention"),
            ProviderError("invalid", detail="output_invalid"),
        ]:
            with (
                self.subTest(kind=error.kind),
                patch("core.cli._review_cli_once", side_effect=error) as run,
            ):
                with self.assertRaises(ProviderError):
                    review_cli(self.provider, {}, lambda: False)
                self.assertEqual(run.call_count, 1)

    def test_cancelled_between_passes_does_not_restart(self):
        cancelled = False

        def first(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            raise self.incomplete()

        with patch("core.cli._review_cli_once", side_effect=first) as run:
            with self.assertRaises(ReviewCancelled):
                review_cli(self.provider, {}, lambda: cancelled)
        self.assertEqual(run.call_count, 1)

    def test_missing_ids_validated_and_not_in_public_diagnostics(self):
        stream = CLIStream("claude_cli", {"files": [{}]})
        stream.feed({"type": "runner_coverage", "read": 0, "total": 1, "missing": ["files_0"]})
        self.assertEqual(stream.missing_evidence, ["files_0"])
        self.assertNotIn("missing", stream.diagnostics)
        with self.assertRaises(ProviderError):
            stream.feed(
                {"type": "runner_coverage", "read": 0, "total": 1, "missing": ["/auth/auth.json"]}
            )

    def test_diagnostics_identify_recovery_pass(self):
        reports = []
        calls = 0

        def run(*args, **kwargs):
            nonlocal calls
            calls += 1
            kwargs["on_response"]({"transport": "claude_cli", "events_received": calls})
            if calls == 1:
                raise self.incomplete()
            return RESULT

        with patch("core.cli._review_cli_once", side_effect=run):
            review_cli(self.provider, {}, lambda: False, on_response=reports.append)
        self.assertEqual([r["recovery_pass"] for r in reports], [0, 1])

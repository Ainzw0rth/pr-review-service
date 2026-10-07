from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from core.cli import CLIStream, review_cli
from core.providers import ProviderError
from core.usage import public_usage


class UsageTests(TestCase):
    def test_cli_retains_cache_counters_but_not_arbitrary_fields(self):
        p = CLIStream("claude_cli")
        p.usage(
            {
                "input_tokens": 3,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 40,
                "output_tokens": 9,
                "secret": "private",
            }
        )
        self.assertEqual(p.diagnostics["cached_input_tokens"], 200)
        self.assertEqual(p.diagnostics["cache_creation_input_tokens"], 40)
        self.assertNotIn("secret", p.diagnostics)

    def test_recovery_retains_both_sessions_without_double_counting_snapshots(self):
        calls = []
        reports = []

        def once(*args, **kwargs):
            calls.append(1)
            kwargs["on_response"]({"input_tokens": 10, "output_tokens": 2})
            kwargs["on_response"]({"input_tokens": 20, "output_tokens": 4})
            if len(calls) == 1:
                raise ProviderError("invalid", detail="cli_coverage")
            return {}

        with patch("core.cli._review_cli_once", side_effect=once):
            review_cli(
                SimpleNamespace(kind="codex_cli"), {}, lambda: False, on_response=reports.append
            )
        counters = public_usage(reports[-1])
        self.assertEqual(counters["totals"]["input_tokens"], 40)
        self.assertEqual(counters["session_count"], 2)
        self.assertIsNone(counters["totals"]["cached_input_tokens"])

    def test_unknown_and_invalid_values_never_become_zero_or_public_text(self):
        result = public_usage(
            {"input_tokens": True, "output_tokens": -2, "usage_passes": [{"input_tokens": 10}, {}]}
        )
        self.assertIsNone(result["totals"]["input_tokens"])
        self.assertIsNone(result["totals"]["output_tokens"])

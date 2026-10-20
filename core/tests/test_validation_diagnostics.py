import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import httpx

from core import providers
from core.cli import CLIStream
from core.review_team import finalize, validate_report
from core.tests.test_review_team import report, synthesis
from core.tests.test_review_v2 import context


class ValidationDiagnosticsTests(TestCase):
    def rejected(self, value, *, code, path, validate=validate_report, evidence=None, rule=None):
        with self.assertRaises(providers.ProviderError) as caught:
            validate(value, evidence or context())
        error = caught.exception
        self.assertEqual(error.kind, "invalid")
        self.assertEqual(error.validation_error, code)
        self.assertEqual(getattr(error, "validation_path", None), path)
        self.assertEqual(getattr(error, "validation_rule", None), rule)
        return error

    def test_schema_failure_identifies_field_and_constraint_not_rejected_content(self):
        value = report()
        value["summary"] = "private-output-" * 1000
        error = self.rejected(value, code="report_schema", path=["summary"], rule="maxLength")
        public = providers.failure_description(
            {"failure_detail": "output_invalid", **providers.validation_diagnostics(error)},
            "failed",
        )
        self.assertIn("$.summary", public)
        self.assertIn("maxLength", public)
        self.assertNotIn("private-output", public + str(error))

    def test_missing_required_field_names_only_the_trusted_schema_field(self):
        value = report(finding=True)
        del value["findings"][0]["impact"]
        self.rejected(value, code="report_schema", path=["findings", 0, "impact"], rule="required")

    def test_count_limit_is_recorded_without_rejected_coverage_text(self):
        value = report()
        value["coverage"]["reviewed"] = ["private-covered-area"] * 101
        error = self.rejected(
            value, code="report_schema", path=["coverage", "reviewed"], rule="maxItems"
        )
        self.assertEqual(getattr(error, "validation_limit", None), 100)
        public = providers.failure_description(
            {"failure_detail": "output_invalid", **providers.validation_diagnostics(error)},
            "failed",
        )
        self.assertIn("100", public)
        self.assertNotIn("private-covered-area", public)

    def test_untrusted_extra_property_name_never_enters_diagnostics(self):
        value = report(finding=True)
        value["findings"][0]["private-source-key"] = "private-source-value"
        error = self.rejected(
            value, code="report_schema", path=["findings", 0], rule="additionalProperties"
        )
        diagnostics = providers.validation_diagnostics(error)
        self.assertNotIn("private-source", json.dumps(diagnostics) + str(error))

    def test_source_and_quote_failures_are_distinct(self):
        for key, value, code in (
            ("source", "private-invented-source", "citation_source"),
            ("quote", "private-invented-excerpt", "citation_quote"),
        ):
            with self.subTest(key=key):
                value_report = report(finding=True)
                value_report["findings"][0]["evidence"][0][key] = value
                error = self.rejected(
                    value_report, code=code, path=["findings", 0, "evidence", 0, key]
                )
                public = providers.failure_description(
                    {"failure_detail": "output_invalid", **providers.validation_diagnostics(error)},
                    "failed",
                )
                self.assertIn(key, public.lower())
                self.assertNotIn(
                    value, public + json.dumps(providers.validation_diagnostics(error))
                )

    def test_missing_changed_file_and_wrong_line_are_distinct(self):
        for field, value, code in (
            ("path", "private/missing.py", "finding_path"),
            ("line", 99, "finding_line"),
        ):
            with self.subTest(field=field):
                value_report = report(finding=True)
                value_report["findings"][0][field] = value
                self.rejected(value_report, code=code, path=["findings", 0, field])

    def test_duplicate_finding_and_whitespace_text_are_explained(self):
        value = report(finding=True)
        value["findings"] *= 2
        self.rejected(value, code="report_duplicate_id", path=["findings"])
        value = report()
        value["coverage"]["reviewed"] = ["   "]
        self.rejected(value, code="report_empty_text", path=["coverage", "reviewed", 0])

    def test_adjudication_cannot_silently_omit_a_candidate(self):
        value = synthesis()
        reports = {"code": report(finding=True), "security": report(), "tests": report()}
        with self.assertRaises(providers.ProviderError) as caught:
            finalize(value, reports, context())
        self.assertEqual(caught.exception.validation_error, "synthesis_assessments")
        self.assertEqual(caught.exception.validation_path, ["assessments"])

    def test_negative_adjudication_minimum_is_retained(self):
        from core.review_team import validate_synthesis_shape

        value = synthesis()
        value["assessments"] = [
            {
                "finding_id": "code:F1",
                "decision": "reject",
                "reason": "Unsupported candidate.",
                "finding_index": -2,
                "evidence": [{"source": "files_0", "quote": "secret"}],
            }
        ]
        with self.assertRaises(providers.ProviderError) as caught:
            validate_synthesis_shape(value)
        self.assertEqual(caught.exception.validation_rule, "minimum")
        self.assertEqual(caught.exception.validation_limit, -1)

    def test_cli_completed_response_preserves_detailed_validation_error(self):
        value = report(finding=True)
        value["findings"][0]["impact"] = None
        parser = CLIStream("codex_cli", task="review_tests")
        parser.feed(
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(value)}}
        )
        parser.feed({"type": "turn.completed"})
        with self.assertRaises(providers.ProviderError) as caught:
            parser.result(0)
        self.assertEqual(caught.exception.detail, "output_invalid")
        self.assertEqual(caught.exception.validation_error, "report_schema")
        self.assertEqual(caught.exception.validation_path, ["findings", 0, "impact"])
        self.assertEqual(parser.diagnostics["validation_rule"], "type")
        self.assertEqual(parser.diagnostics["validation_path"], ["findings", 0, "impact"])

    def test_api_completed_response_preserves_detailed_validation_error(self):
        value = report()
        value["summary"] = ""
        provider = SimpleNamespace(
            kind="openai",
            model="test-model",
            credential=SimpleNamespace(encrypted_value="test-ciphertext"),
        )
        wire = {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}
            ],
        }
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=wire))
        )
        with (
            patch.object(providers, "decrypt", return_value="test-key"),
            patch.object(providers.httpx, "Client", return_value=client),
            self.assertRaises(providers.ProviderError) as caught,
        ):
            providers.review(provider, context(), lambda: False, task="review_security")
        self.assertEqual(caught.exception.detail, "output_invalid")
        self.assertEqual(caught.exception.validation_error, "report_schema")
        self.assertEqual(caught.exception.validation_path, ["summary"])
        self.assertEqual(caught.exception.validation_rule, "minLength")

    def test_diagnostic_boundary_rejects_raw_fields_and_untrusted_rules(self):
        for path in (["private-field"], ["findings", "private-path"], [True], [10**100], "private"):
            with self.subTest(path=path):
                error = providers.ProviderError(
                    "invalid",
                    validation_error="report_schema",
                    validation_path=path,
                    validation_rule="private-rule",
                )
                self.assertIsNone(error.validation_path)
                self.assertIsNone(error.validation_rule)
                public = providers.failure_description(
                    {
                        "failure_detail": "output_invalid",
                        "validation_error": "report_schema",
                        "validation_path": path,
                        "validation_rule": {"private": "value"},
                    },
                    "failed",
                )
                self.assertNotIn("private", public)

    def test_valid_incomplete_report_still_supplies_an_assessment(self):
        value = report(complete=False)
        value["coverage"]["limitations"] = ["Missing caller evidence."]
        self.assertEqual(validate_report(value, context()), value)

    def test_schema_limit_metadata_cannot_contain_model_text_or_unbounded_numbers(self):
        for limit in (True, -1, 10**100, "private-limit", {"private": "value"}):
            with self.subTest(limit=limit):
                error = providers.ProviderError(
                    "invalid",
                    validation_error="report_schema",
                    validation_rule="maxItems",
                    validation_limit=limit,
                )
                self.assertNotIn("validation_limit", providers.validation_diagnostics(error))
                public = providers.failure_description(
                    {
                        "failure_detail": "output_invalid",
                        "validation_error": "report_schema",
                        "validation_rule": "maxItems",
                        "validation_limit": limit,
                    },
                    "failed",
                )
                self.assertNotIn("private", public)

    def test_contextual_validation_reason_survives_legacy_error_detail(self):
        public = providers.failure_description(
            {
                "failure_kind": "invalid",
                "failure_detail": None,
                "validation_error": "action_plan",
                "validation_path": ["action_plan", "inline_findings", "decision"],
            },
            "attention",
        )
        self.assertIn("action plan", public)
        self.assertIn("$.action_plan.inline_findings.decision", public)

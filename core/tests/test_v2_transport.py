import json
from itertools import product
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import httpx

from core.cli import CLIStream
from core.providers import ProviderError, failure_description, review

INTENT = {
    "goal": {"text": "Fix login", "sources": ["pr"], "certainty": "stated"},
    "requirements": [],
    "non_goals": [],
    "unknowns": [],
}


class V2TransportTests(TestCase):
    def test_cli_preserves_intent_validation_reason_without_raw_output(self):
        for kind in ("codex_cli", "claude_cli"):
            with self.subTest(kind=kind):
                parser = CLIStream(kind, task="intent")
                if kind == "codex_cli":
                    parser.feed(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": json.dumps({"private-source": "private-value"}),
                            },
                        }
                    )
                    parser.feed({"type": "turn.completed"})
                else:
                    parser.feed(
                        {
                            "type": "result",
                            "subtype": "success",
                            "structured_output": {"private-source": "private-value"},
                        }
                    )
                with self.assertRaises(ProviderError) as caught:
                    parser.result(0)
                self.assertEqual(caught.exception.detail, "output_invalid")
                self.assertEqual(caught.exception.validation_error, "intent_schema")
                self.assertEqual(parser.diagnostics["validation_error"], "intent_schema")
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("private", json.dumps(parser.diagnostics))

    def test_dashboard_explains_only_allowlisted_validation_reasons(self):
        diagnostics = {
            "failure_detail": "output_invalid",
            "validation_error": "intent_unknown_source",
        }
        self.assertIn("unknown evidence", failure_description(diagnostics, "failed"))
        for untrusted in ("private-model-output", ["private"], {"private": "value"}):
            diagnostics["validation_error"] = untrusted
            self.assertNotIn("private", failure_description(diagnostics, "failed"))
            self.assertIsNone(ProviderError("invalid", validation_error=untrusted).validation_error)

    def test_malformed_cli_json_has_a_safe_distinct_reason(self):
        parser = CLIStream("codex_cli", task="intent")
        parser.feed(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "private text"}}
        )
        parser.feed({"type": "turn.completed"})
        with self.assertRaises(ProviderError) as caught:
            parser.result(0)
        self.assertEqual(caught.exception.validation_error, "invalid_json")
        self.assertNotIn("private", json.dumps(parser.diagnostics))

    def test_api_preserves_intent_validation_reason(self):
        for kind, (text, reason) in product(
            ("openai", "anthropic"),
            (('{"private":"value"}', "intent_schema"), ("private text", "invalid_json")),
        ):
            body = (
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    ],
                }
                if kind == "openai"
                else {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": text}],
                }
            )
            client = httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
            )
            provider = SimpleNamespace(
                kind=kind, model="configured", credential=SimpleNamespace(encrypted_value="unused")
            )
            with (
                self.subTest(kind=kind, reason=reason),
                patch("core.providers.decrypt", return_value="unused"),
                patch("core.providers.httpx.Client", return_value=client),
                self.assertRaises(ProviderError) as caught,
            ):
                review(provider, {"evidence": []}, lambda: False, task="intent")
            self.assertEqual(caught.exception.detail, "output_invalid")
            self.assertEqual(caught.exception.validation_error, reason)
            self.assertNotIn("private", str(caught.exception))

    def test_intent_uses_its_own_schema_in_both_api_adapters(self):
        for kind in ("openai", "anthropic"):
            with self.subTest(kind=kind):
                requests = []

                def respond(request):
                    requests.append(json.loads(request.content))
                    body = (
                        {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {"type": "output_text", "text": json.dumps(INTENT)}
                                    ],
                                }
                            ],
                        }
                        if kind == "openai"
                        else {
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": json.dumps(INTENT)}],
                        }
                    )
                    return httpx.Response(200, json=body)

                client = httpx.Client(transport=httpx.MockTransport(respond))
                provider = SimpleNamespace(
                    kind=kind,
                    model="configured",
                    credential=SimpleNamespace(encrypted_value="unused"),
                )
                with (
                    patch("core.providers.decrypt", return_value="unused"),
                    patch("core.providers.httpx.Client", return_value=client),
                ):
                    self.assertEqual(
                        review(provider, {"evidence": []}, lambda: False, task="intent"), INTENT
                    )
                schema = (
                    requests[0]["text"]["format"]["schema"]
                    if kind == "openai"
                    else requests[0]["output_config"]["format"]["schema"]
                )
                self.assertIn("goal", schema["properties"])
                self.assertNotIn("findings", schema["properties"])
                self.assertNotIn("uniqueItems", json.dumps(schema))

    def test_cli_parser_validates_intent_without_accepting_it_as_review(self):
        parser = CLIStream("claude_cli", task="intent")
        parser.feed({"type": "result", "subtype": "success", "structured_output": INTENT})
        self.assertEqual(parser.result(0), INTENT)
        legacy = CLIStream("claude_cli")
        legacy.feed({"type": "result", "subtype": "success", "structured_output": INTENT})
        with self.assertRaises(ProviderError):
            legacy.result(0)

    def test_unknown_task_never_starts_a_provider_request(self):
        with patch("core.providers.httpx.Client") as client, self.assertRaises(ProviderError):
            review(
                SimpleNamespace(kind="openai", model="configured"),
                {},
                lambda: False,
                task="invented",
            )
        client.assert_not_called()

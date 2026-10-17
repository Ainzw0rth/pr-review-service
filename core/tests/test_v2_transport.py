import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import httpx

from core.cli import CLIStream
from core.providers import ProviderError, review

INTENT = {
    "goal": {"text": "Fix login", "sources": ["pr"], "certainty": "stated"},
    "requirements": [],
    "non_goals": [],
    "unknowns": [],
}


class V2TransportTests(TestCase):
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

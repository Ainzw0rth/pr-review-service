import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import httpx

from core import providers

RESULT = {
    "summary": "Reviewed.",
    "blocking": False,
    "findings": [],
    "replies": [],
    "related_comments": [],
    "complete": True,
}


class ProviderTests(TestCase):
    def test_response_diagnostics_keep_protocol_metadata_not_content(self):
        data = {
            "id": "msg_diagnostic123",
            "stop_reason": "refusal",
            "stop_details": {"category": "cyber", "explanation": "secret-key private-source"},
            "content": [{"type": "text", "text": "private-source"}],
            "usage": {"input_tokens": 123, "output_tokens": 0},
        }
        diagnostics = providers.response_diagnostics(
            200, {"request-id": "req_diagnostic123", "authorization": "secret-key"}, data
        )
        self.assertEqual(diagnostics["refusal_category"], "cyber")
        self.assertEqual(diagnostics["request_id"], "req_diagnostic123")
        self.assertEqual(diagnostics["stop_reason"], "refusal")
        self.assertNotIn("secret-key", json.dumps(diagnostics))
        self.assertNotIn("private-source", json.dumps(diagnostics))

    def test_unknown_diagnostic_values_are_not_retained(self):
        result = providers.response_diagnostics(
            400,
            {"request-id": "sk-secret-value"},
            {"stop_reason": "secret", "stop_details": {"category": ["secret"]}},
        )
        self.assertNotIn("secret", json.dumps(result))

    def call(
        self,
        data,
        status=200,
        headers=None,
        kind="openai",
        cancelled=lambda: False,
        on_response=None,
    ):
        provider = SimpleNamespace(
            kind=kind,
            model="configured-exact-model",
            credential=SimpleNamespace(encrypted_value="encrypted"),
        )
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(status, json=data, headers=headers)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        with (
            patch.object(providers, "decrypt", return_value="secret-key"),
            patch.object(providers.httpx, "Client", return_value=client),
        ):
            result = providers.review(
                provider, {"diff": "untrusted"}, cancelled, on_response=on_response
            )
        return result, requests

    def output(self, result=None):
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(result or RESULT)}],
                }
            ],
        }

    def test_openai_native_structured_output_preserves_model(self):
        result, requests = self.call(self.output())
        self.assertEqual(result, RESULT)
        self.assertEqual(str(requests[0].url), "https://api.openai.com/v1/responses")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["model"], "configured-exact-model")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertFalse(payload["store"])

    def test_anthropic_native_structured_output(self):
        result, requests = self.call(
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(RESULT)}],
            },
            kind="anthropic",
        )
        self.assertEqual(result, RESULT)
        self.assertIn("output_config", json.loads(requests[0].content))
        self.assertEqual(str(requests[0].url), "https://api.anthropic.com/v1/messages")

    def test_refusal_never_becomes_availability_failure(self):
        cases = [
            (
                self.output()
                | {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "refusal", "refusal": "secret-key"}],
                        }
                    ]
                },
                200,
                "openai",
            ),
            ({"stop_reason": "refusal"}, 200, "anthropic"),
            (
                {
                    "error": {
                        "code": "content_policy_violation",
                        "message": "secret-key",
                    }
                },
                403,
                "openai",
            ),
        ]
        for data, status, kind in cases:
            with (
                self.subTest(kind=kind, status=status),
                self.assertRaises(providers.ProviderError) as raised,
            ):
                self.call(data, status, kind=kind)
            self.assertEqual(raised.exception.kind, "attention")
            self.assertNotIn("secret-key", str(raised.exception))

    def test_refusal_callback_receives_diagnostics_before_exception(self):
        captured = []
        with self.assertRaises(providers.ProviderError):
            self.call(
                {"stop_reason": "refusal", "stop_details": {"category": "cyber"}},
                headers={"request-id": "req_test123"},
                kind="anthropic",
                on_response=captured.append,
            )
        self.assertEqual(captured[0]["request_id"], "req_test123")
        self.assertEqual(captured[0]["refusal_category"], "cyber")

    def test_http_error_classification_and_quota_retry(self):
        for status, kind in [
            (429, "quota"),
            (500, "unavailable"),
            (401, "auth"),
            (403, "auth"),
            (400, "invalid"),
        ]:
            with (
                self.subTest(status=status),
                self.assertRaises(providers.ProviderError) as raised,
            ):
                self.call({"error": {"message": "secret-key"}}, status, {"Retry-After": "60"})
            self.assertEqual(raised.exception.kind, kind)
            self.assertNotIn("secret-key", str(raised.exception))
            if status == 429:
                self.assertAlmostEqual(
                    (raised.exception.retry_at - datetime.now(timezone.utc)).total_seconds(),
                    60,
                    delta=3,
                )

    def test_invalid_schema_and_inconsistent_blocking_rejected(self):
        for changes in [
            {"extra": 1},
            {"complete": "yes"},
            {"summary": "x" * 12001},
            {
                "findings": [
                    {
                        "path": "../secret",
                        "line": 1,
                        "body": "x",
                        "severity": "blocking",
                    }
                ]
            },
            {"findings": [{"path": "a.py", "line": 1, "body": "x", "severity": "blocking"}]},
        ]:
            with (
                self.subTest(changes=str(changes)[:60]),
                self.assertRaises(providers.ProviderError) as raised,
            ):
                self.call(self.output(copy.deepcopy(RESULT) | changes))
            self.assertEqual(raised.exception.kind, "invalid")

    def test_cancellation_before_and_after_request(self):
        for callback in [lambda: True, iter([False, True]).__next__]:
            with self.assertRaises(providers.ReviewCancelled):
                self.call(self.output(), cancelled=callback)

    def test_incomplete_response_does_not_pass(self):
        with self.assertRaises(providers.ProviderError):
            self.call(self.output() | {"status": "incomplete"})

    def test_timeout_is_sanitized_availability(self):
        provider = SimpleNamespace(
            kind="openai",
            model="exact",
            credential=SimpleNamespace(encrypted_value="encrypted"),
        )
        with (
            patch.object(providers, "decrypt", return_value="secret-key"),
            patch.object(providers.httpx, "Client", side_effect=httpx.ReadTimeout("secret-key")),
            self.assertRaises(providers.ProviderError) as raised,
        ):
            providers.review(provider, {}, lambda: False)
        self.assertEqual(raised.exception.kind, "unavailable")
        self.assertNotIn("secret-key", str(raised.exception))

    def test_malformed_protocol_fields_are_sanitized(self):
        for data in [
            {"error": {"type": []}},
            {"stop_reason": []},
            {"output": [{"type": "message", "content": None}], "status": "completed"},
        ]:
            with (
                self.subTest(data=data),
                self.assertRaises(providers.ProviderError) as raised,
            ):
                self.call(data)
            self.assertEqual(raised.exception.kind, "invalid")

    def test_unknown_quota_reset_defaults_to_thirty_minutes(self):
        with self.assertRaises(providers.ProviderError) as raised:
            self.call({}, 429)
        self.assertAlmostEqual(
            (raised.exception.retry_at - datetime.now(timezone.utc)).total_seconds(),
            1800,
            delta=3,
        )

    def test_retry_after_http_date(self):
        retry = providers._retry_at({"retry-after": "Wed, 31 Dec 2098 23:00:00 GMT"})
        self.assertEqual(retry.year, 2098)

    def test_caller_message_cannot_expose_secret(self):
        error = providers.ProviderError("invalid", "secret-key")
        self.assertNotIn("secret-key", str(error))
        self.assertIsNone(error.retry_at)

    def test_non_json_context_is_invalid_not_auth_failure(self):
        provider = SimpleNamespace(
            kind="openai",
            model="exact",
            credential=SimpleNamespace(encrypted_value="encrypted"),
        )
        with self.assertRaises(providers.ProviderError) as raised:
            providers.review(provider, {"data": object()}, lambda: False)
        self.assertEqual(raised.exception.kind, "invalid")

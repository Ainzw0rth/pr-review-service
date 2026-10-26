"""Wire boundary rejects malformed work before any executor is invoked."""

import json
import uuid
from unittest import TestCase
from unittest.mock import patch

from execution import protocol


class ExecutionContractTests(TestCase):
    def request(self):
        return {
            "version": 1,
            "request_id": str(uuid.uuid4()),
            "operation": "model",
            "payload": {"context": {"head_sha": "a" * 40}},
        }

    def test_request_round_trip_is_detached_from_callers_state(self):
        original = self.request()
        parsed = protocol.decode_request(json.dumps(original).encode())
        original["payload"]["context"].clear()
        self.assertEqual(parsed.payload["context"]["head_sha"], "a" * 40)
        self.assertEqual(json.loads(parsed.encode())["request_id"], original["request_id"])

    def test_invalid_envelopes_fail_closed(self):
        for change in (
            {"version": True},
            {"version": 2},
            {"operation": "shell"},
            {"request_id": "invalid"},
            {"payload": []},
            {"credential": "secret"},
        ):
            with self.subTest(change=change), self.assertRaises(protocol.ProtocolError):
                protocol.decode_request(json.dumps({**self.request(), **change}).encode())

    def test_duplicate_fields_nonfinite_numbers_and_invalid_encoding_rejected(self):
        for body in (b'{"version":1,"version":2}', b'{"value":NaN}', b'{"value":1e999}', b"\xff"):
            with self.subTest(body=body), self.assertRaises(protocol.ProtocolError):
                protocol.decode_request(body)

    def test_size_checked_before_parse(self):
        with patch.object(protocol, "MAX_ENVELOPE_BYTES", 1024):
            with self.assertRaises(protocol.ProtocolError):
                protocol.decode_request(b" " * 1025)

    def test_dispatch_only_calls_explicit_registered_operation(self):
        from execution.local import LocalExecutor

        seen = []
        executor = LocalExecutor(
            {"model": lambda payload, cancelled: seen.append(payload) or {"ok": True}}
        )
        request = protocol.decode_request(json.dumps(self.request()).encode())
        result = executor.execute(request, cancelled=lambda: False)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen, [request.payload])
        with self.assertRaises(protocol.ProtocolError):
            LocalExecutor({}).execute(request, cancelled=lambda: False)

    def test_cancelled_request_never_starts(self):
        from execution.local import ExecutionCancelled, LocalExecutor

        def forbidden(payload, cancelled):
            self.fail("Cancelled work executed")

        request = protocol.decode_request(json.dumps(self.request()).encode())
        with self.assertRaises(ExecutionCancelled):
            LocalExecutor({"model": forbidden}).execute(request, cancelled=lambda: True)


class LocalModelBoundaryTests(TestCase):
    def test_provider_input_is_detached_before_execution(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from core.providers import review

        context = {"files": [{"filename": "original.py"}]}
        provider = SimpleNamespace(kind="codex_cli", model="test-model")

        def execute(provider, evidence, cancelled, **kwargs):
            evidence["files"][0]["filename"] = "changed.py"
            return {"complete": True}

        with patch("core.providers._review", side_effect=execute):
            self.assertEqual(review(provider, context, lambda: False), {"complete": True})
        self.assertEqual(context["files"][0]["filename"], "original.py")

    def test_mid_execution_cancellation_does_not_return_result(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from core.providers import ReviewCancelled, review

        stopped = False

        def execute(*args, **kwargs):
            nonlocal stopped
            stopped = True
            return {"complete": True}

        with patch("core.providers._review", side_effect=execute):
            with self.assertRaises(ReviewCancelled):
                review(SimpleNamespace(kind="codex_cli", model="test"), {}, lambda: stopped)


class ExecutionResultTests(TestCase):
    def test_invalid_executor_output_is_rejected(self):
        from execution.local import LocalExecutor

        request = protocol.ExecutionRequest(str(uuid.uuid4()), "model", {})
        for output in ({"value": float("nan")}, {"value": object()}, []):
            with self.subTest(output=type(output)), self.assertRaises(protocol.ProtocolError):
                LocalExecutor({"model": lambda payload, stop: output}).execute(
                    request, cancelled=lambda: False
                )

    def test_result_does_not_retain_executor_mutable_state(self):
        from execution.local import LocalExecutor

        output = {"findings": []}
        request = protocol.ExecutionRequest(str(uuid.uuid4()), "model", {})
        result = LocalExecutor({"model": lambda payload, stop: output}).execute(
            request, cancelled=lambda: False
        )
        output["findings"].append("later mutation")
        self.assertEqual(result, {"findings": []})

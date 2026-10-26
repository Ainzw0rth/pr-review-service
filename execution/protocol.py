"""Bounded, versioned execution envelopes. No Django or credential dependency."""

import json
import math
import uuid
from dataclasses import dataclass

# Preserve the existing 128 MiB CLI context allowance plus envelope overhead.
MAX_ENVELOPE_BYTES = 129 * 1024 * 1024
OPERATIONS = frozenset({"model", "test", "graph"})


class ProtocolError(ValueError):
    """Invalid or unsupported wire input; messages never include input data."""


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ProtocolError("Duplicate field")
        result[key] = value
    return result


def _constant(value):
    raise ProtocolError("Non-finite number")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ProtocolError("Non-finite number")
    return number


@dataclass(frozen=True)
class ExecutionRequest:
    request_id: str
    operation: str
    payload: dict

    def encode(self):
        try:
            body = json.dumps(
                {
                    "version": 1,
                    "request_id": self.request_id,
                    "operation": self.operation,
                    "payload": self.payload,
                },
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise ProtocolError("Invalid request") from None
        if len(body) > MAX_ENVELOPE_BYTES:
            raise ProtocolError("Request too large")
        return body


def decode_request(body):
    if not isinstance(body, bytes) or len(body) > MAX_ENVELOPE_BYTES:
        raise ProtocolError("Invalid request size")
    try:
        value = json.loads(
            body, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float
        )
        if not isinstance(value, dict) or set(value) != {
            "version",
            "request_id",
            "operation",
            "payload",
        }:
            raise ValueError()
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError()
        identifier = value["request_id"]
        if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
            raise ValueError()
        if not isinstance(value["operation"], str) or value["operation"] not in OPERATIONS:
            raise ValueError()
        if not isinstance(value["payload"], dict):
            raise ValueError()
        return ExecutionRequest(identifier, value["operation"], value["payload"])
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
        raise ProtocolError("Invalid request") from None


def result_snapshot(value):
    """Validate transportability without replacing operation-specific validators."""
    if not isinstance(value, dict):
        raise ProtocolError("Invalid result")
    try:
        body = json.dumps(value, allow_nan=False).encode()
        if len(body) > MAX_ENVELOPE_BYTES:
            raise ProtocolError("Result too large")
        return json.loads(body, parse_float=_float)
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
        raise ProtocolError("Invalid result") from None

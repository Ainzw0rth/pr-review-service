"""Explicit local dispatch; callbacks supply execution without ORM dependencies."""

from .protocol import ProtocolError, decode_request, result_snapshot


class ExecutionCancelled(Exception):
    pass


class LocalExecutor:
    def __init__(self, handlers):
        self.handlers = dict(handlers)

    def execute(self, request, *, cancelled):
        # Validate and detach even locally: mutable input must not bypass wire checks.
        request = decode_request(request.encode())
        if cancelled():
            raise ExecutionCancelled()
        handler = self.handlers.get(request.operation)
        if handler is None:
            raise ProtocolError("Unsupported operation")
        result = handler(request.payload, cancelled)
        if cancelled():
            raise ExecutionCancelled()
        return result_snapshot(result)

"""Bearer-authenticated worker control API; never accepts dashboard sessions."""

import hashlib
import json
from datetime import timedelta

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables

from . import worker_registry as registry
from .models import LoginAttempt

MAX_BODY = 65536


def _response(value, status=200):
    response = JsonResponse(value, status=status, json_dumps_params={"separators": (",", ":")})
    response["Cache-Control"] = "no-store"
    return response


def _budget(request, action, identity=None):
    # Direct peer only: forwarded headers are not an authentication authority.
    identity = identity or request.META.get("REMOTE_ADDR", "unknown")
    key = hashlib.sha256(f"worker:{action}:{identity}".encode()).hexdigest()
    now = timezone.now()
    with transaction.atomic():
        row, _ = LoginAttempt.objects.get_or_create(key=key, defaults={"window_at": now})
        row = LoginAttempt.objects.select_for_update().get(pk=row.pk)
        if row.window_at <= now - timedelta(minutes=15):
            row.window_at, row.count = now, 0
        if row.count >= (10 if action.endswith("enroll") else 12000 if action == "event" else 1200):
            return False
        row.count += 1
        row.save(update_fields=["count", "window_at"])
    return True


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _constant(value):
    raise ValueError()


def _limited():
    response = _response({"error": "rate_limited"}, 429)
    response["Retry-After"] = "900"
    return response


def _unauthorized(request, action):
    if not _budget(request, "failed-" + action):
        return _limited()
    return _response({"error": "unauthorized"}, 401)


@csrf_exempt
@sensitive_variables()
def worker_endpoint(request, action):
    # CSRF exemption applies only to these non-cookie authenticated endpoints.
    if request.method != "POST":
        response = _response({"error": "method_not_allowed"}, 405)
        response["Allow"] = "POST"
        return response
    if not request.is_secure():
        return _response({"error": "https_required"}, 403)
    worker = None
    if action != "enroll":
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return _unauthorized(request, action)
        try:
            worker = registry.authenticate(authorization[7:])
        except registry.WorkerAuthError:
            return _unauthorized(request, action)
    if worker is not None and not _budget(request, action, str(worker.pk)):
        return _limited()
    if request.content_type != "application/json":
        return _response({"error": "json_required"}, 415)
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
        if length < 0:
            raise ValueError()
        if length > MAX_BODY:
            return _response({"error": "body_too_large"}, 413)
        body = request.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            return _response({"error": "body_too_large"}, 413)
        data = json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant)
        if not isinstance(data, dict):
            raise ValueError()
        if action == "enroll":
            if set(data) != {"code", "name"}:
                raise ValueError()
            if not registry.enrollment_available(data["code"]):
                return _unauthorized(request, action)
            if not _budget(request, "enroll", data["code"]):
                return _limited()
            worker, token = registry.enroll(data["code"], data["name"])
            return _response({"version": 1, "worker_id": str(worker.pk), "token": token}, 201)
        if action != "heartbeat":
            from . import execution_dispatch as dispatch

            try:
                if action == "claim":
                    if set(data) != {"incarnation"}:
                        raise ValueError()
                    return _response({"assignment": dispatch.take(worker, data["incarnation"])})
                handlers = {
                    "input": dispatch.input_payload,
                    "event": dispatch.append_output,
                    "renew": dispatch.renew,
                    "complete": dispatch.complete,
                }
                if action not in handlers:
                    raise ValueError()
                return _response(handlers[action](worker, data))
            except dispatch.AssignmentConflict:
                return _response({"error": "stale_assignment"}, 409)
        registry.heartbeat(worker, data)
        worker.refresh_from_db()
        return _response(
            {
                "version": 1,
                "heartbeat_seconds": registry.HEARTBEAT_SECONDS,
                "draining": worker.draining,
            }
        )
    except registry.WorkerAuthError:
        return _unauthorized(request, action)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return _response({"error": "invalid_request"}, 400)

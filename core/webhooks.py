import hashlib
import hmac
import json
import re

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.models import Repository, Webhook
from core.secrets import decrypt


@csrf_exempt
@require_POST
def github_webhook(request, repository_id):
    repo = (
        Repository.objects.filter(pk=repository_id, enabled=True)
        .select_related("webhook_credential")
        .first()
    )
    if not repo:
        return JsonResponse({"error": "Unknown connection"}, status=404)
    if int(request.META.get("CONTENT_LENGTH") or 0) > 5 * 1024 * 1024:
        return JsonResponse({"error": "Payload too large"}, status=413)
    body = request.body
    signature = (
        "sha256="
        + hmac.new(
            decrypt(repo.webhook_credential.encrypted_value).encode(), body, hashlib.sha256
        ).hexdigest()
    )
    if not hmac.compare_digest(signature, request.headers.get("X-Hub-Signature-256", "")):
        return JsonResponse({"error": "Invalid signature"}, status=403)
    delivery = request.headers.get("X-GitHub-Delivery", "")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", delivery):
        return JsonResponse({"error": "Invalid delivery"}, status=400)
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
        if (
            payload.get("repository", {}).get("full_name", "").casefold()
            != repo.full_name.casefold()
        ):
            return JsonResponse({"error": "Repository mismatch"}, status=400)
        event = request.headers.get("X-GitHub-Event", "")
        if event not in ["pull_request", "ping"]:
            return JsonResponse({"ignored": True})
        if event == "pull_request":
            pr = payload["pull_request"]
            if (
                not isinstance(payload["number"], int)
                or payload["number"] < 1
                or not re.fullmatch(r"[a-f0-9]{40,64}", pr["head"]["sha"])
            ):
                raise ValueError
            if not isinstance(payload.get("sender"), dict):
                raise ValueError
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"error": "Invalid payload"}, status=400)
    if event == "ping":
        return JsonResponse({"ok": True})
    # Retain event control metadata, not PR descriptions/source or actor profiles.
    payload = {
        "action": payload.get("action"),
        "number": payload["number"],
        "pull_request": {
            "number": payload["number"],
            "head": {"sha": payload["pull_request"]["head"]["sha"]},
        },
        "label": {"name": payload.get("label", {}).get("name", "")},
        "requested_reviewer": {"login": (payload.get("requested_reviewer") or {}).get("login", "")},
        "sender": {
            "login": payload["sender"].get("login", ""),
            "type": payload["sender"].get("type", ""),
        },
    }
    _, created = Webhook.objects.get_or_create(
        delivery=delivery, defaults={"repository": repo, "event": event, "payload": payload}
    )
    return JsonResponse({"accepted": True, "duplicate": not created}, status=202)

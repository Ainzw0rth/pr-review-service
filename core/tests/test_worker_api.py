import json

from django.test import Client, TestCase, override_settings

from core import worker_registry as registry


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkerAPITests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def post(self, route, body, token=None):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + token} if token else {}
        return self.client.post(
            "/api/workers/v1/" + route + "/",
            data=json.dumps(body),
            content_type="application/json",
            secure=True,
            **headers,
        )

    def test_enrollment_requires_https_and_one_use_code(self):
        code = registry.create_enrollment()
        response = self.post("enroll", {"code": code, "name": "Remote"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response["Cache-Control"], "no-store")
        token = response.json()["token"]
        self.assertEqual(str(registry.authenticate(token).pk), response.json()["worker_id"])
        self.assertEqual(self.post("enroll", {"code": code, "name": "Again"}).status_code, 401)

    def test_plain_http_rejected(self):
        response = self.client.post(
            "/api/workers/v1/enroll/", data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)

    def test_heartbeat_requires_worker_credential_not_browser_session(self):
        self.assertEqual(self.post("heartbeat", {}).status_code, 401)
        worker, token = registry.enroll(registry.create_enrollment(), "Remote")
        report = {
            "version": 1,
            "incarnation": "a" * 32,
            "slots": 1,
            "active": 0,
            "capabilities": ["model"],
            "authenticated": True,
        }
        response = self.post("heartbeat", report, token)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(token, response.content.decode())
        registry.revoke(worker.pk)
        self.assertEqual(self.post("heartbeat", report, token).status_code, 401)

    def test_invalid_and_oversized_requests_rejected(self):
        for body, expected in (("not-json", 400), (" " * 65537, 413), ('{"code":1,"code":2}', 400)):
            response = self.client.post(
                "/api/workers/v1/enroll/", data=body, content_type="application/json", secure=True
            )
            self.assertEqual(response.status_code, expected)

    def test_enrollment_attempts_are_rate_limited(self):
        for _ in range(10):
            self.post("enroll", {"code": "x" * 43, "name": "Remote"})
        self.assertEqual(self.post("enroll", {"code": "x" * 43, "name": "Remote"}).status_code, 429)

    def test_invalid_enrollment_flood_cannot_block_valid_pairing(self):
        for _ in range(11):
            self.post("enroll", {"code": "x" * 43, "name": "Invalid"})
        response = self.post("enroll", {"code": registry.create_enrollment(), "name": "Valid"})
        self.assertEqual(response.status_code, 201)

    def test_invalid_auth_flood_cannot_block_valid_heartbeat(self):
        from core.models import LoginAttempt

        self.post("heartbeat", {})
        LoginAttempt.objects.update(count=1200)
        worker, token = registry.enroll(registry.create_enrollment(), "Valid")
        response = self.post(
            "heartbeat",
            {
                "version": 1,
                "incarnation": "a" * 32,
                "slots": 1,
                "active": 0,
                "capabilities": ["model"],
                "authenticated": True,
            },
            token,
        )
        self.assertEqual(response.status_code, 200)

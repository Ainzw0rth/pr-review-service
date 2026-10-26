import base64

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core import worker_registry as registry
from core.models import WorkerEnrollment, WorkerOwner


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkersUITests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="fleet-owner", is_superuser=True)
        self.setting = override_settings(DASHBOARD_RETRY_USER_ID=self.owner.pk)
        self.setting.enable()
        self.addCleanup(self.setting.disable)
        self.client.force_login(self.owner)
        self.worker, self.token = registry.enroll(registry.create_enrollment(), "InstaBox")

    def action(self, action, **kwargs):
        return self.client.post(
            reverse("worker_action"), {"action": action, "worker_id": str(self.worker.pk), **kwargs}
        )

    def test_local_inventory_and_remote_unknown_metrics(self):
        WorkerOwner.objects.create()
        response = self.client.get(reverse("workers"))
        self.assertContains(response, "Local worker")
        self.assertContains(response, "InstaBox")
        state = self.client.get(reverse("workers_state")).json()
        remote = next(w for w in state["workers"] if w["id"] == str(self.worker.pk))
        self.assertEqual(remote["memory"], "Unknown")
        self.assertEqual(remote["available"], 0)
        self.assertNotIn(self.token, str(state))
        self.assertNotIn(self.worker.credential_digest, str(state))
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_pairing_only_post_and_shown_once(self):
        response = self.client.post(reverse("worker_action"), {"action": "pair"})
        code = response.context["pairing_code"]
        self.assertContains(response, code)
        self.assertNotContains(self.client.get(reverse("workers")), code)
        self.assertNotIn(code, self.client.get(reverse("workers_state")).content.decode())
        self.assertFalse(WorkerEnrollment.objects.filter(digest=code).exists())

    def test_resume_never_enables_unvalidated_worker(self):
        self.assertEqual(self.action("drain").status_code, 302)
        self.worker.refresh_from_db()
        self.assertTrue(self.worker.draining)
        self.assertEqual(self.action("resume").status_code, 302)
        self.worker.refresh_from_db()
        self.assertFalse(self.worker.draining)
        self.assertFalse(self.worker.enabled)

    def test_configuration_validates_account_and_slot_boundaries(self):
        self.assertEqual(
            self.action(
                "configure", enabled="on", capabilities=["model"], max_slots="1"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.action("configure", capabilities=["shell"], max_slots="1").status_code, 400
        )
        self.assertEqual(
            self.action("configure", capabilities=["test"], max_slots="33").status_code, 400
        )
        self.assertEqual(
            self.action(
                "configure",
                enabled="on",
                capabilities=["model"],
                account_kinds=["codex_cli"],
                max_slots="1",
            ).status_code,
            302,
        )
        self.worker.refresh_from_db()
        self.assertTrue(self.worker.enabled)

    def test_revocation_blocks_resume_and_invalidates_credential(self):
        self.assertEqual(self.action("revoke").status_code, 302)
        self.assertEqual(self.action("resume").status_code, 400)
        with self.assertRaises(registry.WorkerAuthError):
            registry.authenticate(self.token)

    def test_invalid_worker_identifier_is_bad_request(self):
        self.assertEqual(
            self.client.post(
                reverse("worker_action"), {"action": "drain", "worker_id": "invalid"}
            ).status_code,
            400,
        )

    @override_settings(CREDENTIAL_KEY=Fernet.generate_key().decode())
    def test_keepalive_configuration_redacts_secret_and_manual_check(self):
        key = (
            "ssh-ed25519 "
            + base64.b64encode(
                b"\x00\x00\x00\x0bssh-ed25519" + b"\x00\x00\x00\x20" + b"a" * 32
            ).decode()
        )
        self.assertEqual(
            self.action(
                "keepalive",
                enabled="on",
                host="example.test",
                port="2222",
                user="private-ssh-token",
                known_host_key=key,
                interval_minutes="20",
            ).status_code,
            302,
        )
        self.assertEqual(
            self.action(
                "keepalive",
                enabled="on",
                host="",
                port="",
                user="",
                known_host_key="",
                interval_minutes="20",
            ).status_code,
            302,
        )
        for name in ("workers", "workers_state"):
            body = self.client.get(reverse(name)).content.decode()
            for secret in ("private-ssh-token", "example.test", key):
                self.assertNotIn(secret, body)
        self.assertEqual(self.action("check").status_code, 302)

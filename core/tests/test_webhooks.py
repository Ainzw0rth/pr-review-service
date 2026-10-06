import hashlib
import hmac
import json

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings

from core.models import Credential, Organization, Repository, Webhook
from core.secrets import encrypt


@override_settings(SECURE_SSL_REDIRECT=False, CREDENTIAL_KEY=Fernet.generate_key().decode())
class WebhookTests(TestCase):
    def setUp(self):
        c = Credential.objects.create(name="token", encrypted_value=encrypt("secret"))
        self.repo = Repository.objects.create(
            organization=Organization.objects.create(name="org", slug="org"),
            full_name="org/repo",
            reviewer="louis",
            github_credential=c,
            webhook_credential=c,
        )
        self.payload = {
            "repository": {"full_name": "org/repo"},
            "action": "labeled",
            "number": 1,
            "pull_request": {"number": 1, "head": {"sha": "a" * 40}},
            "sender": {"login": "dev", "type": "User"},
            "label": {"name": "on review"},
        }

    def send(self, valid=True):
        body = json.dumps(self.payload).encode()
        sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        return self.client.post(
            f"/webhooks/github/{self.repo.pk}/",
            body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sig if valid else "bad",
            HTTP_X_GITHUB_DELIVERY="delivery-1",
            HTTP_X_GITHUB_EVENT="pull_request",
        )

    def test_signature_is_required(self):
        self.assertEqual(self.send(False).status_code, 403)
        self.assertFalse(Webhook.objects.exists())

    def test_delivery_deduplicated(self):
        self.assertEqual(self.send().status_code, 202)
        self.assertEqual(self.send().status_code, 202)
        self.assertEqual(Webhook.objects.count(), 1)

    def test_other_repo_cannot_cross_connection(self):
        self.payload["repository"]["full_name"] = "other/private"
        self.assertEqual(self.send().status_code, 400)
        self.assertFalse(Webhook.objects.exists())

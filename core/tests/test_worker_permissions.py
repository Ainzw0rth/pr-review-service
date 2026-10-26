from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkerPermissionTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="owner", is_superuser=True)
        self.other = get_user_model().objects.create_user(username="other", is_superuser=True)
        self.setting = override_settings(DASHBOARD_RETRY_USER_ID=self.owner.pk)
        self.setting.enable()
        self.addCleanup(self.setting.disable)

    def test_anonymous_and_other_admin_cannot_read_or_pair(self):
        for user in (None, self.other):
            if user:
                self.client.force_login(user)
            for name in ("workers", "workers_state"):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)
            self.assertEqual(
                self.client.post(reverse("worker_action"), {"action": "pair"}).status_code, 403
            )

    def test_owner_post_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(reverse("worker_action"), {"action": "pair"}).status_code, 403)
        self.assertEqual(client.get(reverse("worker_action")).status_code, 405)

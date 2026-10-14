from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core.tests.test_workflow import WorkflowTests


@override_settings(SECURE_SSL_REDIRECT=False, DASHBOARD_RETRY_USER_ID=1)
class RelatedSettingsTests(TestCase):
    def setUp(self):
        WorkflowTests.setUp(self)
        self.owner = get_user_model().objects.create_user(pk=1, username="owner", is_superuser=True)
        self.client.force_login(self.owner)

    def save(self, **fields):
        version = self.client.get("/settings/").context["version"]
        return self.client.post("/settings/", {"version": version, **fields})

    def test_numeric_unlimited_and_back(self):
        self.assertEqual(self.repo.related_item_limit, 50)
        for fields, expected in [
            ({f"related_item_limit_{self.repo.pk}": "75"}, 75),
            ({f"related_unlimited_{self.repo.pk}": "on"}, None),
            ({f"related_item_limit_{self.repo.pk}": "30"}, 30),
        ]:
            self.assertEqual(self.save(**fields).status_code, 302)
            self.repo.refresh_from_db()
            self.assertEqual(self.repo.related_item_limit, expected)

    def test_invalid_limit_does_not_change_other_policies(self):
        for value in ["0", "-1", "abc", "1.5", "", "2147483648"]:
            with self.subTest(value=value):
                response = self.save(
                    **{
                        f"related_item_limit_{self.repo.pk}": value,
                        f"auto_approve_{self.repo.pk}": "on",
                    }
                )
                self.assertEqual(response.status_code, 400)
                self.repo.refresh_from_db()
                self.assertFalse(self.repo.auto_approve)

    def test_limit_participates_in_stale_form_detection_and_owner_gate(self):
        version = self.client.get("/settings/").context["version"]
        self.save(**{f"related_item_limit_{self.repo.pk}": "75"})
        self.assertEqual(self.client.post("/settings/", {"version": version}).status_code, 409)
        other = get_user_model().objects.create_user(username="other", is_superuser=True)
        self.client.force_login(other)
        self.assertEqual(self.client.get("/settings/").status_code, 403)
        self.assertEqual(
            self.client.post("/settings/", {f"related_unlimited_{self.repo.pk}": "on"}).status_code,
            403,
        )

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Job
from core.tests import test_dashboard


@override_settings(SECURE_SSL_REDIRECT=False)
class RetryTests(TestCase):
    def setUp(self):
        test_dashboard.DashboardTests.setUp(self)
        self.owner = get_user_model().objects.create_superuser(
            "owner", password="Owner-browser-test-9281"
        )
        self.other_admin = get_user_model().objects.create_superuser(
            "other", password="Other-browser-test-9281"
        )
        self.setting = override_settings(DASHBOARD_RETRY_USER_ID=self.owner.pk)
        self.setting.enable()
        self.addCleanup(self.setting.disable)
        self.visible.status = "attention"
        self.visible.save()
        p = self.visible.pr
        p.generation = 1
        p.requested = True
        p.labels = ["on review"]
        p.canonical_trigger = "10"
        p.save()
        self.url = f"/jobs/{self.visible.pk}/retry/"
        self.snapshot = {
            "number": 17,
            "state": "open",
            "head": {"sha": "a" * 40},
            "labels": [{"name": "on review"}],
            "requested_reviewers": [{"login": "louis"}],
        }

    def test_members_and_other_admins_cannot_retry(self):
        for user in [self.user, self.other_admin]:
            self.client.force_login(user)
            self.assertEqual(self.client.post(self.url).status_code, 403)
            self.assertNotContains(
                self.client.get(reverse("job_detail", args=[self.visible.pk])), "Retry review"
            )

    def test_owner_only_post_and_csrf(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.url).status_code, 403)
        self.assertContains(
            self.client.get(reverse("job_detail", args=[self.visible.pk])), "Retry review"
        )

    def retry(self):
        self.client.force_login(self.owner)
        with (
            patch("core.github.GitHub") as cls,
            patch("core.workflow.canonical_cycle", return_value={"key": "10", "eligible": True}),
        ):
            cls.return_value.pull.return_value = self.snapshot
            response = self.client.post(self.url)
            self.assertFalse(cls.return_value.post.called)
            return response

    def test_retry_creates_fresh_attempt_and_duplicate_click_is_safe(self):
        response = self.retry()
        self.assertEqual(response.status_code, 302)
        fresh = Job.objects.get(pr=self.visible.pr, generation=2)
        self.assertEqual(fresh.status, "queued")
        self.assertIsNone(fresh.provider_id)
        self.assertEqual(fresh.actor, "owner (dashboard retry)")
        self.assertEqual(fresh.retry_of_id, self.visible.pk)
        self.assertEqual(self.retry().status_code, 302)
        self.assertEqual(Job.objects.filter(pr=self.visible.pr).count(), 2)

    def test_changed_commit_or_missing_label_prevents_retry(self):
        for change in [{"head": {"sha": "b" * 40}}, {"labels": []}]:
            old = self.snapshot.copy()
            self.snapshot.update(change)
            self.assertEqual(self.retry().status_code, 302)
            self.assertEqual(Job.objects.filter(pr=self.visible.pr).count(), 1)
            self.snapshot = old

    def test_published_or_running_attempt_cannot_retry(self):
        from core.models import Receipt

        self.visible.status = "running"
        self.visible.save()
        self.retry()
        self.assertEqual(Job.objects.filter(pr=self.visible.pr).count(), 1)
        self.visible.status = "attention"
        self.visible.save()
        Receipt.objects.create(job=self.visible, key="review")
        self.retry()
        self.assertEqual(Job.objects.filter(pr=self.visible.pr).count(), 1)

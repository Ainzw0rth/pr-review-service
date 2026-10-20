from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from core.claims import release
from core.models import JobClaim, ServiceSettings
from core.tests import test_claims
from core.workflow import claim, finish


@override_settings(SECURE_SSL_REDIRECT=False, DASHBOARD_RETRY_USER_ID=1)
class ConcurrencySettingsTests(TestCase):
    job = test_claims.ClaimTests.job

    def setUp(self):
        test_claims.ClaimTests.setUp(self)
        self.owner = get_user_model().objects.create_user(pk=1, username="owner", is_superuser=True)
        self.client.force_login(self.owner)
        self.service = ServiceSettings.objects.get(pk=1)

    def save(self, **fields):
        version = self.client.get("/settings/").context["version"]
        return self.client.post("/settings/", {"version": version, **fields})

    def test_current_value_and_valid_bounds_preserve_drain_state(self):
        self.service.draining = True
        self.service.drain_generation = 7
        self.service.save()
        for value in ("1", "8", "2"):
            with self.subTest(value=value):
                response = self.save(concurrent_pr_reviews=value)
                self.assertEqual(response.status_code, 302)
                self.service.refresh_from_db()
                self.assertEqual(self.service.concurrent_pr_reviews, int(value))
                self.assertTrue(self.service.draining)
                self.assertEqual(self.service.drain_generation, 7)
                page = self.client.get("/settings/")
                self.assertContains(page, f'name="concurrent_pr_reviews" value="{value}"')

    def test_invalid_capacity_never_changes_any_repository_policy(self):
        for value in ("0", "-1", "1.5", "", "9", "bad", " 2", "2 ", "+2", "٢"):
            with self.subTest(value=value):
                response = self.save(
                    concurrent_pr_reviews=value,
                    **{
                        f"auto_approve_{self.repo.pk}": "on",
                        f"auto_merge_{self.repo.pk}": "on",
                        f"merge_method_{self.repo.pk}": "rebase",
                        f"related_item_limit_{self.repo.pk}": "75",
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, 'aria-invalid="true"', status_code=400)
                self.repo.refresh_from_db()
                self.service.refresh_from_db()
                self.assertEqual(self.service.concurrent_pr_reviews, 2)
                self.assertFalse(self.repo.auto_approve)
                self.assertFalse(self.repo.auto_merge)
                self.assertEqual(self.repo.merge_method, "squash")
                self.assertEqual(self.repo.related_item_limit, 50)

    def test_invalid_repository_policy_does_not_change_capacity(self):
        response = self.save(
            concurrent_pr_reviews="8", **{f"related_item_limit_{self.repo.pk}": "0"}
        )
        self.assertEqual(response.status_code, 400)
        self.service.refresh_from_db()
        self.assertEqual(self.service.concurrent_pr_reviews, 2)

    def test_capacity_only_change_rejects_stale_tab(self):
        version = self.client.get("/settings/").context["version"]
        self.assertEqual(self.save(concurrent_pr_reviews="3").status_code, 302)
        response = self.client.post(
            "/settings/",
            {
                "version": version,
                "concurrent_pr_reviews": "1",
                f"auto_approve_{self.repo.pk}": "on",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.service.refresh_from_db()
        self.repo.refresh_from_db()
        self.assertEqual(self.service.concurrent_pr_reviews, 3)
        self.assertFalse(self.repo.auto_approve)

    def test_omitted_capacity_preserves_saved_value(self):
        self.service.concurrent_pr_reviews = 5
        self.service.save()
        self.assertEqual(self.save(**{f"auto_approve_{self.repo.pk}": "on"}).status_code, 302)
        self.service.refresh_from_db()
        self.repo.refresh_from_db()
        self.assertEqual(self.service.concurrent_pr_reviews, 5)
        self.assertTrue(self.repo.auto_approve)

    def test_owner_and_csrf_required(self):
        other = get_user_model().objects.create_user(pk=2, username="other", is_superuser=True)
        self.client.force_login(other)
        self.assertEqual(self.client.get("/settings/").status_code, 403)
        self.assertEqual(
            self.client.post("/settings/", {"concurrent_pr_reviews": "8"}).status_code, 403
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(
            csrf_client.post("/settings/", {"concurrent_pr_reviews": "8"}).status_code, 403
        )
        self.service.refresh_from_db()
        self.assertEqual(self.service.concurrent_pr_reviews, 2)

    def test_saved_capacity_controls_new_claims_without_interrupting_cleanup(self):
        for number in range(1, 5):
            self.job(number)
        first, second = claim(), claim()
        self.addCleanup(release, first)
        self.addCleanup(release, second)
        self.assertEqual(self.save(concurrent_pr_reviews="3").status_code, 302)
        third = claim()
        self.assertIsNotNone(third)
        self.addCleanup(release, third)
        self.assertEqual(self.save(concurrent_pr_reviews="1").status_code, 302)
        self.assertEqual(JobClaim.objects.count(), 3)
        for job in (first, second, third):
            job.refresh_from_db()
            self.assertEqual(job.status, "running")
        self.assertIsNone(claim())
        finish(first, "completed")
        release(second)
        release(third)
        self.assertIsNone(claim())  # Terminal job still owns its slot through cleanup.
        release(first)
        fourth = claim()
        self.assertIsNotNone(fourth)
        release(fourth)

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core.tests.test_publication import PublicationTests


@override_settings(SECURE_SSL_REDIRECT=False, DASHBOARD_RETRY_USER_ID=1)
class ApprovalSettingsTests(TestCase):
    def test_settings_are_owner_only_and_default_off(self):
        from core.tests.test_workflow import WorkflowTests

        WorkflowTests.setUp(self)
        owner = get_user_model().objects.create_user(pk=1, username="owner", is_superuser=True)
        other = get_user_model().objects.create_user(username="other", is_superuser=True)
        self.client.force_login(other)
        self.assertEqual(self.client.get("/settings/").status_code, 403)
        self.assertEqual(self.client.post("/settings/", {}).status_code, 403)
        self.client.force_login(owner)
        response = self.client.get("/settings/")
        self.assertEqual(response.status_code, 200)
        self.repo.refresh_from_db()
        self.assertFalse(self.repo.auto_approve)
        version = response.context["version"]
        response = self.client.post(
            "/settings/", {"version": version, f"auto_approve_{self.repo.pk}": "on"}
        )
        self.assertEqual(response.status_code, 302)
        self.repo.refresh_from_db()
        self.assertTrue(self.repo.auto_approve)
        self.assertEqual(self.client.post("/settings/", {"version": version}).status_code, 409)


@override_settings(LIVE_WRITES=True)
class AutomaticApprovalTests(PublicationTests):
    def test_enabled_passing_review_formally_approves_without_merge(self):
        from core.publication import publish

        self.repo.auto_approve = True
        self.repo.save()
        job, gh = self.prepare()
        gh.snapshot["user"] = {"login": "developer"}
        self.assertTrue(job.auto_approve)
        self.assertEqual(
            publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"}), "ready"
        )
        reviews = [data for path, data in gh.writes if path.endswith("/reviews")]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["event"], "APPROVE")
        self.assertEqual(reviews[0]["commit_id"], job.head_sha)
        self.assertFalse(any("merge" in path for path, _ in gh.writes))

    def test_disabling_prevents_approval_for_already_queued_job(self):
        from core.publication import publish

        self.repo.auto_approve = True
        self.repo.save()
        job, gh = self.prepare()
        self.repo.auto_approve = False
        self.repo.save()
        publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        self.assertFalse(any(data.get("event") == "APPROVE" for _, data in gh.writes))

    def test_own_pr_cannot_receive_formal_approval(self):
        from core.github import IntegrationError
        from core.publication import publish

        self.repo.auto_approve = True
        self.repo.save()
        job, gh = self.prepare()
        gh.snapshot["user"] = {"login": "louis"}
        with self.assertRaisesRegex(IntegrationError, "own PR"):
            publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        self.assertFalse(any(data.get("event") == "APPROVE" for _, data in gh.writes))

    def test_new_commit_dismisses_only_recorded_service_approval_even_toggle_off(self):
        from core.approvals import dismiss_stale
        from core.models import ApprovalRecord
        from core.publication import publish

        self.repo.auto_approve = True
        self.repo.save()
        job, gh = self.prepare()
        gh.snapshot["user"] = {"login": "developer"}
        publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        gh.reviews.append(
            {
                "id": 999,
                "state": "APPROVED",
                "user": {"login": "human"},
                "commit_id": job.head_sha,
                "body": "Looks good",
            }
        )
        gh.snapshot["head"]["sha"] = "b" * 40
        self.repo.auto_approve = False
        self.repo.save()
        with patch.object(gh, "put", create=True, return_value={"state": "DISMISSED"}) as put:
            dismiss_stale(gh, self.repo, job.pr.number)
        self.assertEqual(put.call_count, 1)
        self.assertNotIn("/999/", put.call_args.args[0])
        self.assertIsNotNone(ApprovalRecord.objects.get(job=job).dismissed_at)

    def test_blocked_review_never_approves_when_enabled(self):
        from core.publication import publish

        self.repo.auto_approve = True
        self.repo.save()
        job, gh = self.prepare()
        gh.snapshot["user"] = {"login": "developer"}
        outcome = publish(gh, job, self.result(), {}, {"passed": False, "summary": "CI failed"})
        self.assertEqual(outcome, "feedback")
        self.assertTrue(any(data.get("event") == "REQUEST_CHANGES" for _, data in gh.writes))
        self.assertFalse(any(data.get("event") == "APPROVE" for _, data in gh.writes))

    def test_existing_github_auto_merge_prevents_approval(self):
        from core.github import IntegrationError
        from core.publication import publish

        self.repo.auto_approve = True
        self.repo.save()
        job, gh = self.prepare()
        gh.snapshot["user"] = {"login": "developer"}
        gh.snapshot["auto_merge"] = {"merge_method": "squash"}
        with self.assertRaisesRegex(IntegrationError, "auto-merge"):
            publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        self.assertFalse(any(data.get("event") == "APPROVE" for _, data in gh.writes))

    def test_lost_approval_response_reconciles_without_duplicate(self):
        from core.delivery import deliver, save_review
        from core.github import IntegrationError
        from core.models import ApprovalRecord
        from core.publication import publish

        self.repo.auto_approve = True
        self.repo.save()
        job, gh = self.prepare()
        gh.snapshot["user"] = {"login": "developer"}
        context = {"files": [], "threads": [], "discussion": [], "issues": [], "related_prs": []}
        save_review(job, self.result(), context, {"passed": True, "summary": "Pass"})
        original = gh.post
        dropped = False

        def post(path, data):
            nonlocal dropped
            value = original(path, data)
            if data.get("event") == "APPROVE" and not dropped:
                dropped = True
                raise IntegrationError("GitHub request unavailable or malformed.")
            return value

        with (
            patch.object(gh, "post", side_effect=post),
            patch("core.delivery.verify_inputs"),
            patch("core.delivery.time.sleep"),
        ):
            self.assertEqual(deliver(gh, job, publish, lambda *args: None), "ready")
        self.assertEqual(sum(data.get("event") == "APPROVE" for _, data in gh.writes), 1)
        self.assertTrue(ApprovalRecord.objects.get(job=job).review_id)

    def test_approval_record_survives_job_cleanup_and_dismissal_is_retryable(self):
        from core.approvals import dismiss_stale
        from core.models import ApprovalRecord
        from core.publication import publish

        self.repo.auto_approve = True
        self.repo.save()
        job, gh = self.prepare()
        gh.snapshot["user"] = {"login": "developer"}
        publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        number = job.pr.number
        from core.claims import release

        release(job)
        job.delete()
        gh.snapshot["head"]["sha"] = "c" * 40
        with patch.object(gh, "put", create=True, side_effect=RuntimeError("private")):
            with self.assertRaises(RuntimeError):
                dismiss_stale(gh, self.repo, number)
        row = ApprovalRecord.objects.get()
        self.assertIsNone(row.job_id)
        self.assertIsNone(row.dismissed_at)
        self.assertNotIn("private", row.error)
        with patch.object(gh, "put", create=True, return_value={"state": "DISMISSED"}):
            dismiss_stale(gh, self.repo, number)
        row.refresh_from_db()
        self.assertIsNotNone(row.dismissed_at)
        self.assertEqual([label["name"] for label in gh.snapshot["labels"]], ["feedback"])

    def test_ci_regression_after_approval_withdraws_it(self):
        from core.models import ApprovalRecord
        from core.publication import LabelCleanupError, publish

        self.repo.auto_approve = True
        self.repo.required_checks = ["ci"]
        self.repo.save()
        job, gh = self.prepare()
        gh.snapshot["user"] = {"login": "developer"}
        with (
            patch("core.github.check_required", side_effect=[{"passed": True}, {"passed": False}]),
            patch.object(gh, "put", create=True, return_value={"state": "DISMISSED"}),
        ):
            with self.assertRaises(LabelCleanupError):
                publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        record = ApprovalRecord.objects.get(job=job)
        self.assertTrue(record.revoke_requested)
        self.assertIsNotNone(record.dismissed_at)
        self.assertNotIn("approved", [label["name"] for label in gh.snapshot["labels"]])

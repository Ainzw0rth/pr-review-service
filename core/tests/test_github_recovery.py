from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core import workflow
from core.models import JobClaim
from core.tests import test_workflow
from core.tests.test_publication import FakeGitHub
from core.worker import run_job


@override_settings(LIVE_WRITES=True)
class GitHubRecoveryTests(TestCase):
    setUp = test_workflow.WorkflowTests.setUp
    snapshot = test_workflow.WorkflowTests.snapshot
    event = test_workflow.WorkflowTests.event
    reconcile = test_workflow.WorkflowTests.reconcile

    def job(self):
        self.reconcile(self.event("labeled"), self.snapshot())
        return workflow.claim()

    def limit(self):
        from core.github import GitHubRateLimited

        return GitHubRateLimited(timezone.now() + timedelta(minutes=5), "primary")

    def test_startup_limit_requeues_without_feedback_or_terminal_timestamp(self):
        job, error = self.job(), self.limit()
        github = FakeGitHub(self.snapshot())
        with (
            patch("core.github.GitHub", return_value=github),
            patch("core.worker.guard", side_effect=error),
            patch("core.worker.once") as feedback,
            patch("core.worker.fail_to_feedback") as labels,
        ):
            run_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.next_retry_at, error.retry_at)
        self.assertIsNone(job.finished_at)
        self.assertIn("GitHub", job.reason)
        feedback.assert_not_called()
        labels.assert_not_called()
        self.assertFalse(JobClaim.objects.filter(job=job).exists())
        self.assertTrue(job.activity.filter(code="github_waiting").exists())

    def test_context_limit_requeues_without_model_or_feedback(self):
        job = self.job()
        with (
            patch("core.github.GitHub", return_value=FakeGitHub(self.snapshot())),
            patch("core.context.build_context", side_effect=self.limit()),
            patch("core.providers.review") as model,
            patch("core.worker.once") as feedback,
        ):
            run_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        model.assert_not_called()
        feedback.assert_not_called()

    def test_deferred_queued_job_not_claimed_before_retry_time(self):
        job = self.job()
        error = self.limit()
        workflow.finish(job, "queued", str(error), retry_at=error.retry_at)
        from core.claims import release

        release(job)
        self.assertIsNone(workflow.claim())
        with patch("core.claims.timezone.now", return_value=error.retry_at + timedelta(seconds=1)):
            resumed = workflow.claim()
        self.assertIsNotNone(resumed)
        release(resumed)

    def test_ordinary_403_has_specific_safe_diagnostic(self):
        from core.github import IntegrationError

        job = self.job()
        with (
            patch("core.github.GitHub", return_value=FakeGitHub(self.snapshot())),
            patch(
                "core.worker.guard",
                side_effect=IntegrationError("GitHub request failed (HTTP 403)."),
            ),
        ):
            run_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIn("HTTP 403", job.reason)
        self.assertNotIn("required checks", job.reason)

    def test_untrusted_integration_message_not_exposed(self):
        from core.github import IntegrationError

        job = self.job()
        with (
            patch("core.github.GitHub", return_value=FakeGitHub(self.snapshot())),
            patch("core.worker.guard", side_effect=IntegrationError("Bearer SECRET raw body")),
        ):
            run_job(job)
        job.refresh_from_db()
        self.assertNotIn("SECRET", job.reason)

    def test_saved_analysis_survives_limit_and_resume_skips_model(self):
        from core.delivery import save_review
        from core.models import ReviewCheckpoint
        from core.tests.test_providers import RESULT

        job, error = self.job(), self.limit()
        save_review(job, RESULT, {"complete": True}, {"passed": True})
        payload = ReviewCheckpoint.objects.get(job=job).encrypted_payload
        with (
            patch("core.github.GitHub", return_value=FakeGitHub(self.snapshot())),
            patch("core.worker._deliver_and_finish", side_effect=error),
            patch("core.providers.review") as model,
            patch("core.context.build_context") as context,
            patch("core.worker.once") as feedback,
        ):
            run_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual(ReviewCheckpoint.objects.get(job=job).encrypted_payload, payload)
        self.assertIsNone(workflow.claim())
        with patch("core.claims.timezone.now", return_value=error.retry_at + timedelta(seconds=1)):
            resumed = workflow.claim()
        with (
            patch("core.github.GitHub", return_value=FakeGitHub(self.snapshot())),
            patch(
                "core.worker._deliver_and_finish",
                side_effect=lambda github, active, progress: workflow.finish(active, "completed"),
            ),
            patch("core.providers.review") as resumed_model,
            patch("core.context.build_context") as resumed_context,
        ):
            run_job(resumed)
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertEqual(ReviewCheckpoint.objects.get(job=job).encrypted_payload, payload)
        for mock in (model, context, feedback, resumed_model, resumed_context):
            mock.assert_not_called()

    def test_interrupted_model_attempt_closes_safely_before_requeue(self):
        from core.models import ModelAttempt, Provider

        Provider.objects.create(name="CLI", kind="codex_cli", model="test")
        job = self.job()
        job.workflow_revision = "v1.1"
        job.save(update_fields=["workflow_revision"])
        with (
            patch("core.github.GitHub", return_value=FakeGitHub(self.snapshot())),
            patch("core.context.build_context", return_value={"complete": True}),
            patch("core.github.check_required", return_value={"passed": True, "summary": "Passed"}),
            patch("core.providers.review", side_effect=self.limit()),
        ):
            run_job(job)
        job.refresh_from_db()
        attempt = ModelAttempt.objects.get(job=job)
        self.assertEqual(job.status, "queued")
        self.assertEqual(attempt.status, "attention")
        self.assertIsNotNone(attempt.finished_at)
        self.assertEqual(attempt.diagnostics["failure_detail"], "github_rate_limit")

    def test_post_analysis_ci_limit_retains_result_for_delivery_only_retry(self):
        from core.delivery import load_review
        from core.models import Provider
        from core.tests.test_providers import RESULT

        Provider.objects.create(name="CLI", kind="codex_cli", model="test")
        job, error = self.job(), self.limit()
        job.workflow_revision = "v1.1"
        job.save(update_fields=["workflow_revision"])
        repo = job.pr.repository
        repo.required_checks = ["tests"]
        repo.save(update_fields=["required_checks"])
        with (
            patch("core.github.GitHub", return_value=FakeGitHub(self.snapshot())),
            patch("core.context.build_context", return_value={"complete": True}),
            patch(
                "core.github.ci_snapshot",
                side_effect=[{"passed": True, "summary": "Passed"}, error],
            ),
            patch("core.providers.review", return_value=RESULT) as model,
        ):
            run_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual(model.call_count, 1)
        _, saved = load_review(job)
        self.assertEqual(saved["result"], RESULT)
        with patch("core.claims.timezone.now", return_value=error.retry_at + timedelta(seconds=1)):
            resumed = workflow.claim()
        with (
            patch("core.github.GitHub", return_value=FakeGitHub(self.snapshot())),
            patch(
                "core.worker._deliver_and_finish",
                side_effect=lambda github, active, progress: workflow.finish(active, "completed"),
            ),
            patch("core.providers.review") as repeated_model,
        ):
            run_job(resumed)
        repeated_model.assert_not_called()

    def test_server_error_defers_then_stops_after_three_job_retries(self):
        from core.github import GitHubUnavailable

        job = self.job()
        for attempt in range(4):
            with (
                patch("core.github.GitHub", return_value=FakeGitHub(self.snapshot())),
                patch(
                    "core.context.build_context",
                    side_effect=GitHubUnavailable(503, "/issues/1", "ABC"),
                ),
                patch("core.worker.once") as feedback,
            ):
                run_job(job)
            job.refresh_from_db()
            feedback.assert_not_called()
            if attempt < 3:
                self.assertEqual(job.status, "queued")
                with patch(
                    "core.claims.timezone.now",
                    return_value=job.next_retry_at + timedelta(seconds=1),
                ):
                    job = workflow.claim()
            else:
                self.assertEqual(job.status, "attention")

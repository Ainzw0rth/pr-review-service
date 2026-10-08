from contextlib import ExitStack
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from core import workflow
from core.models import Credential, Job, Provider
from core.providers import ProviderError
from core.tests.test_publication import FakeGitHub
from core.tests.test_workflow import WorkflowTests
from core.worker import run_job


@override_settings(LIVE_WRITES=True)
class WorkerTests(WorkflowTests):
    def setUp(self):
        super().setUp()
        self.first = Provider.objects.create(
            name="first",
            kind="openai",
            model="configured-model",
            credential=Credential.objects.first(),
        )
        self.second = Provider.objects.create(
            name="backup",
            kind="anthropic",
            model="configured-backup",
            credential=Credential.objects.first(),
            order=1,
        )

    def run_case(self, effect):
        self.reconcile(self.event("labeled"), self.snapshot())
        job = workflow.claim()
        gh = FakeGitHub(self.snapshot())
        with ExitStack() as stack:
            stack.enter_context(patch("core.github.GitHub", return_value=gh))
            stack.enter_context(
                patch("core.context.build_context", return_value={"complete": True})
            )
            stack.enter_context(
                patch(
                    "core.github.check_required", return_value={"passed": True, "summary": "Passed"}
                )
            )
            stack.enter_context(patch("core.sheets.sync_status"))
            reviewer = stack.enter_context(patch("core.providers.review", side_effect=effect))
            run_job(job)
        job.refresh_from_db()
        return job, gh, reviewer

    def test_workflow_edit_after_claim_cannot_change_model(self):
        from core.tests.test_providers import RESULT

        original = self.first.model
        actual_run = run_job

        def run_with_changed_configuration(job):
            Provider.objects.filter(pk=self.first.pk).update(model="edited-model", enabled=False)
            actual_run(job)

        with patch("core.tests.test_worker.run_job", side_effect=run_with_changed_configuration):
            job, _, reviewer = self.run_case([{**RESULT, "blocking": True}])
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(reviewer.call_args.args[0].model, original)
        self.assertEqual(job.provider_plan[0]["model"], original)

    def test_label_failure_after_publication_does_not_report_review_failure(self):
        from core.github import IntegrationError
        from core.models import Receipt
        from core.tests.test_providers import RESULT

        with patch(
            "core.publication.terminal_transition",
            side_effect=IntegrationError("GitHub request failed (HTTP 404)."),
        ):
            job, gh, reviewer = self.run_case([{**RESULT, "blocking": True}])
        self.assertEqual(job.status, "attention")
        self.assertIn("Review published", job.reason)
        self.assertIn("label", job.reason)
        self.assertTrue(Receipt.objects.filter(job=job, key="review").exists())
        self.assertFalse(Receipt.objects.filter(job=job, key="failure").exists())
        self.assertEqual(reviewer.call_count, 1)

    def test_stale_publication_reason_preserves_delivered_review_context(self):
        from core.publication import StaleReview
        from core.tests.test_providers import RESULT

        with patch(
            "core.publication.terminal_transition",
            side_effect=StaleReview("Human changed review lifecycle during publication"),
        ):
            job, gh, reviewer = self.run_case([{**RESULT, "blocking": True}])
        self.assertEqual(job.status, "superseded")
        self.assertIn("Review published", job.reason)
        self.assertIn("Human changed review lifecycle", job.reason)
        self.assertEqual(reviewer.call_count, 1)

    def test_unrecognized_cancellation_reason_is_not_exposed(self):
        from core.publication import StaleReview
        from core.tests.test_providers import RESULT

        with patch(
            "core.publication.terminal_transition", side_effect=StaleReview("secret-content")
        ):
            job, gh, reviewer = self.run_case([RESULT])
        self.assertEqual(job.status, "superseded")
        self.assertNotIn("secret-content", job.reason)

    def test_quota_keeps_provider_and_releases_worker_without_backup(self):
        retry = timezone.now() + timezone.timedelta(hours=1)
        job, gh, reviewer = self.run_case(ProviderError("quota", retry_at=retry))
        self.assertEqual(job.status, "quota")
        self.assertEqual(job.provider, self.first)
        self.assertEqual(job.next_retry_at, retry)
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(gh.writes, [])
        self.assertIn("quota", list(job.activity.values_list("code", flat=True)))

    def test_subscription_quota_falls_forward_and_skips_shared_account(self):
        from core.tests.test_providers import RESULT

        self.first.kind = "claude_cli"
        self.first.save()
        self.second.kind = "codex_cli"
        self.second.save()
        job, gh, reviewer = self.run_case([ProviderError("quota", detail="cli_quota"), RESULT])
        self.assertEqual(job.status, "completed")
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(job.provider, self.second)

    def test_known_exhausted_subscription_is_skipped_without_a_model_call(self):
        from core.quota import record_exhausted
        from core.tests.test_providers import RESULT

        self.first.kind = "claude_cli"
        self.first.save()
        record_exhausted(self.first)
        job, gh, reviewer = self.run_case([RESULT])
        self.assertEqual(job.status, "completed")
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(job.provider, self.second)
        self.assertEqual(
            list(job.model_attempts.values_list("status", flat=True)), ["skipped", "success"]
        )

    def test_all_subscription_quotas_wait_without_calling_other_opus(self):
        self.first.kind = self.second.kind = "claude_cli"
        self.first.save()
        self.second.save()
        job, gh, reviewer = self.run_case(ProviderError("quota", detail="cli_quota"))
        self.assertEqual(job.status, "quota")
        self.assertEqual(reviewer.call_count, 1)
        self.assertIsNotNone(job.next_retry_at)
        self.assertEqual(gh.writes, [])

    def test_all_refusals_try_each_provider_once_then_pause(self):
        job, gh, reviewer = self.run_case(ProviderError("attention"))
        self.assertEqual(job.status, "attention")
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(
            list(job.model_attempts.values_list("status", flat=True)), ["refused", "refused"]
        )
        self.assertEqual(gh.writes, [])

    def test_public_cli_output_is_persisted_with_its_model_source(self):
        def effect(provider, context, cancelled, *, on_response, on_activity):
            on_activity("cli_output_text", "Checking the changed handler.")
            raise ProviderError("attention")

        job, gh, reviewer = self.run_case(effect)
        events = job.activity.filter(code="cli_output_text")
        self.assertEqual(events.count(), 2)
        self.assertTrue(
            all(
                event.source and event.detail == "Checking the changed handler." for event in events
            )
        )
        self.assertEqual(gh.writes, [])

    def test_each_refusal_keeps_its_diagnostics_without_publication(self):
        metadata = {"http_status": 200, "stop_reason": "refusal", "refusal_category": "cyber"}

        def effect(provider, context, cancelled, *, on_response, on_activity=None):
            on_response(metadata)
            raise ProviderError("attention")

        job, gh, reviewer = self.run_case(effect)
        expected = {**metadata, "failure_kind": "attention", "failure_detail": None}
        self.assertEqual(job.provider_diagnostics, expected)
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(
            list(job.model_attempts.values_list("diagnostics", flat=True)), [expected, expected]
        )
        self.assertEqual(gh.writes, [])

    def test_refusal_then_outage_then_third_model_success(self):
        third = Provider.objects.create(
            name="third",
            kind="anthropic",
            model="third-model",
            credential=Credential.objects.first(),
            order=2,
        )
        result = {
            "complete": True,
            "blocking": False,
            "summary": "No defects.",
            "findings": [],
            "replies": [],
            "related_comments": [],
        }
        job, gh, reviewer = self.run_case(
            [ProviderError("attention"), ProviderError("unavailable"), result]
        )
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.provider, third)
        self.assertEqual(
            list(job.model_attempts.values_list("status", flat=True)),
            ["refused", "unavailable", "success"],
        )
        self.assertEqual(reviewer.call_count, 3)
        self.assertEqual(reviewer.call_args_list[0].args[1], reviewer.call_args_list[2].args[1])

    def test_cancellation_after_refusal_prevents_next_model(self):
        def effect(*args, **kwargs):
            Job.objects.filter(status="running").update(status="cancelled")
            raise ProviderError("attention")

        job, gh, reviewer = self.run_case(effect)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(gh.writes, [])

    def test_quota_retry_starts_at_same_provider_not_prior_refusal(self):
        job, gh, reviewer = self.run_case([ProviderError("attention"), ProviderError("quota")])
        self.assertEqual(job.provider, self.second)
        Job.objects.filter(pk=job.pk).update(next_retry_at=timezone.now())
        resumed = workflow.claim()
        with (
            patch("core.github.GitHub", return_value=gh),
            patch("core.context.build_context", return_value={"complete": True}),
            patch("core.github.check_required", return_value={"passed": True, "summary": "Passed"}),
            patch("core.providers.review", side_effect=ProviderError("quota")) as retry,
        ):
            run_job(resumed)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args.args[0], self.second)
        self.assertEqual(
            list(job.model_attempts.values_list("status", flat=True)),
            ["refused", "quota", "quota"],
        )
        self.assertEqual(gh.writes, [])

    def test_authentication_error_does_not_rotate(self):
        job, gh, reviewer = self.run_case(ProviderError("auth"))
        self.assertEqual(job.status, "attention")
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(gh.writes, [])

    def test_outage_uses_backup_and_publishes(self):
        result = {
            "complete": True,
            "blocking": False,
            "summary": "No defects.",
            "findings": [],
            "replies": [],
            "related_comments": [],
        }
        job, gh, reviewer = self.run_case([ProviderError("unavailable"), result])
        self.assertEqual(job.status, "completed")
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(job.provider, self.second)
        self.assertEqual([x["name"] for x in gh.snapshot["labels"]], ["approved"])
        codes = list(job.activity.values_list("code", flat=True))
        self.assertEqual(codes.count("model"), 2)
        self.assertLess(codes.index("unavailable"), codes.index("result"))
        self.assertLess(codes.index("result"), codes.index("publishing"))
        self.assertEqual(codes[-1], "ended")

    def test_bad_credential_marks_job_failed_not_stuck_running(self):
        from core.github import IntegrationError

        self.reconcile(self.event("labeled"), self.snapshot())
        job = workflow.claim()
        with patch("core.github.GitHub", side_effect=IntegrationError("Unavailable")):
            run_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")

    def test_context_failure_exposes_safe_cause_without_calling_model(self):
        self.reconcile(self.event("labeled"), self.snapshot())
        job = workflow.claim()
        reason = "Too many linked issues or pull requests for complete review."
        github = FakeGitHub(self.snapshot())
        with (
            patch("core.github.GitHub", return_value=github),
            patch("core.context.build_context", return_value={"complete": False, "reason": reason}),
            patch("core.providers.review") as model,
        ):
            run_job(job)
        job.refresh_from_db()
        self.assertEqual(job.reason, reason)
        self.assertIn("context_failed", job.activity.values_list("code", flat=True))
        model.assert_not_called()
        self.assertEqual([label["name"] for label in github.snapshot["labels"]], ["feedback"])

    def test_context_diagnostic_never_persists_arbitrary_external_text(self):
        from core.context import failure_reason

        self.assertEqual(
            failure_reason({"reason": "secret-token or untrusted GitHub response"}),
            "Review context is incomplete or unavailable; no model review was started.",
        )

    def test_exhausted_cli_coverage_falls_forward_without_publishing_partial_review(self):
        from core.tests.test_providers import RESULT

        job, gh, reviewer = self.run_case(
            [
                ProviderError("invalid", detail="cli_coverage"),
                RESULT,
            ]
        )
        self.assertEqual(job.status, "completed")
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(
            list(job.model_attempts.values_list("status", flat=True)), ["incomplete", "success"]
        )
        self.assertIn("model_incomplete", list(job.activity.values_list("code", flat=True)))
        self.assertEqual(job.provider, self.second)

    def test_all_coverage_failures_pause_without_publishing(self):
        job, gh, reviewer = self.run_case(ProviderError("invalid", detail="cli_coverage"))
        self.assertEqual(job.status, "attention")
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(gh.writes, [])
        self.assertIn("incomplete evidence", job.reason)

    def test_invalid_schema_exhausts_route_without_publishing(self):
        job, gh, reviewer = self.run_case(ProviderError("invalid", detail="output_invalid"))
        self.assertEqual(job.status, "attention")
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(gh.writes, [])

    def test_cli_protocol_then_invalid_output_uses_third_model(self):
        from core.tests.test_providers import RESULT

        third = Provider.objects.create(name="third", kind="claude_cli", model="backup", order=2)
        job, gh, reviewer = self.run_case(
            [
                ProviderError("invalid", detail="cli_protocol"),
                ProviderError("invalid", detail="output_invalid"),
                RESULT,
            ]
        )
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.provider, third)
        self.assertEqual(
            list(job.model_attempts.values_list("status", flat=True)),
            ["failed", "failed", "success"],
        )
        self.assertEqual(reviewer.call_args_list[0].args[1], reviewer.call_args_list[2].args[1])

    def test_invalid_thread_assessments_cannot_survive_exhausted_fallback(self):
        from core.tests.test_providers import RESULT

        with patch("core.worker.thread_replies", side_effect=ValueError("invalid")):
            job, gh, reviewer = self.run_case([RESULT, RESULT])
        self.assertEqual(job.status, "attention")
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(gh.writes, [])

    def test_tool_boundary_failure_does_not_rotate(self):
        job, gh, reviewer = self.run_case(ProviderError("invalid", detail="cli_tools"))
        self.assertEqual(job.status, "attention")
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(gh.writes, [])

    def test_journal_capture_failure_does_not_change_review_outcome(self):
        from core.tests.test_providers import RESULT

        with patch("core.journal.queue_job", side_effect=RuntimeError("private-token")):
            job, gh, _ = self.run_case([RESULT])
        self.assertEqual(job.status, "completed")
        self.assertIn({"name": "approved"}, gh.snapshot["labels"])
        self.assertIn("journal_attention", list(job.activity.values_list("code", flat=True)))
        self.assertNotIn("private-token", job.reason)

    def test_final_review_is_handed_to_journal_only_after_publication(self):
        from core.tests.test_providers import RESULT

        with patch("core.journal.queue_job", return_value=None) as capture:
            job, gh, _ = self.run_case([RESULT])
        self.assertEqual(job.status, "completed")
        self.assertEqual(capture.call_args.args[1], RESULT)
        self.assertEqual(capture.call_args.args[2], "ready")
        self.assertTrue(gh.writes)

    def test_owner_delivery_retry_reuses_saved_analysis_and_summary(self):
        from core.delivery import queue_retry
        from core.github import IntegrationError
        from core.tests.test_providers import RESULT

        original = FakeGitHub.post

        def fail_review(github, path, data):
            if path.endswith("/reviews"):
                raise IntegrationError("GitHub request failed (HTTP 403).")
            return original(github, path, data)

        with patch.object(FakeGitHub, "post", autospec=True, side_effect=fail_review):
            job, gh, reviewer = self.run_case([{**RESULT, "blocking": True}])
        self.assertEqual(job.status, "attention")
        self.assertIn("HTTP 403", job.reason)
        self.assertEqual(len(gh.comments), 1)
        queued = queue_retry(job)
        self.assertEqual(queued.pk, job.pk)
        with (
            patch("core.github.GitHub", return_value=gh),
            patch("core.providers.review") as model,
            patch("core.context.build_context") as context,
            patch("core.sheets.sync_status"),
        ):
            run_job(workflow.claim())
        model.assert_not_called()
        context.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertEqual(len(gh.comments), 1)
        self.assertEqual(len(gh.reviews), 1)

    def test_delivery_retry_new_commit_cannot_publish_saved_analysis(self):
        from core.delivery import queue_retry, save_review
        from core.tests.test_providers import RESULT

        self.reconcile(self.event("labeled"), self.snapshot())
        job = workflow.claim()
        save_review(job, RESULT, {}, {"passed": True, "summary": "Pass"})
        workflow.finish(job, "attention")
        job.refresh_from_db()
        queue_retry(job)
        gh = FakeGitHub(self.snapshot(sha="b" * 40))
        with patch("core.github.GitHub", return_value=gh), patch("core.providers.review") as model:
            run_job(workflow.claim())
        model.assert_not_called()
        self.assertEqual(gh.writes, [])
        job.refresh_from_db()
        self.assertEqual(job.status, "superseded")

    def test_restart_recognizes_confirmed_delivery_without_rerunning(self):
        from core.models import ReviewCheckpoint
        from core.worker import recover_interrupted

        self.reconcile(self.event("labeled"), self.snapshot())
        job = workflow.claim()
        ReviewCheckpoint.objects.create(job=job, stage="done", encrypted_payload="saved")
        recover_interrupted()
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertIn("delivery completed", job.reason)

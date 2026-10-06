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

    def test_quota_keeps_provider_and_releases_worker_without_backup(self):
        retry = timezone.now() + timezone.timedelta(hours=1)
        job, gh, reviewer = self.run_case(ProviderError("quota", retry_at=retry))
        self.assertEqual(job.status, "quota")
        self.assertEqual(job.provider, self.first)
        self.assertEqual(job.next_retry_at, retry)
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(gh.writes, [])
        self.assertIn("quota", list(job.activity.values_list("code", flat=True)))

    def test_all_refusals_try_each_provider_once_then_pause(self):
        job, gh, reviewer = self.run_case(ProviderError("attention"))
        self.assertEqual(job.status, "attention")
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(
            list(job.model_attempts.values_list("status", flat=True)), ["refused", "refused"]
        )
        self.assertEqual(gh.writes, [])

    def test_each_refusal_keeps_its_diagnostics_without_publication(self):
        metadata = {"http_status": 200, "stop_reason": "refusal", "refusal_category": "cyber"}

        def effect(provider, context, cancelled, *, on_response):
            on_response(metadata)
            raise ProviderError("attention")

        job, gh, reviewer = self.run_case(effect)
        self.assertEqual(job.provider_diagnostics, metadata)
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(
            list(job.model_attempts.values_list("diagnostics", flat=True)), [metadata, metadata]
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

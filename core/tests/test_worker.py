from contextlib import ExitStack
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from core import workflow
from core.models import Credential, Provider
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

    def test_policy_refusal_never_falls_back(self):
        job, gh, reviewer = self.run_case(ProviderError("attention"))
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

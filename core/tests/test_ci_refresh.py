from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from core import workflow
from core.flow import snapshot
from core.github import check_required
from core.tests import test_worker as worker_tests
from core.tests.test_providers import RESULT
from core.tests.test_publication import FakeGitHub
from core.worker import run_job


@override_settings(LIVE_WRITES=True)
class CIRefreshTests(TestCase):
    # Reuse fixtures without inheriting their test methods.
    setUp = worker_tests.WorkflowTests.setUp
    snapshot = worker_tests.WorkflowTests.snapshot
    event = worker_tests.WorkflowTests.event
    reconcile = worker_tests.WorkflowTests.reconcile

    def test_missing_aggregate_waits_while_dependencies_run(self):
        gh = Mock()
        gh.pages.side_effect = [[{"name": "rust", "status": "in_progress"}], []]
        result = check_required(gh, SimpleNamespace(required_checks=["required"]), "a" * 40)
        self.assertTrue(result["pending"])
        self.assertFalse(result["passed"])

    def test_missing_check_without_active_ci_is_not_an_infinite_wait(self):
        gh = Mock()
        gh.pages.side_effect = [[], []]
        result = check_required(gh, SimpleNamespace(required_checks=["required"]), "a" * 40)
        self.assertFalse(result["pending"])
        self.assertFalse(result["passed"])

    def test_failed_required_check_is_not_hidden_by_other_running_ci(self):
        gh = Mock()
        gh.pages.side_effect = [
            [
                {"name": "required", "status": "completed", "conclusion": "failure"},
                {"name": "optional", "status": "in_progress"},
            ],
            [],
        ]
        self.assertFalse(
            check_required(gh, SimpleNamespace(required_checks=["required"]), "a" * 40)["pending"]
        )

    def test_ci_node_uses_latest_result(self):
        self.reconcile(self.event("labeled"), self.snapshot())
        job = workflow.claim()
        events = [
            SimpleNamespace(code=c, count=None, created_at=None)
            for c in ["checks", "checks_blocked", "checks_passed"]
        ]
        ci = next(n for n in snapshot(job, events)["stages"] if n["id"] == "checks")
        self.assertEqual(ci["state"], "done")
        events.append(SimpleNamespace(code="checks_blocked", count=None, created_at=None))
        ci = next(n for n in snapshot(job, events)["stages"] if n["id"] == "checks")
        self.assertEqual(ci["state"], "blocked")

    def run_review(self, results, *, local_tests=None):
        from core.models import Credential, Provider

        Provider.objects.create(
            name="test", kind="openai", model="test", credential=Credential.objects.first()
        )
        self.repo.required_checks = ["required"]
        if local_tests is not None:
            self.repo.test_command = ["test"]
        self.repo.save()
        self.reconcile(self.event("labeled"), self.snapshot())
        job = workflow.claim()
        gh = FakeGitHub(self.snapshot())
        gh.archive = Mock(return_value=b"archive")
        results = iter(results)
        last = None

        def check(*args):
            nonlocal last
            last = next(results, last)
            return last

        with (
            patch("core.github.GitHub", return_value=gh),
            patch("core.context.build_context", return_value={"complete": True}),
            patch("core.github.check_required", side_effect=check),
            patch("core.providers.review", return_value=RESULT) as review,
            patch("core.runner.run_tests", return_value=local_tests),
            patch("core.sheets.sync_status"),
            patch("core.worker.time.sleep"),
        ):
            run_job(job)
        job.refresh_from_db()
        return job, gh, review

    def test_pending_ci_finishes_before_model_sees_evidence(self):
        job, gh, review = self.run_review(
            [
                {"passed": False, "pending": True, "summary": "CI running"},
                {"passed": True, "pending": False, "summary": "CI passed"},
            ]
        )
        self.assertEqual(job.status, "completed")
        self.assertTrue(review.call_args.args[1]["checks"]["passed"])
        self.assertIn({"name": "approved"}, gh.snapshot["labels"])
        self.assertIn("checks_waiting", list(job.activity.values_list("code", flat=True)))

    def test_latest_success_not_combined_with_old_ci_failure(self):
        job, gh, _ = self.run_review(
            [
                {"passed": False, "summary": "CI failed"},
                {"passed": True, "summary": "CI passed"},
            ]
        )
        self.assertEqual(job.status, "completed")
        self.assertIn({"name": "approved"}, gh.snapshot["labels"])

    def test_ci_success_does_not_erase_failed_local_tests(self):
        job, gh, _ = self.run_review(
            [
                {"passed": True, "summary": "CI passed"},
            ],
            local_tests={"passed": False, "summary": "Local tests failed"},
        )
        self.assertEqual(job.status, "completed")
        self.assertIn({"name": "feedback"}, gh.snapshot["labels"])
        summary = next(data["body"] for path, data in gh.writes if path.endswith("/comments"))
        self.assertIn("Local tests failed", summary)

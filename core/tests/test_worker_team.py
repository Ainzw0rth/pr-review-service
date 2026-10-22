import copy
import json
import threading
import time
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.flow import snapshot as progress_snapshot
from core.models import Job
from core.providers import ProviderError, ReviewCancelled
from core.secrets import decrypt
from core.tests import test_worker, test_worker_v2
from core.tests.test_review_team import report, synthesis
from core.tests.test_v2_transport import INTENT
from core.usage import public_usage


def team_result():
    value = synthesis()
    value["replies"] = []
    return value


@override_settings(LIVE_WRITES=True)
class WorkerTeamTests(TestCase):
    def setUp(self):
        test_worker_v2.WorkerV2Tests.setUp(self)
        from core.secrets import encrypt

        credential = self.first.credential
        credential.encrypted_value = encrypt("test-api-key")
        credential.save(update_fields=["encrypted_value"])

    snapshot = test_worker_v2.WorkerV2Tests.snapshot
    event = test_worker_v2.WorkerV2Tests.event
    reconcile = test_worker_v2.WorkerV2Tests.reconcile
    run_v2 = test_worker_v2.WorkerV2Tests.run_v2

    def complete(self, provider, evidence, cancelled, **kwargs):
        kwargs["on_response"]({"input_tokens": 20, "output_tokens": 2})
        if kwargs["task"] == "intent":
            return copy.deepcopy(INTENT)
        if kwargs["task"] == "review_synthesis":
            return team_result()
        return report()

    def test_new_jobs_run_three_specialists_then_publish_one_review(self):
        job, gh, model = self.run_v2(self.complete)
        self.assertEqual(job.workflow_revision, "v3.1")
        self.assertEqual(job.status, "completed", job.reason)
        tasks = [call.kwargs["task"] for call in model.call_args_list]
        self.assertEqual(tasks[0], "intent")
        self.assertCountEqual(tasks[1:4], ["review_code", "review_security", "review_tests"])
        self.assertEqual(tasks[4], "review_synthesis")
        synth_context = model.call_args.args[1]
        self.assertEqual(synth_context["intent"], INTENT)
        self.assertEqual(synth_context["pr"]["body"], "Fix login only.")
        self.assertEqual(set(synth_context["reviewer_reports"]), {"code", "security", "tests"})
        summaries = [data["body"] for path, data in gh.writes if path.endswith("/comments")]
        self.assertEqual(len(summaries), 1)
        self.assertIn("Review perspectives", summaries[0])
        self.assertFalse(any(data.get("event") == "APPROVE" for _, data in gh.writes))
        saved = json.loads(decrypt(job.review_checkpoint.encrypted_payload))
        self.assertEqual(set(saved["review_team"]["reports"]), {"code", "security", "tests"})
        usage = public_usage(job.model_attempts.get().diagnostics)
        self.assertEqual(usage["session_count"], 5)
        self.assertEqual(usage["totals"]["input_tokens"], 100)
        self.assertEqual(
            [p["phase"] for p in usage["passes"]],
            ["intent", "code", "security", "tests", "synthesis"],
        )
        timeline = job.model_attempts.get().diagnostics["phase_timeline"]
        self.assertEqual(
            {p["phase"] for p in timeline}, {"intent", "code", "security", "tests", "synthesis"}
        )
        self.assertTrue(
            all(p["state"] == "done" and p["started_at"] and p["finished_at"] for p in timeline)
        )
        public_events = json.dumps(list(job.activity.values("code", "detail")))
        self.assertNotIn("Checked affected behavior", public_events)
        self.assertNotIn("Fix login", public_events)

    def test_specialist_failure_restarts_complete_team_on_fallback_provider(self):
        def call(provider, evidence, cancelled, **kwargs):
            if provider.pk == self.first.pk and kwargs["task"] == "review_security":
                raise ProviderError("unavailable")
            return self.complete(provider, evidence, cancelled, **kwargs)

        job, gh, model = self.run_v2(call)
        self.assertEqual(job.status, "completed", job.reason)
        self.assertEqual(job.provider_id, self.second.pk)
        self.assertFalse(
            any(
                c.args[0].pk == self.first.pk and c.kwargs["task"] == "review_synthesis"
                for c in model.call_args_list
            )
        )
        self.assertEqual(
            list(job.model_attempts.values_list("status", flat=True)), ["unavailable", "success"]
        )

    @override_settings(CLI_ACCOUNT_CONCURRENCY=1)
    def test_fallback_reuses_validated_intent_and_completed_specialist(self):
        self.first.kind = "codex_cli"
        self.first.save()

        def call(provider, evidence, cancelled, **kwargs):
            if provider.pk == self.first.pk and kwargs["task"] == "review_security":
                raise ProviderError("unavailable")
            return self.complete(provider, evidence, cancelled, **kwargs)

        job, gh, model = self.run_v2(call)
        self.assertEqual(job.status, "completed", job.reason)
        fallback = [
            c.kwargs["task"] for c in model.call_args_list if c.args[0].pk == self.second.pk
        ]
        self.assertCountEqual(fallback, ["review_security", "review_tests", "review_synthesis"])
        self.assertEqual(job.phase_checkpoints.count(), 4)
        for row in job.phase_checkpoints.all():
            self.assertNotIn("Checked affected behavior", row.encrypted_payload)
        audit = json.loads(decrypt(job.review_checkpoint.encrypted_payload))["review_team"]
        self.assertEqual(audit["phase_sources"]["code"]["model"], self.first.model)
        self.assertEqual(audit["phase_sources"]["security"]["model"], self.second.model)

    def test_synthesis_failure_only_retries_synthesis(self):
        def call(provider, evidence, cancelled, **kwargs):
            if provider.pk == self.first.pk and kwargs["task"] == "review_synthesis":
                raise ProviderError("unavailable")
            return self.complete(provider, evidence, cancelled, **kwargs)

        job, _, model = self.run_v2(call)
        self.assertEqual(job.status, "completed", job.reason)
        self.assertEqual(
            [c.kwargs["task"] for c in model.call_args_list if c.args[0].pk == self.second.pk],
            ["review_synthesis"],
        )
        usage = public_usage(job.model_attempts.last().diagnostics)
        self.assertEqual(usage["session_count"], 1)
        self.assertEqual(usage["totals"]["input_tokens"], 20)

    def test_parallel_failure_retains_successful_peers(self):
        barrier = threading.Barrier(3)

        def call(provider, evidence, cancelled, **kwargs):
            task = kwargs["task"]
            if provider.pk == self.first.pk and task.startswith("review_"):
                barrier.wait(timeout=3)
                if task == "review_security":
                    raise ProviderError("unavailable")
                time.sleep(0.05)
            return self.complete(provider, evidence, cancelled, **kwargs)

        job, _, model = self.run_v2(call)
        self.assertEqual(job.status, "completed", job.reason)
        self.assertCountEqual(
            [c.kwargs["task"] for c in model.call_args_list if c.args[0].pk == self.second.pk],
            ["review_security", "review_synthesis"],
        )

    def test_new_explicit_retry_resumes_saved_analysis_without_republishing(self):
        from core.claims import claim
        from core.retries import retry_job
        from core.worker import run_job

        def fail_synthesis(provider, evidence, cancelled, **kwargs):
            if kwargs["task"] == "review_synthesis":
                raise ProviderError("unavailable")
            return self.complete(provider, evidence, cancelled, **kwargs)

        old, gh, model = self.run_v2(fail_synthesis)
        self.assertEqual(old.status, "attention", old.reason)
        self.assertEqual(gh.writes, [])
        self.assertFalse(hasattr(old, "review_checkpoint"))
        evidence = copy.deepcopy(
            next(c.args[1] for c in model.call_args_list if c.kwargs["task"] == "review_code")
        )
        evidence.pop("intent")
        with (
            patch("core.github.GitHub") as client,
            patch(
                "core.workflow.canonical_cycle",
                return_value={"eligible": True, "key": old.pr.canonical_trigger},
            ),
        ):
            client.return_value.pull.return_value = self.snapshot()
            retry_job(old, "owner")
        retry = claim()
        self.assertIsNotNone(retry)
        with (
            patch("core.github.GitHub", return_value=gh),
            patch("core.context.build_context", return_value=evidence),
            patch("core.github.check_required", return_value={"passed": True, "summary": "Passed"}),
            patch("core.sheets.sync_status"),
            patch("core.providers.review", side_effect=self.complete) as reviewer,
        ):
            run_job(retry)
        retry.refresh_from_db()
        self.assertEqual(retry.status, "completed", retry.reason)
        self.assertEqual([c.kwargs["task"] for c in reviewer.call_args_list], ["review_synthesis"])
        self.assertEqual(old.phase_checkpoints.count(), 4)
        self.assertFalse(retry.phase_checkpoints.exists())
        self.assertTrue(retry.activity.filter(code="phase_reused").exists())

    def test_rejected_citation_persists_safe_reason_and_location_without_publication(self):
        self.first.kind = "codex_cli"
        self.first.save()
        self.second.enabled = False
        self.second.save()

        def call(provider, evidence, cancelled, **kwargs):
            if kwargs["task"] == "review_security":
                value = report(finding=True)
                value["findings"][0]["evidence"][0]["quote"] = "private-unmatched-excerpt"
                return value
            return self.complete(provider, evidence, cancelled, **kwargs)

        evidence = {
            "complete": True,
            "head_sha": "a" * 40,
            "pr": {**self.snapshot(), "body": "Fix login only."},
            "files": [{"filename": "widget.py", "valid_lines": [3], "content": "secret"}],
            "threads": [],
            "issues": [],
            "related_prs": [],
        }
        job, gh, model = test_worker.WorkerTests.run_case(self, call, v2=True, context=evidence)
        self.assertEqual(job.status, "attention")
        self.assertEqual(gh.writes, [])
        self.assertFalse(hasattr(job, "review_checkpoint"))
        self.assertNotIn("review_synthesis", [c.kwargs["task"] for c in model.call_args_list])
        diagnostics = job.model_attempts.get().diagnostics
        self.assertEqual(diagnostics["failure_detail"], "output_invalid")
        self.assertEqual(diagnostics["validation_error"], "citation_quote")
        self.assertEqual(diagnostics["validation_path"], ["findings", 0, "evidence", 0, "quote"])
        public = progress_snapshot(job, list(job.activity.all()))
        reason = public["models"][0]["attempts"][0]["failure_reason"]
        self.assertIn("quote", reason.lower())
        self.assertIn("$.findings[0].evidence[0].quote", reason)
        self.assertNotIn(
            "private-unmatched-excerpt", json.dumps(diagnostics) + json.dumps(public, default=str)
        )

    def test_concurrent_api_quota_waits_without_fallback_or_publication(self):
        barrier = threading.Barrier(3)

        def call(provider, evidence, cancelled, **kwargs):
            task = kwargs["task"]
            if task == "intent":
                return copy.deepcopy(INTENT)
            barrier.wait(timeout=3)
            if task == "review_security":
                raise ProviderError("unavailable")
            threading.Event().wait(0.03)
            raise ProviderError("quota")

        job, gh, model = self.run_v2(call)
        self.assertEqual(job.status, "quota", job.reason)
        self.assertEqual(gh.writes, [])
        self.assertEqual({c.args[0].pk for c in model.call_args_list}, {self.first.pk})
        self.assertEqual(job.model_attempts.get().status, "quota")

    @override_settings(CLI_ACCOUNT_CONCURRENCY=3)
    def test_concurrent_cli_progress_and_failure_preserve_failing_role(self):
        from core.review_orchestration import run_team

        self.first.kind = "codex_cli"
        self.first.save()
        self.second.enabled = False
        self.second.save()
        barrier = threading.Barrier(3)
        observed = []

        def inspect_team(*args, **kwargs):
            response = kwargs["on_response"]

            def inspect_response(phase, data):
                response(phase, data)
                if phase in {"code", "security", "tests"}:
                    observed.append(Job.objects.get(status="running").provider_diagnostics["phase"])

            return run_team(*args, **{**kwargs, "on_response": inspect_response})

        def call(provider, evidence, cancelled, **kwargs):
            if kwargs["task"] == "intent":
                return copy.deepcopy(INTENT)
            barrier.wait(timeout=2)
            if kwargs["task"] == "review_security":
                raise ProviderError("invalid", detail="output_invalid")
            kwargs["on_response"]({"input_tokens": 20})
            return report()

        with patch("core.review_orchestration.run_team", side_effect=inspect_team):
            job, gh, _ = self.run_v2(call)
        self.assertEqual(job.status, "attention", job.reason)
        self.assertEqual(gh.writes, [])
        self.assertTrue(observed)
        self.assertEqual(set(observed), {"specialists"})
        self.assertEqual(job.model_attempts.get().diagnostics["failed_phase"], "security")

    @override_settings(CLI_ACCOUNT_CONCURRENCY=3)
    def test_cli_peer_quota_joins_all_peers_before_fallback(self):
        self.first.kind = "claude_cli"
        self.first.save()
        barrier = threading.Barrier(3)
        exited = set()

        def call(provider, evidence, cancelled, **kwargs):
            if provider.pk != self.first.pk:
                self.assertEqual(exited, {"review_code", "review_security", "review_tests"})
                return self.complete(provider, evidence, cancelled, **kwargs)
            task = kwargs["task"]
            if task == "intent":
                return copy.deepcopy(INTENT)
            barrier.wait(timeout=2)
            try:
                if task == "review_security":
                    raise ProviderError("unavailable")
                if task == "review_code":
                    threading.Event().wait(0.03)
                    raise ProviderError("quota")
                deadline = time.monotonic() + 2
                while not cancelled() and time.monotonic() < deadline:
                    threading.Event().wait(0.01)
                self.assertTrue(cancelled())
                raise ReviewCancelled()
            finally:
                exited.add(task)

        job, gh, _ = self.run_v2(call)
        self.assertEqual(job.status, "completed", job.reason)
        self.assertEqual(job.provider_id, self.second.pk)
        self.assertEqual(
            list(job.model_attempts.values_list("status", flat=True)), ["quota", "success"]
        )

    def test_one_security_blocker_produces_one_request_changes_review(self):
        def call(provider, evidence, cancelled, **kwargs):
            task = kwargs["task"]
            if task == "intent":
                return copy.deepcopy(INTENT)
            if task == "review_synthesis":
                value = synthesis(findings=True)
                value["replies"] = []
                value["assessments"][0]["finding_id"] = "security:F1"
                return value
            return report(finding=task == "review_security")

        evidence = {
            "complete": True,
            "head_sha": "a" * 40,
            "pr": {**self.snapshot(), "body": "Preserve widget content."},
            "files": [{"filename": "widget.py", "valid_lines": [3], "content": "secret"}],
            "threads": [],
            "issues": [],
            "related_prs": [],
        }
        job, gh, _ = test_worker.WorkerTests.run_case(self, call, v2=True, context=evidence)
        self.assertEqual(job.status, "completed", job.reason)
        self.assertIn({"name": "feedback"}, gh.snapshot["labels"])
        reviews = [data for path, data in gh.writes if path.endswith("/reviews")]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["event"], "REQUEST_CHANGES")
        self.assertEqual(len(reviews[0]["comments"]), 1)
        self.assertIn("Security", reviews[0]["comments"][0]["body"])

    def test_incomplete_assessment_publishes_feedback_never_approval_merge_or_resolution(self):
        self.repo.auto_approve = True
        self.repo.auto_merge = True
        self.repo.save()

        def call(provider, evidence, cancelled, **kwargs):
            if kwargs["task"] == "review_tests":
                return report(complete=False)
            return self.complete(provider, evidence, cancelled, **kwargs)

        job, gh, model = self.run_v2(call)
        self.assertEqual(job.status, "completed", job.reason)
        self.assertIn("review_synthesis", [c.kwargs["task"] for c in model.call_args_list])
        self.assertEqual({c.args[0].pk for c in model.call_args_list}, {self.first.pk})
        self.assertIn({"name": "feedback"}, gh.snapshot["labels"])
        self.assertFalse(
            any(
                data.get("event") == "APPROVE" or path.endswith("/merge")
                for path, data in gh.writes
            )
        )
        saved = json.loads(decrypt(job.review_checkpoint.encrypted_payload))
        self.assertFalse(saved["result"]["complete"])
        self.assertTrue(saved["result"]["blocking"])
        self.assertIn("Incomplete review", saved["result"]["summary"])
        self.assertFalse(saved["review_team"]["reports"]["tests"]["complete"])
        self.assertTrue(all(not reply["resolve"] for reply in saved["result"]["replies"]))
        self.assertTrue(job.activity.filter(code="tests_incomplete").exists())
        flow = progress_snapshot(job, list(job.activity.all()))
        phases = {phase["phase"]: phase for phase in flow["models"][0]["attempts"][0]["phases"]}
        for name in ("code", "security", "tests", "synthesis"):
            self.assertEqual(phases[name]["state"], "done")
        self.assertIs(phases["tests"]["coverage_complete"], False)
        self.assertIs(phases["synthesis"]["coverage_complete"], False)

    def test_cli_cancellation_between_specialists_cannot_publish(self):
        self.first.kind = "codex_cli"
        self.first.save()

        def call(provider, evidence, cancelled, **kwargs):
            value = self.complete(provider, evidence, cancelled, **kwargs)
            if kwargs["task"] == "review_code":
                Job.objects.filter(status="running").update(status="cancelled")
            return value

        job, gh, model = self.run_v2(call)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(gh.writes, [])
        self.assertEqual(
            [c.kwargs["task"] for c in model.call_args_list], ["intent", "review_code"]
        )

    def test_saved_delivery_retry_does_not_rerun_team(self):
        from core.delivery import queue_retry
        from core.github import IntegrationError
        from core.worker import run_job
        from core.workflow import claim

        with patch(
            "core.publication.terminal_transition",
            side_effect=IntegrationError("GitHub request failed (HTTP 404)."),
        ):
            job, gh, _ = self.run_v2(self.complete)
        self.assertEqual(job.status, "attention")
        queue_retry(job)
        with (
            patch("core.github.GitHub", return_value=gh),
            patch("core.providers.review") as model,
            patch("core.sheets.sync_status"),
            patch("core.context.threads", return_value=[]),
        ):
            run_job(claim())
        model.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "completed", job.reason)

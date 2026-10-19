import copy
import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.models import Credential, Job, Provider
from core.providers import ProviderError
from core.secrets import decrypt
from core.tests import test_worker, test_workflow
from core.tests.test_providers import RESULT
from core.tests.test_v2_transport import INTENT
from core.usage import public_usage

V2_RESULT = {
    **RESULT,
    "action_plan": {
        name: {"decision": "not_needed", "reason": "No relevant action."}
        for name in (
            "inline_findings",
            "discussion_replies",
            "related_pr_comments",
            "related_issue_comments",
        )
    },
}


@override_settings(LIVE_WRITES=True)
class WorkerV2Tests(TestCase):
    snapshot = test_workflow.WorkflowTests.snapshot
    event = test_workflow.WorkflowTests.event
    reconcile = test_workflow.WorkflowTests.reconcile

    def setUp(self):
        test_workflow.WorkflowTests.setUp(self)
        self.first = Provider.objects.create(
            name="first", kind="openai", model="configured", credential=Credential.objects.first()
        )
        self.second = Provider.objects.create(
            name="backup",
            kind="anthropic",
            model="backup",
            credential=Credential.objects.first(),
            order=1,
        )

    def run_v2(self, effect):
        context = {
            "complete": True,
            "head_sha": "a" * 40,
            "pr": {**self.snapshot(), "title": "Fix login", "body": "Fix login only."},
            "files": [],
            "threads": [],
            "issues": [],
            "related_prs": [],
        }
        return test_worker.WorkerTests.run_case(self, effect, v2=True, context=context)

    def test_intent_precedes_review_original_evidence_retained_and_brief_private(self):
        job, gh, model = self.run_v2([INTENT, copy.deepcopy(V2_RESULT)])
        self.assertEqual(job.status, "completed")
        self.assertEqual(
            [call.kwargs["task"] for call in model.call_args_list], ["intent", "review_v2"]
        )
        self.assertEqual(model.call_args.args[1]["intent"], INTENT)
        self.assertEqual(model.call_args.args[1]["pr"]["body"], "Fix login only.")
        self.assertEqual(job.activity.filter(code="intent_ready").count(), 1)
        self.assertNotIn("Fix login", json.dumps(list(job.activity.values("code", "detail"))))
        self.assertEqual(
            json.loads(decrypt(job.review_checkpoint.encrypted_payload))["intent"], INTENT
        )

    def test_saved_v2_attempts_still_run_both_phases(self):
        with patch("core.workflow_schema.REVISION", "v2"):
            job, _, model = self.run_v2([INTENT, copy.deepcopy(V2_RESULT)])
        self.assertEqual(job.workflow_revision, "v2")
        self.assertEqual(job.status, "completed")
        self.assertEqual(
            [call.kwargs["task"] for call in model.call_args_list], ["intent", "review_v2"]
        )

    def test_bad_intent_reference_falls_back_before_review(self):
        bad = copy.deepcopy(INTENT)
        bad["goal"]["sources"] = ["invented_source"]
        job, _, model = self.run_v2([bad, INTENT, copy.deepcopy(V2_RESULT)])
        self.assertEqual(job.status, "completed")
        self.assertEqual(
            [call.kwargs["task"] for call in model.call_args_list],
            ["intent", "intent", "review_v2"],
        )
        self.assertEqual(
            list(job.model_attempts.values_list("status", flat=True)), ["failed", "success"]
        )
        diagnostics = job.model_attempts.order_by("id").first().diagnostics
        self.assertEqual(diagnostics["failure_detail"], "output_invalid")
        self.assertEqual(diagnostics["validation_error"], "intent_unknown_source")
        self.assertNotIn("invented_source", json.dumps(diagnostics))

    def test_inconsistent_action_plan_cannot_publish(self):
        bad = copy.deepcopy(V2_RESULT)
        bad["action_plan"]["related_issue_comments"]["decision"] = "publish"
        job, gh, _ = self.run_v2([INTENT, bad, ProviderError("unavailable")])
        self.assertEqual(job.status, "attention")
        self.assertEqual(gh.writes, [])
        self.assertFalse(hasattr(job, "review_checkpoint"))

    def test_usage_retains_both_phases_without_double_counting_updates(self):
        def model(provider, context, cancelled, **kwargs):
            callback = kwargs["on_response"]
            callback({"input_tokens": 10, "output_tokens": 1})
            callback({"input_tokens": 20, "output_tokens": 2})
            return INTENT if kwargs["task"] == "intent" else copy.deepcopy(V2_RESULT)

        job, _, _ = self.run_v2(model)
        self.assertEqual(job.status, "completed")
        usage = public_usage(job.model_attempts.get().diagnostics)
        self.assertEqual(usage["session_count"], 2)
        self.assertEqual(usage["totals"]["input_tokens"], 40)
        self.assertEqual([p["phase"] for p in usage["passes"]], ["intent", "review"])

    def test_review_failure_without_diagnostics_does_not_reuse_intent_token_counts(self):
        calls = 0

        def model(provider, context, cancelled, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                kwargs["on_response"]({"input_tokens": 20, "output_tokens": 2})
                return INTENT
            raise ProviderError("unavailable")

        job, _, _ = self.run_v2(model)
        usage = public_usage(job.model_attempts.order_by("id").first().diagnostics)
        self.assertEqual(usage["session_count"], 2)
        self.assertIsNone(usage["totals"]["input_tokens"])
        self.assertIsNone(usage["passes"][1]["input_tokens"])

    def test_intent_quota_never_calls_review_or_publishes(self):
        job, gh, model = self.run_v2(ProviderError("quota"))
        self.assertEqual(job.status, "quota")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.kwargs["task"], "intent")
        self.assertEqual(gh.writes, [])

    def test_cancellation_between_phases_prevents_review(self):
        def model(*args, **kwargs):
            Job.objects.filter(status="running").update(status="cancelled")
            return INTENT

        job, gh, model = self.run_v2(model)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(gh.writes, [])

    def test_delivery_retry_reuses_intent_and_review_without_model_calls(self):
        from core.delivery import queue_retry
        from core.github import IntegrationError
        from core.worker import run_job
        from core.workflow import claim

        with patch(
            "core.publication.terminal_transition",
            side_effect=IntegrationError("GitHub request failed (HTTP 404)."),
        ):
            job, gh, _ = self.run_v2([INTENT, copy.deepcopy(V2_RESULT)])
        self.assertEqual(job.status, "attention")
        self.assertEqual(queue_retry(job).pk, job.pk)
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

from django.test import TestCase, override_settings

from core.activity import message, record
from core.flow import snapshot
from core.models import Credential, Job, Organization, PullRequest, Repository


@override_settings(GRAPH_REPOSITORIES=[])
class WorkflowV2Tests(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Org", slug="org")
        credential = Credential.objects.create(name="github", encrypted_value="unused")
        repo = Repository.objects.create(
            organization=org,
            full_name="org/repo",
            reviewer="owner",
            github_credential=credential,
            webhook_credential=credential,
        )
        pr = PullRequest.objects.create(repository=repo, number=1, head_sha="a")
        self.job = Job.objects.create(
            pr=pr, generation=1, head_sha="a", actor="dev", status="running"
        )

    def stages(self):
        return {
            stage["id"]: stage
            for stage in snapshot(self.job, list(self.job.activity.all()))["stages"]
        }

    def test_new_jobs_snapshot_explicit_v2_steps(self):
        self.assertEqual(self.job.workflow_revision, "v2.1")
        self.assertEqual(
            list(self.stages()),
            [
                "files",
                "discussion",
                "docs",
                "intent",
                "checks",
                "pr_summary",
                "inline_findings",
                "discussion_replies",
                "related_pr_comments",
                "related_issue_comments",
                "labels",
            ],
        )
        self.assertTrue(all(stage["state"] == "pending" for stage in self.stages().values()))

    def test_delivery_not_needed_is_explicit_and_safe(self):
        for prefix, key in [
            ("inline", "inline_findings"),
            ("discussion", "discussion_replies"),
            ("related_pr", "related_pr_comments"),
            ("related_issue", "related_issue_comments"),
        ]:
            record(self.job, f"{prefix}_started")
            record(self.job, f"{prefix}_unused", count=0)
            stage = self.stages()[key]
            self.assertEqual(stage["state"], "not_needed")
            self.assertEqual(stage["count"], 0)
            self.assertIsNotNone(stage["finished_at"])
        with self.assertRaises(ValueError):
            record(self.job, "inline_unused", detail="Private model rationale")
        self.assertNotIn("quota", message(self.job.activity.last()))

    def test_completed_receipts_survive_retry_and_label_failure(self):
        record(self.job, "starting")
        record(self.job, "intent_started")
        record(self.job, "intent_ready")
        record(self.job, "summary_started")
        record(self.job, "summary_done", count=1)
        record(self.job, "inline_done", count=2)
        record(self.job, "labels_started")
        self.job.status = "attention"
        self.assertEqual(self.stages()["labels"]["state"], "attention")
        self.assertEqual(self.stages()["pr_summary"]["state"], "done")
        record(self.job, "starting")
        record(self.job, "delivery_resume")
        record(self.job, "summary_started")
        self.job.status = "running"
        self.assertEqual(self.stages()["pr_summary"]["state"], "done")
        self.assertEqual(self.stages()["inline_findings"]["count"], 2)
        self.assertEqual(self.stages()["intent"]["state"], "done")
        record(self.job, "labels_done", count=1)
        self.job.status = "completed"
        self.assertEqual(self.stages()["labels"]["state"], "done")

    def test_later_failed_intent_does_not_inherit_first_provider_success(self):
        record(self.job, "intent_started")
        record(self.job, "intent_ready")
        record(self.job, "intent_started")
        self.job.status = "failed"
        self.assertEqual(self.stages()["intent"]["state"], "attention")
        self.assertIsNone(self.stages()["intent"]["finished_at"])

    def test_ended_unfinished_category_does_not_claim_success_or_count(self):
        record(self.job, "inline_started")
        self.job.status = "cancelled"
        stage = self.stages()["inline_findings"]
        self.assertEqual(stage["state"], "cancelled")
        self.assertIsNone(stage["count"])
        self.assertEqual(self.stages()["pr_summary"]["state"], "pending")

    def test_later_completed_intent_reports_its_own_timing(self):
        record(self.job, "intent_started")
        record(self.job, "intent_ready")
        record(self.job, "intent_started")
        started = self.job.activity.last()
        record(self.job, "intent_ready")
        finished = self.job.activity.last()
        stage = self.stages()["intent"]
        self.assertEqual(stage["state"], "done")
        self.assertEqual(stage["started_at"], started.created_at)
        self.assertEqual(stage["finished_at"], finished.created_at)

    def test_saved_historical_versions_ignore_new_events(self):
        legacy_steps = ["files", "discussion", "docs", "checks", "publication"]
        record(self.job, "intent_ready")
        record(self.job, "summary_done", count=1)
        for revision in ["v1", "v1.1"]:
            with self.subTest(revision=revision):
                self.job.workflow_revision = revision
                self.job.workflow_steps = legacy_steps
                self.assertEqual(list(self.stages()), legacy_steps)

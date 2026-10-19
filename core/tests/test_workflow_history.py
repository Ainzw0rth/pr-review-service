from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core.activity import record
from core.flow import snapshot
from core.models import Credential, Job, Organization, PullRequest, Repository


@override_settings(SECURE_SSL_REDIRECT=False, GRAPH_REPOSITORIES=[])
class WorkflowHistoryTests(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Org", slug="org")
        credential = Credential.objects.create(name="github", encrypted_value="unused")
        self.repo = Repository.objects.create(
            organization=org,
            full_name="org/repo",
            reviewer="owner",
            github_credential=credential,
            webhook_credential=credential,
        )
        self.pr = PullRequest.objects.create(repository=self.repo, number=1, head_sha="a")

    def job(self):
        return Job.objects.create(
            pr=self.pr, generation=self.pr.jobs.count() + 1, head_sha="a", actor="dev"
        )

    def test_saved_plan_survives_repository_changes_and_later_workflow_revision(self):
        old = self.job()
        Job.objects.filter(pk=old.pk).update(
            workflow_revision="v1.1",
            workflow_steps=["files", "discussion", "docs", "checks", "publication"],
        )
        self.repo.test_command = "python -m unittest"
        self.repo.sheet_config = {"spreadsheet_id": "example"}
        self.repo.save()
        with override_settings(GRAPH_REPOSITORIES=["org/repo"]):
            newer = self.job()
            old.status = "completed"
            old.save(update_fields=["status"])
            old.refresh_from_db()
            newer.refresh_from_db()
            self.assertEqual(old.workflow_revision, "v1.1")
            self.assertEqual(newer.workflow_revision, "v2.1")
            self.assertEqual(
                [n["id"] for n in snapshot(old, [])["stages"]],
                ["files", "discussion", "docs", "checks", "publication"],
            )
            self.assertEqual(
                [n["id"] for n in snapshot(newer, [])["stages"]],
                [
                    "files",
                    "discussion",
                    "docs",
                    "graph",
                    "intent",
                    "checks",
                    "tests",
                    "pr_summary",
                    "inline_findings",
                    "discussion_replies",
                    "related_pr_comments",
                    "related_issue_comments",
                    "labels",
                    "sheet",
                ],
            )

    def test_optional_steps_remain_visible_after_configuration_is_removed(self):
        self.repo.test_command = "python -m unittest"
        self.repo.sheet_config = {"spreadsheet_id": "example"}
        self.repo.save()
        with override_settings(GRAPH_REPOSITORIES=["org/repo"]):
            job = self.job()
        self.repo.test_command = ""
        self.repo.sheet_config = {}
        self.repo.save()
        job.refresh_from_db()
        record(job, "tests")
        record(job, "tests_passed")
        stages = {n["id"]: n for n in snapshot(job, list(job.activity.all()))["stages"]}
        self.assertIn("graph", stages)
        self.assertIn("sheet", stages)
        self.assertEqual(stages["tests"]["state"], "done")

    def test_saved_step_membership_and_order_control_projection(self):
        job = self.job()
        # A historical plan without a stage added by a later application release.
        Job.objects.filter(pk=job.pk).update(workflow_steps=["checks", "files", "publication"])
        job.refresh_from_db()
        self.assertEqual(
            [n["id"] for n in snapshot(job, [])["stages"]],
            ["checks", "files", "publication"],
        )

    def test_attempt_page_and_feed_distinguish_workflow_from_model_configuration(self):
        job = self.job()
        owner = get_user_model().objects.create_user(username="owner", is_superuser=True)
        self.client.force_login(owner)
        response = self.client.get(f"/jobs/{job.pk}/")
        self.assertContains(response, "Workflow v2.1")
        self.assertContains(response, "Model configuration")
        flow = self.client.get(f"/jobs/{job.pk}/activity/").json()["flow"]
        self.assertEqual(flow["revision"], "v2.1")

    def test_legacy_row_is_not_assigned_a_version_on_save_or_read(self):
        job = self.job()
        Job.objects.filter(pk=job.pk).update(workflow_revision="", workflow_steps=[])
        self.repo.test_command = "python -m unittest"
        self.repo.sheet_config = {"spreadsheet_id": "example"}
        self.repo.save()
        job.refresh_from_db()
        record(job, "graph_started")
        record(job, "context_ready")
        job.status = "completed"
        job.save()
        owner = get_user_model().objects.create_user(username="owner", is_superuser=True)
        self.client.force_login(owner)
        self.assertContains(self.client.get(f"/jobs/{job.pk}/"), "Legacy / version unknown")
        flow = self.client.get(f"/jobs/{job.pk}/activity/").json()["flow"]
        self.assertIsNone(flow["revision"])
        self.assertIn("graph", [n["id"] for n in flow["stages"]])
        self.assertNotIn("tests", [n["id"] for n in flow["stages"]])
        self.assertNotIn("sheet", [n["id"] for n in flow["stages"]])
        job.refresh_from_db()
        self.assertEqual(job.workflow_revision, "")
        self.assertEqual(job.workflow_steps, [])

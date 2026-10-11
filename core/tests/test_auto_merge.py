from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from core.models import ApprovalRecord, Job, Receipt
from core.tests.test_workflow import WorkflowTests


@override_settings(LIVE_WRITES=True)
class AutoMergeTests(TestCase):
    def setUp(self):
        WorkflowTests.setUp(self)
        self.repo.auto_approve = True
        self.repo.auto_merge = True
        self.repo.save()
        from core.models import PullRequest, ReviewCheckpoint

        pr = PullRequest.objects.create(
            repository=self.repo, number=1, head_sha="a" * 40, generation=1
        )
        self.job = Job.objects.create(
            pr=pr, head_sha=pr.head_sha, generation=1, actor="dev", status="completed"
        )
        ReviewCheckpoint.objects.create(
            job=self.job, stage="done", state={"blocked": False, "review_event": "APPROVE"}
        )
        ApprovalRecord.objects.create(
            job=self.job,
            repository=self.repo,
            number=1,
            head_sha=pr.head_sha,
            marker="tag",
            actor="louis",
            review_id="42",
        )
        self.gh = Mock()
        self.gh.pull.return_value = {
            "state": "open",
            "head": {"sha": pr.head_sha},
            "draft": False,
            "labels": [{"name": "approved"}],
        }
        self.gh.pages.return_value = [
            {
                "id": 42,
                "state": "APPROVED",
                "commit_id": pr.head_sha,
                "user": {"login": "louis"},
                "body": "tag",
            }
        ]
        self.gh.graphql.return_value = {
            "repository": {
                "pullRequest": {
                    "headRefOid": pr.head_sha,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "reviewDecision": "APPROVED",
                }
            }
        }
        self.gh.put.return_value = {"merged": True, "sha": "b" * 40}
        self.checks = patch("core.merging.check_required", return_value={"passed": True}).start()
        self.addCleanup(patch.stopall)

    def merge(self):
        from core.merging import merge_approved

        return merge_approved(self.gh, self.job, "ready")

    def test_pins_sha_and_records_success(self):
        self.merge()
        self.gh.put.assert_called_once_with(
            "/pulls/1/merge", {"sha": self.job.head_sha, "merge_method": "squash"}
        )
        self.assertTrue(Receipt.objects.filter(job=self.job, key="merge").exists())
        self.merge()
        self.gh.put.assert_called_once()

    def test_defaults_off_and_existing_job_not_upgraded(self):
        self.assertFalse(self.repo._meta.get_field("auto_merge").default)
        Job.objects.filter(pk=self.job.pk).update(auto_merge=False)
        self.merge()
        self.gh.put.assert_not_called()

    def test_live_disable_prevents_merge(self):
        self.repo.auto_merge = False
        self.repo.save()
        self.merge()
        self.gh.put.assert_not_called()

    def test_new_commit_prevents_merge(self):
        self.gh.pull.return_value["head"]["sha"] = "c" * 40
        self.merge()
        self.gh.put.assert_not_called()

    def test_failed_ci_prevents_merge(self):
        self.checks.return_value = {"passed": False}
        self.merge()
        self.gh.put.assert_not_called()

    def test_unapproved_or_blocked_graphql_prevents_merge(self):
        for field, value in [
            ("reviewDecision", "CHANGES_REQUESTED"),
            ("mergeStateStatus", "BLOCKED"),
            ("mergeable", "UNKNOWN"),
        ]:
            with self.subTest(field=field):
                remote = self.gh.graphql.return_value["repository"]["pullRequest"]
                old = remote[field]
                remote[field] = value
                self.merge()
                self.gh.put.assert_not_called()
                remote[field] = old

    def test_dismissed_service_approval_prevents_merge(self):
        self.gh.pages.return_value[0]["state"] = "DISMISSED"
        self.merge()
        self.gh.put.assert_not_called()

    def test_feedback_never_merges(self):
        from core.merging import merge_approved

        merge_approved(self.gh, self.job, "feedback")
        self.gh.put.assert_not_called()

    def test_uncertain_response_is_not_blindly_retried(self):
        from core.github import IntegrationError

        self.gh.put.side_effect = IntegrationError("GitHub request unavailable or malformed.")
        self.merge()
        self.merge()
        self.gh.put.assert_called_once()
        self.job.refresh_from_db()
        self.assertIn("could not be confirmed", self.job.reason)

    def test_false_response_is_not_success(self):
        self.gh.put.return_value = {"merged": False}
        self.merge()
        self.assertFalse(Receipt.objects.filter(job=self.job, key="merge").exists())
        self.job.refresh_from_db()
        self.assertIn("did not merge", self.job.reason)

    def test_disabled_live_writes_never_merges(self):
        with override_settings(LIVE_WRITES=False):
            self.merge()
        self.gh.put.assert_not_called()

    def test_owner_disable_during_checks_prevents_merge(self):
        def state(*args):
            type(self.repo).objects.filter(pk=self.repo.pk).update(auto_merge=False)
            return {
                "repository": {
                    "pullRequest": {
                        "headRefOid": self.job.head_sha,
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "reviewDecision": "APPROVED",
                    }
                }
            }

        self.gh.graphql.side_effect = state
        self.merge()
        self.gh.put.assert_not_called()

    def test_saved_method_used_and_not_changed_by_later_settings(self):
        Job.objects.filter(pk=self.job.pk).update(merge_method="rebase")
        self.merge()
        self.assertEqual(self.gh.put.call_args.args[1]["merge_method"], "rebase")

    def test_superseded_generation_never_merges(self):
        type(self.job.pr).objects.filter(pk=self.job.pr.pk).update(generation=2)
        self.merge()
        self.gh.put.assert_not_called()

    def test_worker_invokes_merge_only_after_delivery_and_completion(self):
        from core.worker import _deliver_and_finish

        def invoke(github, job, outcome):
            job.refresh_from_db()
            self.assertEqual(job.status, "completed")
            self.assertEqual(outcome, "ready")

        with (
            patch("core.delivery.deliver", return_value="ready"),
            patch("core.sheets.sync_status"),
            patch("core.merging.merge_approved", side_effect=invoke) as merge,
        ):
            _deliver_and_finish(self.gh, self.job, Mock())
        merge.assert_called_once()


@override_settings(SECURE_SSL_REDIRECT=False, DASHBOARD_RETRY_USER_ID=1)
class MergeSettingsTests(TestCase):
    def setUp(self):
        WorkflowTests.setUp(self)
        from django.contrib.auth import get_user_model

        self.owner = get_user_model().objects.create_user(pk=1, username="owner", is_superuser=True)
        self.client.force_login(self.owner)

    def save(self, **fields):
        version = self.client.get("/settings/").context["version"]
        return self.client.post("/settings/", {"version": version, **fields})

    def test_default_unchecked_and_owner_can_enable(self):
        response = self.client.get("/settings/")
        self.assertContains(response, "Automatically merge approved PRs")
        self.assertFalse(self.repo.auto_merge)
        response = self.save(
            **{
                f"auto_approve_{self.repo.pk}": "on",
                f"auto_merge_{self.repo.pk}": "on",
                f"merge_method_{self.repo.pk}": "merge",
            }
        )
        self.assertEqual(response.status_code, 302)
        self.repo.refresh_from_db()
        self.assertTrue(self.repo.auto_merge)
        self.assertEqual(self.repo.merge_method, "merge")

    def test_approval_required_and_invalid_method_rejected(self):
        self.assertEqual(self.save(**{f"auto_merge_{self.repo.pk}": "on"}).status_code, 400)
        self.assertEqual(self.save(**{f"merge_method_{self.repo.pk}": "bad"}).status_code, 400)
        self.repo.refresh_from_db()
        self.assertFalse(self.repo.auto_merge)

    def test_other_superuser_cannot_enable(self):
        from django.contrib.auth import get_user_model

        self.client.force_login(
            get_user_model().objects.create_user(username="other", is_superuser=True)
        )
        self.assertEqual(
            self.client.post("/settings/", {f"auto_merge_{self.repo.pk}": "on"}).status_code, 403
        )

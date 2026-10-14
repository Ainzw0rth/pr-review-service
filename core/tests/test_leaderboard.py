from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from core.assignees import sync_assignees
from core.models import (
    Credential,
    Job,
    Membership,
    Organization,
    PRAssignee,
    PullRequest,
    Repository,
    ReviewCheckpoint,
)


@override_settings(SECURE_SSL_REDIRECT=False)
class LeaderboardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("member")
        org = Organization.objects.create(name="Team", slug="team")
        Membership.objects.create(user=self.user, organization=org)
        credential = Credential.objects.create(name="github", encrypted_value="private")
        self.repo = Repository.objects.create(
            organization=org,
            full_name="team/repo",
            reviewer="owner",
            github_credential=credential,
            webhook_credential=credential,
        )
        self.pr = PullRequest.objects.create(repository=self.repo, number=1, head_sha="a" * 40)
        self.client.force_login(self.user)

    def completed(self, pr=None, generation=1, days=0):
        job = Job.objects.create(
            pr=pr or self.pr,
            generation=generation,
            head_sha="a" * 40,
            actor="trigger-person",
            status="completed",
            finished_at=timezone.now() - timedelta(days=days),
        )
        ReviewCheckpoint.objects.create(job=job, stage="done", encrypted_payload="private")
        return job

    def assign(self, pr=None, users=None):
        sync_assignees(
            pr or self.pr,
            {
                "assignees": users
                if users is not None
                else [
                    {"id": 42, "login": "alice"},
                    {"id": 43, "login": "bob"},
                ]
            },
        )

    def test_counts_unique_published_prs_and_multi_assignees_not_retries(self):
        self.assign()
        self.completed()
        self.completed(generation=2)
        response = self.client.get("/leaderboard/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(
            [(r["login"], r["count"]) for r in response.context["leaders"]],
            [("alice", 1), ("bob", 1)],
        )
        self.assertEqual(sum(d["count"] for d in response.context["days"]), 1)
        self.assertContains(response, "avatars.githubusercontent.com/u/42")
        self.assertNotContains(response, "trigger-person")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_scope_and_incomplete_jobs_do_not_count(self):
        self.assign()
        self.completed()
        other = Organization.objects.create(name="Secret", slug="secret")
        hidden = Repository.objects.create(
            organization=other,
            full_name="secret/repo",
            reviewer="owner",
            github_credential=self.repo.github_credential,
            webhook_credential=self.repo.webhook_credential,
        )
        hidden_pr = PullRequest.objects.create(repository=hidden, number=2, head_sha="b" * 40)
        self.assign(hidden_pr, [{"id": 99, "login": "secret-person"}])
        self.completed(hidden_pr)
        failed = PullRequest.objects.create(repository=self.repo, number=3, head_sha="c" * 40)
        self.assign(failed, [{"id": 91, "login": "failed-person"}])
        Job.objects.create(pr=failed, generation=1, head_sha="c" * 40, status="completed")
        response = self.client.get("/leaderboard/")
        self.assertEqual(response.context["total"], 1)
        self.assertNotContains(response, "secret-person")
        self.assertNotContains(response, "failed-person")
        Membership.objects.all().delete()
        self.assertEqual(self.client.get("/leaderboard/").context["total"], 0)
        self.client.logout()
        self.assertEqual(self.client.get("/leaderboard/").status_code, 302)

    def test_unassigned_unknown_and_window(self):
        self.completed(days=45)
        self.assertEqual(self.client.get("/leaderboard/", {"period": "30"}).context["total"], 0)
        response = self.client.get("/leaderboard/", {"period": "invalid"})
        self.assertEqual(response.context["period"], 90)
        self.assertEqual(response.context["unknown"], 1)
        self.assign(users=[])
        response = self.client.get("/leaderboard/")
        self.assertEqual(response.context["unassigned"], 1)
        self.assertEqual(response.context["unknown"], 0)
        self.assertEqual(response.context["leaders"], [])
        self.assertEqual(self.client.post("/leaderboard/").status_code, 405)

    def test_sync_replaces_assignments_without_changing_audit_actor(self):
        job = self.completed()
        self.assign()
        self.assign(users=[{"id": 42, "login": "renamed-alice"}])
        self.assertEqual(list(self.pr.assignees.values_list("login", flat=True)), ["renamed-alice"])
        sync_assignees(self.pr, {})
        self.assertEqual(self.pr.assignees.count(), 1)
        self.assign(users=[])
        self.assertEqual(self.pr.assignees.count(), 0)
        job.refresh_from_db()
        self.assertEqual(job.actor, "trigger-person")
        self.pr.refresh_from_db()
        self.assertIsNotNone(self.pr.assignees_synced_at)

    def test_invalid_profiles_leave_previous_snapshot_intact(self):
        self.assign()
        for users in [
            [{"id": True, "login": "alice"}],
            [{"id": 42, "login": "bad/<script>"}],
            None,
        ]:
            with self.assertRaises(ValueError):
                sync_assignees(self.pr, {"assignees": users})
            self.assertEqual(self.pr.assignees.count(), 2)

    def test_filter_uses_assignee_not_actor_and_preserves_pagination(self):
        self.assign()
        self.completed()
        response = self.client.get("/", {"assignee": "42"})
        self.assertEqual(response.context["page"].paginator.count, 1)
        self.assertContains(response, "Assignee")
        self.assertEqual(
            self.client.get("/", {"assignee": "999"}).context["page"].paginator.count, 0
        )
        self.assertEqual(
            self.client.get("/", {"assignee": "unknown"}).context["page"].paginator.count, 0
        )
        detail = self.client.get(f"/jobs/{self.pr.jobs.first().id}/")
        self.assertContains(detail, "Triggered by")
        self.assertContains(detail, "trigger-person")

    @patch("core.management.commands.sync_pr_assignees.GitHub")
    def test_backfill_only_reads_github_and_does_not_enqueue(self, github):
        self.completed()
        github.return_value.__enter__.return_value.pull.return_value = {
            "number": 1,
            "assignees": [{"id": 42, "login": "alice"}],
        }
        call_command("sync_pr_assignees", verbosity=0)
        self.assertEqual(PRAssignee.objects.get().login, "alice")
        self.assertEqual(Job.objects.count(), 1)
        call_command("sync_pr_assignees", verbosity=0)
        self.assertEqual(github.return_value.__enter__.return_value.pull.call_count, 1)

    def test_renamed_account_is_one_filter_option_and_one_rank(self):
        self.assign(users=[{"id": 42, "login": "old-login"}])
        self.completed()
        second = PullRequest.objects.create(repository=self.repo, number=2, head_sha="b" * 40)
        self.assign(second, [{"id": 42, "login": "new-login"}])
        self.completed(second)
        response = self.client.get("/")
        self.assertEqual(response.context["assignees"], [{"github_id": 42, "login": "new-login"}])
        leaders = self.client.get("/leaderboard/").context["leaders"]
        self.assertEqual([(row["login"], row["count"]) for row in leaders], [("new-login", 2)])

    @patch("core.management.commands.sync_pr_assignees.GitHub")
    def test_backfill_cannot_overwrite_concurrent_webhook_snapshot(self, github):
        self.completed()

        def fetched(number):
            self.assign(users=[{"id": 43, "login": "new-assignee"}])
            return {"number": number, "assignees": [{"id": 42, "login": "old-assignee"}]}

        github.return_value.__enter__.return_value.pull.side_effect = fetched
        call_command("sync_pr_assignees", verbosity=0)
        self.assertEqual(self.pr.assignees.get().login, "new-assignee")

    def test_bot_assignment_is_supported(self):
        self.assign(users=[{"id": 44, "login": "copilot-swe-agent[bot]"}])
        self.assertEqual(self.pr.assignees.get().login, "copilot-swe-agent[bot]")

    @patch("django.utils.timezone.now")
    def test_daily_chart_uses_jakarta_midnight(self, now):
        now.return_value = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
        self.assign()
        job = self.completed()
        # 17:30 UTC belongs to the following Jakarta day (00:30 WIB).
        Job.objects.filter(pk=job.pk).update(finished_at=datetime(2026, 10, 14, 17, 30, tzinfo=UTC))
        response = self.client.get("/leaderboard/", {"period": "30"})
        self.assertEqual(str(response.context["days"][-1]["date"]), "2026-10-15")
        self.assertEqual(response.context["days"][-1]["count"], 1)
        self.assertEqual(response.context["days"][-2]["count"], 0)

    def test_requester_ranking_includes_unassigned_and_deduplicates_per_person(self):
        self.completed()
        self.completed(generation=2)
        other = self.completed(generation=3)
        other.actor = "other-person"
        other.save()
        case = self.completed(generation=4)
        case.actor = "TRIGGER-PERSON"
        case.save()
        response = self.client.get("/leaderboard/", {"group": "requester", "period": "30"})
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(len(response.context["leaders"]), 2)
        self.assertEqual([r["count"] for r in response.context["leaders"]], [1, 1])
        self.assertContains(response, "Requestors")
        self.assertContains(response, "group=requester")
        self.assertContains(response, "?actor=")
        self.assertEqual(self.client.get("/leaderboard/").context["leaders"], [])

    def test_requester_filter_combines_with_assignee_and_remains_scoped(self):
        self.assign()
        self.completed()
        response = self.client.get("/", {"actor": "trigger-person", "assignee": "42"})
        self.assertEqual(response.context["page"].paginator.count, 1)
        self.assertContains(response, "Requested by")
        self.assertEqual(
            self.client.get("/", {"actor": "other-person", "assignee": "42"})
            .context["page"]
            .paginator.count,
            0,
        )
        Membership.objects.all().delete()
        response = self.client.get("/", {"actor": "trigger-person"})
        self.assertEqual(response.context["actors"], [])
        self.assertEqual(response.context["page"].paginator.count, 0)
        self.assertEqual(
            self.client.get("/leaderboard/", {"group": "requester"}).context["leaders"], []
        )

    def test_missing_requester_not_guessed_and_invalid_group_defaults_to_assignee(self):
        job = self.completed()
        job.actor = ""
        job.save()
        response = self.client.get("/leaderboard/", {"group": "requester"})
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["unknown"], 1)
        self.assertEqual(response.context["leaders"], [])
        self.assertEqual(
            self.client.get("/leaderboard/", {"group": "invalid"}).context["group"], "assignee"
        )

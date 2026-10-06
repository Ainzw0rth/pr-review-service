import hashlib
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core.models import (
    AccessLink,
    Credential,
    Job,
    Membership,
    Organization,
    PullRequest,
    Repository,
)


@override_settings(SECURE_SSL_REDIRECT=False)
class DashboardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "member", password="Uncommon-stone-bridge-41"
        )
        self.org = Organization.objects.create(name="Visible team", slug="visible")
        Membership.objects.create(user=self.user, organization=self.org)
        credential = Credential.objects.create(name="test", encrypted_value="unused")

        def job(org, repo):
            repository = Repository.objects.create(
                organization=org,
                full_name=repo,
                reviewer="louis",
                github_credential=credential,
                webhook_credential=credential,
            )
            pr = PullRequest.objects.create(repository=repository, number=17, head_sha="a" * 40)
            return Job.objects.create(pr=pr, generation=1, head_sha="a" * 40, actor="developer")

        self.visible = job(self.org, "visible/project")
        self.hidden = job(
            Organization.objects.create(name="Private", slug="private"), "private/secret-project"
        )

    def link(self, **kwargs):
        token = "test-token"
        AccessLink.objects.create(
            digest=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(hours=1),
            **kwargs,
        )
        return "/access/" + token + "/"

    def test_members_see_only_their_organization(self):
        self.client.force_login(self.user)
        response = self.client.get("/")
        self.assertContains(response, "visible/project")
        self.assertNotContains(response, "private/secret-project")
        self.assertEqual(self.client.get(f"/jobs/{self.hidden.id}/").status_code, 404)

    def test_activity_requires_membership_and_is_read_only(self):
        url = f"/jobs/{self.visible.id}/activity/"
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.client.get(f"/jobs/{self.hidden.id}/activity/").status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 405)
        Membership.objects.filter(user=self.user).delete()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_provider_diagnostics_are_not_in_member_dashboard_or_feed(self):
        self.visible.provider_diagnostics = {"request_id": "req_admin_only_identifier"}
        self.visible.save()
        self.client.force_login(self.user)
        for suffix in ("", "activity/"):
            response = self.client.get(f"/jobs/{self.visible.id}/" + suffix)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "req_admin_only_identifier")

    def test_flow_nodes_aggregate_counters_and_preserve_model_attempts(self):
        from core.activity import record
        from core.models import ModelAttempt, Provider

        provider = Provider.objects.create(
            name="Opus", kind="anthropic", model="opus-model", credential=Credential.objects.first()
        )
        self.visible.status = "attention"
        self.visible.save()
        for code, count in [
            ("context", None),
            ("files", 1),
            ("files", 24),
            ("threads", None),
            ("related", None),
            ("docs", 27),
            ("context_ready", None),
            ("checks_passed", None),
            ("model", None),
            ("models_exhausted", None),
        ]:
            record(self.visible, code, count=count)
        ModelAttempt.objects.create(
            job=self.visible,
            provider=provider,
            provider_name="Opus",
            model="opus-model",
            status="refused",
            finished_at=timezone.now(),
            diagnostics={"refusal_category": "cyber", "request_id": "req_private"},
        )
        self.client.force_login(self.user)
        response = self.client.get(f"/jobs/{self.visible.pk}/activity/")
        flow = response.json()["flow"]
        stages = {stage["id"]: stage for stage in flow["stages"]}
        self.assertEqual(stages["files"]["count"], 24)
        self.assertEqual(stages["docs"]["count"], 27)
        self.assertEqual(stages["checks"]["state"], "done")
        self.assertEqual(stages["publication"]["state"], "pending")
        self.assertEqual(flow["models"][0]["attempts"][0]["state"], "refused")
        self.assertNotContains(response, "req_private")

    def test_requester_filter_is_scoped_and_combines_with_status(self):
        self.visible.actor = "alice"
        self.visible.save()
        self.client.force_login(self.user)
        response = self.client.get("/", {"actor": "alice", "status": self.visible.status})
        self.assertEqual(list(response.context["page"]), [self.visible])
        self.assertEqual(list(self.client.get("/", {"actor": "nobody"}).context["page"]), [])
        self.assertEqual(list(response.context["actors"]), ["alice"])

    def test_cli_progress_exposes_only_bounded_operational_counts(self):
        from core.models import ModelAttempt, Provider

        provider = Provider.objects.create(name="Sol CLI", kind="codex_cli", model="gpt-6.1-sol")
        attempt = ModelAttempt.objects.create(
            job=self.visible,
            provider=provider,
            provider_name=provider.name,
            model=provider.model,
            diagnostics={
                "transport": "codex_cli",
                "events_received": 12,
                "evidence_read": 3,
                "evidence_total": 8,
                "reasoning": "private-model-text",
            },
        )
        self.client.force_login(self.user)
        url = f"/jobs/{self.visible.pk}/activity/"
        response = self.client.get(url)
        model = next(
            m for m in response.json()["flow"]["models"] if m["provider_id"] == provider.pk
        )
        self.assertEqual(model["backend"], "codex_cli")
        self.assertEqual(model["attempts"][0]["evidence_read"], 3)
        self.assertNotContains(response, "private-model-text")
        attempt.diagnostics.update(evidence_read=-1, evidence_total=1_000_001)
        attempt.save()
        model = next(
            m
            for m in self.client.get(url).json()["flow"]["models"]
            if m["provider_id"] == provider.pk
        )
        self.assertIsNone(model["attempts"][0]["evidence_total"])
        self.assertIsNone(model["attempts"][0]["evidence_read"])

    def test_old_activity_does_not_invent_model_retries(self):
        from core.activity import record
        from core.models import Provider

        self.visible.provider = Provider.objects.create(
            name="Old model",
            kind="anthropic",
            model="old-model",
            credential=Credential.objects.first(),
        )
        self.visible.status = "attention"
        self.visible.reason = "Provider refused the review; admin inspection required."
        self.visible.save()
        record(self.visible, "model")
        record(self.visible, "model_attention")
        self.client.force_login(self.user)
        flow = self.client.get(f"/jobs/{self.visible.pk}/activity/").json()["flow"]
        self.assertTrue(flow["legacy"])
        self.assertEqual(len(flow["models"]), 1)
        self.assertEqual(flow["models"][0]["state"], "refused")

    def test_activity_is_bounded_and_contains_only_operational_messages(self):
        from core.activity import record
        from core.models import JobActivity

        with self.assertRaises(ValueError):
            record(self.visible, "secret-token-or-model-reasoning")
        with self.assertRaises(ValueError):
            record(self.visible, "files", count="private-source-code")
        record(self.visible, "files", count=7)
        JobActivity.objects.bulk_create(
            [JobActivity(job=self.visible, code="checks") for _ in range(130)]
        )
        self.client.force_login(self.user)
        response = self.client.get(f"/jobs/{self.visible.id}/activity/")
        data = response.json()
        self.assertEqual(len(data["events"]), 100)
        self.assertIn("Checking required CI", data["events"][-1]["message"])
        self.assertNotIn("secret-token", response.content.decode())
        self.assertNotIn("private-source", response.content.decode())
        self.visible.delete()
        self.assertEqual(JobActivity.objects.count(), 0)

    def test_worker_heartbeat_throttles_and_preserves_cancellation(self):
        from unittest.mock import patch

        from core.activity import Progress

        self.visible.status = "running"
        self.visible.save()
        progress = Progress(self.visible)
        with patch("core.activity.current", return_value=True):
            self.assertFalse(progress.cancelled())
            self.visible.refresh_from_db()
            first = self.visible.heartbeat_at
            self.assertIsNotNone(first)
            self.assertFalse(progress.cancelled())
            self.visible.refresh_from_db()
            self.assertEqual(first, self.visible.heartbeat_at)
        with patch("core.activity.current", return_value=False):
            self.assertTrue(progress.cancelled())

    def test_member_cannot_change_or_cancel_job_or_enter_admin(self):
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.post(f"/jobs/{self.visible.id}/", {"status": "cancelled"}).status_code, 405
        )
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.visible.refresh_from_db()
        self.assertEqual(self.visible.status, "queued")

    def test_invite_creates_member_and_cannot_be_replayed(self):
        url = self.link(organization=self.org)
        response = self.client.post(
            url,
            {
                "username": "newmember",
                "password1": "Different-long-passphrase-73",
                "password2": "Different-long-passphrase-73",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Membership.objects.filter(user__username="newmember", organization=self.org).exists()
        )
        self.assertEqual(self.client.get(url).status_code, 410)

    def test_expired_invite_does_not_create_user(self):
        url = self.link(organization=self.org)
        AccessLink.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.post(url, {"username": "outsider"}).status_code, 410)
        self.assertFalse(get_user_model().objects.filter(username="outsider").exists())

    def test_reset_invalidates_previous_session(self):
        self.client.force_login(self.user)
        reset = Client()
        url = self.link(user=self.user)
        response = reset.post(
            url,
            {
                "new_password1": "Fresh-long-passphrase-91",
                "new_password2": "Fresh-long-passphrase-91",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Fresh-long-passphrase-91"))

    def test_login_requires_csrf_and_is_rate_limited(self):
        csrf = Client(enforce_csrf_checks=True)
        self.assertEqual(
            csrf.post("/login/", {"username": "member", "password": "bad"}).status_code, 403
        )
        for _ in range(10):
            self.client.post("/login/", {"username": "member", "password": "bad"})
        self.assertEqual(
            self.client.post(
                "/login/", {"username": "member", "password": "Uncommon-stone-bridge-41"}
            ).status_code,
            429,
        )

    def test_staff_without_superuser_cannot_access_configuration(self):
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)
        self.assertIn(self.client.get("/admin/").status_code, [302, 403])
        self.assertIn(
            self.client.post(
                "/admin/core/job/",
                {"action": "interrupt", "_selected_action": str(self.visible.id)},
            ).status_code,
            [302, 403],
        )
        self.visible.refresh_from_db()
        self.assertEqual(self.visible.status, "queued")

    def test_admin_interrupts_only_active_jobs(self):
        self.user.is_staff = self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.post(
            "/admin/core/job/", {"action": "interrupt", "_selected_action": str(self.visible.id)}
        )
        self.assertEqual(response.status_code, 302)
        self.visible.refresh_from_db()
        self.assertEqual(self.visible.status, "cancelled")
        self.assertIsNotNone(self.visible.finished_at)

    def test_revoked_invite_and_short_password_rejected(self):
        url = self.link(organization=self.org)
        response = self.client.post(
            url, {"username": "short", "password1": "short", "password2": "short"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(username="short").exists())
        AccessLink.objects.update(revoked=True)
        self.assertEqual(self.client.get(url).status_code, 410)

    def test_existing_member_can_join_another_organization(self):
        org = Organization.objects.get(slug="private")
        url = self.link(organization=org)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertTrue(Membership.objects.filter(user=self.user, organization=org).exists())

    def test_invite_action_stores_only_digest(self):
        import re

        self.user.is_staff = self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.post(
            "/admin/core/organization/", {"action": "invite", "_selected_action": str(self.org.id)}
        )
        self.assertEqual(response.status_code, 200)
        token = re.search(r"/access/([^/]+)/", response.content.decode()).group(1)
        link = AccessLink.objects.get()
        self.assertNotEqual(link.digest, token)
        self.assertEqual(link.digest, hashlib.sha256(token.encode()).hexdigest())
        self.assertIn("no-store", response["Cache-Control"])

    def test_credential_is_encrypted_and_never_rendered(self):
        from cryptography.fernet import Fernet

        from core.secrets import decrypt

        self.user.is_staff = self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        with override_settings(CREDENTIAL_KEY=Fernet.generate_key().decode()):
            response = self.client.post(
                "/admin/core/credential/add/",
                {"name": "API key", "secret": "extremely-private-value", "_save": "Save"},
            )
            self.assertEqual(response.status_code, 302)
            credential = Credential.objects.get(name="API key")
            self.assertNotIn("extremely-private-value", credential.encrypted_value)
            self.assertEqual(decrypt(credential.encrypted_value), "extremely-private-value")
            page = self.client.get(f"/admin/core/credential/{credential.pk}/change/")
            self.assertNotContains(page, "extremely-private-value")
            self.assertNotContains(page, credential.encrypted_value)
            self.client.post(
                f"/admin/core/credential/{credential.pk}/change/",
                {"name": "API key", "secret": "", "_save": "Save"},
            )
            credential.refresh_from_db()
            self.assertEqual(decrypt(credential.encrypted_value), "extremely-private-value")

    def test_reset_generation_revokes_older_reset_link(self):
        from core.views import issue_access_link

        first = issue_access_link(user=self.user)
        second = issue_access_link(user=self.user)
        self.assertEqual(self.client.get(first).status_code, 410)
        self.assertEqual(self.client.get(second).status_code, 200)

    def test_logout_requires_post(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_no_jobs_from_revoked_membership(self):
        self.client.force_login(self.user)
        Membership.objects.filter(user=self.user).delete()
        self.assertNotContains(self.client.get("/"), "visible/project")
        self.assertEqual(self.client.get(f"/jobs/{self.visible.pk}/").status_code, 404)

    def test_private_bootstrap_link_has_no_default_password(self):
        from io import StringIO

        from django.core.management import CommandError, call_command

        out = StringIO()
        call_command("bootstrap_admin", setup_link=True, stdout=out)
        admin = get_user_model().objects.get(username="louis")
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_staff)
        self.assertFalse(admin.has_usable_password())
        self.assertEqual(admin.email, "louiscaesapdf@gmail.com")
        response = self.client.post(
            out.getvalue().strip(),
            {
                "new_password1": "Private-initial-password-74",
                "new_password2": "Private-initial-password-74",
            },
        )
        self.assertEqual(response.status_code, 302)
        admin.refresh_from_db()
        self.assertTrue(admin.check_password("Private-initial-password-74"))
        with self.assertRaises(CommandError):
            call_command("bootstrap_admin", setup_link=True, stdout=StringIO())

    def test_long_running_attempt_remains_visible_but_old_history_does_not(self):
        old = timezone.now() - timedelta(days=91)
        Job.objects.filter(pk=self.visible.pk).update(status="running", created_at=old)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(f"/jobs/{self.visible.pk}/").status_code, 200)
        Job.objects.filter(pk=self.visible.pk).update(status="completed")
        self.assertEqual(self.client.get(f"/jobs/{self.visible.pk}/").status_code, 404)

    def test_webhook_admin_is_read_only_and_never_exposes_payload(self):
        from core.models import Webhook

        event = Webhook.objects.create(
            delivery="private-delivery",
            repository=self.visible.pr.repository,
            event="pull_request",
            payload={"secret": "payload-must-not-appear"},
            error="Reconciliation unavailable.",
        )
        self.client.force_login(self.user)
        for url in ["/admin/core/webhook/", f"/admin/core/webhook/{event.pk}/change/"]:
            self.assertIn(self.client.get(url).status_code, [302, 403])
        self.user.is_staff = self.user.is_superuser = True
        self.user.save()
        for url in ["/admin/core/webhook/", f"/admin/core/webhook/{event.pk}/change/"]:
            response = self.client.get(url)
            self.assertContains(response, "private-delivery")
            self.assertContains(response, "Reconciliation unavailable.")
            self.assertNotContains(response, "payload-must-not-appear")
            self.assertNotContains(response, 'name="payload"')
        self.assertEqual(self.client.get("/admin/core/webhook/add/").status_code, 403)
        self.assertEqual(
            self.client.post(
                f"/admin/core/webhook/{event.pk}/change/", {"error": "modified"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/admin/core/webhook/{event.pk}/delete/", {"post": "yes"}
            ).status_code,
            403,
        )
        event.refresh_from_db()
        self.assertEqual(event.error, "Reconciliation unavailable.")

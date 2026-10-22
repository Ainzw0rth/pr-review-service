from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from core.models import Credential, Repository
from core.secrets import decrypt, encrypt
from core.tests import test_workflow


@override_settings(
    SECURE_SSL_REDIRECT=False,
    DASHBOARD_RETRY_USER_ID=1,
    PUBLIC_URL="https://reviews.example.test",
    WEBHOOK_PUBLIC_URL="",
)
class RepositorySettingsTests(TestCase):
    def setUp(self):
        test_workflow.WorkflowTests.setUp(self)
        self.repo.github_credential.encrypted_value = encrypt("stored-token")
        self.repo.github_credential.save()
        self.owner = get_user_model().objects.create_user(username="owner", is_superuser=True)
        binding = override_settings(DASHBOARD_RETRY_USER_ID=self.owner.pk)
        binding.enable()
        self.addCleanup(binding.disable)
        self.client.force_login(self.owner)

    def data(self, **changes):
        return {
            "full_name": "org/new",
            "organization": self.repo.organization_id,
            "reviewer": "louis",
            "github_credential": self.repo.github_credential_id,
            "enabled": "on",
            "graph_enabled": "on",
            "review_label": "on review",
            "feedback_label": "feedback",
            "approved_label": "approved",
            "required_checks": "tests\nlint\n",
            **changes,
        }

    def test_add_preserves_local_repository_on_remote_failure_and_encrypts_separate_secret(self):
        with patch("core.repository_views.connect_repository", return_value=False):
            response = self.client.post("/settings/repositories/add/", self.data())
        self.assertEqual(response.status_code, 302)
        repo = Repository.objects.get(full_name="org/new")
        self.assertNotEqual(repo.webhook_credential_id, repo.github_credential_id)
        self.assertGreaterEqual(len(decrypt(repo.webhook_credential.encrypted_value)), 32)
        self.assertEqual(repo.required_checks, ["tests", "lint"])
        self.assertFalse(repo.auto_approve)
        self.assertFalse(repo.auto_merge)
        self.assertTrue(repo.graph_enabled)

    def test_form_validates_repository_paths_and_token_is_write_only(self):
        from core.forms import RepositorySettingsForm

        for name in ["../repo", "org/..", "https://github.com/org/repo", "org/repo/extra"]:
            form = RepositorySettingsForm(self.data(full_name=name))
            self.assertFalse(form.is_valid(), name)
        form = RepositorySettingsForm(self.data(github_token="new-private-token"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("new-private-token", str(form["github_token"]))

    def test_rejects_invalid_reviewer_token_and_case_insensitive_duplicate(self):
        from core.forms import RepositorySettingsForm

        for reviewer in ["@louis", "first last", "bad/name", "-start", "end-", "a" * 40]:
            form = RepositorySettingsForm(self.data(reviewer=reviewer))
            self.assertFalse(form.is_valid(), reviewer)
            self.assertIn("reviewer", form.errors)
        for token in ["token\n", "first\nsecond", "a" * 4097]:
            form = RepositorySettingsForm(self.data(github_token=token))
            self.assertFalse(form.is_valid())
            self.assertIn("github_token", form.errors)
        form = RepositorySettingsForm(self.data(full_name="ORG/REPO"))
        self.assertFalse(form.is_valid())
        self.assertIn("full_name", form.errors)

    def test_owner_post_only_and_csrf_guards(self):
        self.assertEqual(
            self.client.get(f"/settings/repositories/{self.repo.pk}/toggle/").status_code, 405
        )
        other = get_user_model().objects.create_user(username="other", is_superuser=True)
        self.client.force_login(other)
        self.assertEqual(self.client.get("/settings/repositories/add/").status_code, 403)
        self.assertEqual(
            self.client.post(f"/settings/repositories/{self.repo.pk}/toggle/").status_code, 403
        )
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(
            client.post(f"/settings/repositories/{self.repo.pk}/toggle/").status_code, 403
        )

    def test_edit_rejects_stale_version_and_full_name_is_immutable(self):
        from core.repository_views import repository_revision

        version = repository_revision(self.repo)
        self.repo.reviewer = "changed"
        self.repo.save()
        response = self.client.post(
            f"/settings/repositories/{self.repo.pk}/", self.data(version=version)
        )
        self.assertEqual(response.status_code, 409)
        self.repo.refresh_from_db()
        self.assertEqual(self.repo.reviewer, "changed")
        with patch("core.repository_views.connect_repository", return_value=True):
            response = self.client.post(
                f"/settings/repositories/{self.repo.pk}/",
                self.data(version=repository_revision(self.repo)),
            )
        self.assertEqual(response.status_code, 302)
        self.repo.refresh_from_db()
        self.assertEqual(self.repo.full_name, "org/repo")

    def test_connect_reuses_only_matching_webhook_and_creates_only_missing_labels(self):
        from core.repository_views import connect_repository, webhook_url

        self.repo.webhook_credential = Credential.objects.create(
            name="hook", encrypted_value=encrypt("hook-secret")
        )
        self.repo.save()
        with patch("core.repository_views.GitHub") as cls:
            github = cls.return_value.__enter__.return_value
            github.get.return_value = {"full_name": "org/repo", "permissions": {"admin": True}}
            github.pages.side_effect = [
                [{"name": "on review"}],
                [
                    {"id": 99, "config": {"url": "https://other/hook"}},
                    {"id": 42, "config": {"url": webhook_url(self.repo)}},
                ],
            ]
            github.patch.return_value = {"id": 42}
            self.assertTrue(connect_repository(self.repo.pk))
            self.assertEqual(
                [c.args[1]["name"] for c in github.post.call_args_list], ["feedback", "approved"]
            )
            self.assertEqual(github.patch.call_args.args[0], "/hooks/42")
        self.repo.refresh_from_db()
        self.assertEqual(self.repo.webhook_id, "42")

    @override_settings(GRAPH_REPOSITORIES=["org/repo"])
    def test_graph_override_and_legacy_fallback(self):
        from core.graph import enabled
        from core.graph_status import status

        self.assertTrue(enabled(self.repo))
        self.repo.graph_enabled = False
        self.assertFalse(enabled(self.repo))
        self.assertEqual(status(self.repo)["state"], "disabled")
        self.repo.full_name = "new/repo"
        self.repo.graph_enabled = True
        self.assertTrue(enabled(self.repo))

    def test_token_replacement_uses_new_encrypted_credential_without_altering_shared_token(self):
        from core.repository_views import repository_revision

        original = self.repo.github_credential_id
        with patch("core.repository_views.connect_repository", return_value=True):
            response = self.client.post(
                f"/settings/repositories/{self.repo.pk}/",
                self.data(
                    version=repository_revision(self.repo), github_token="replacement-secret"
                ),
            )
        self.assertEqual(response.status_code, 302)
        self.repo.refresh_from_db()
        self.assertNotEqual(self.repo.github_credential_id, original)
        self.assertEqual(decrypt(self.repo.github_credential.encrypted_value), "replacement-secret")
        self.assertEqual(
            decrypt(Credential.objects.get(pk=original).encrypted_value), "stored-token"
        )

    def test_connection_failure_has_static_feedback_and_never_leaks_upstream_error(self):
        from core.github import IntegrationError

        with patch(
            "core.repository_views.GitHub",
            side_effect=IntegrationError("stored-token private upstream"),
        ):
            response = self.client.post(
                f"/settings/repositories/{self.repo.pk}/connect/", follow=True
            )
        self.assertContains(response, "Check token repository/admin access")
        self.assertNotContains(response, "stored-token")
        self.assertNotContains(response, "private upstream")

    def test_pause_resume_and_connection_lock_prevent_concurrent_mutations(self):
        from core.repository_views import repository_lock

        url = f"/settings/repositories/{self.repo.pk}/toggle/"
        with repository_lock(self.repo.pk):
            response = self.client.post(url, {"enabled": "0"})
        self.assertEqual(response.status_code, 302)
        self.repo.refresh_from_db()
        self.assertTrue(self.repo.enabled)
        self.client.post(url, {"enabled": "0"})
        self.repo.refresh_from_db()
        self.assertFalse(self.repo.enabled)
        self.client.post(url, {"enabled": "1"})
        self.repo.refresh_from_db()
        self.assertTrue(self.repo.enabled)

    def test_github_repository_root_uses_canonical_metadata_endpoint(self):
        from core.github import GitHub

        with GitHub(self.repo) as github:
            self.assertEqual(github._url("/"), "https://api.github.com/repos/org/repo")

    def test_stale_repository_resubmission_does_not_silently_accept_fresh_version(self):
        from core.repository_views import repository_revision

        old_version = repository_revision(self.repo)
        self.repo.reviewer = "newowner"
        self.repo.save()
        response = self.client.post(
            f"/settings/repositories/{self.repo.pk}/", self.data(version=old_version)
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.context["version"], old_version)
        retry = self.client.post(
            f"/settings/repositories/{self.repo.pk}/",
            self.data(version=response.context["version"]),
        )
        self.assertEqual(retry.status_code, 409)
        self.repo.refresh_from_db()
        self.assertEqual(self.repo.reviewer, "newowner")

    def test_stale_service_settings_resubmission_does_not_overwrite_concurrent_policy(self):
        old_version = self.client.get("/settings/").context["version"]
        self.repo.auto_approve = True
        self.repo.save()
        response = self.client.post("/settings/", {"version": old_version})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.context["version"], old_version)
        retry = self.client.post("/settings/", {"version": response.context["version"]})
        self.assertEqual(retry.status_code, 409)
        self.repo.refresh_from_db()
        self.assertTrue(self.repo.auto_approve)

    @override_settings(PUBLIC_URL="http://localhost:8040", WEBHOOK_PUBLIC_URL="")
    def test_localhost_repository_is_saved_without_attempting_remote_webhook(self):
        with patch("core.repository_views.GitHub") as github:
            response = self.client.post("/settings/repositories/add/", self.data(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Repository.objects.filter(full_name="org/new").exists())
        github.assert_not_called()
        self.assertContains(response, "public HTTPS")

    @override_settings(
        PUBLIC_URL="http://localhost:8040", WEBHOOK_PUBLIC_URL="https://hooks.example.test"
    )
    def test_public_forwarding_url_is_separate_from_dashboard_url(self):
        from core.repository_views import webhook_url

        self.assertEqual(
            webhook_url(self.repo), f"https://hooks.example.test/webhooks/github/{self.repo.pk}/"
        )

    def test_saved_connection_choices_exclude_unrelated_provider_secrets(self):
        from core.forms import RepositorySettingsForm

        unrelated = Credential.objects.create(
            name="API secret", encrypted_value=encrypt("provider-secret")
        )
        form = RepositorySettingsForm()
        self.assertNotIn(unrelated, form.fields["github_credential"].queryset)
        self.assertIn(self.repo.github_credential, form.fields["github_credential"].queryset)

    def test_graph_override_is_captured_in_new_job_steps(self):
        from core.models import Job, PullRequest

        self.repo.graph_enabled = True
        self.repo.save()
        pr = PullRequest.objects.create(repository=self.repo, number=2, head_sha="a" * 40)
        job = Job.objects.create(pr=pr, generation=1, head_sha=pr.head_sha, actor="dev")
        self.assertIn("graph", job.workflow_steps)

    def test_repeated_pause_does_not_resume_monitoring(self):
        url = f"/settings/repositories/{self.repo.pk}/toggle/"
        self.client.post(url, {"enabled": "0"})
        self.client.post(url, {"enabled": "0"})
        self.repo.refresh_from_db()
        self.assertFalse(self.repo.enabled)

    def test_toggle_rejects_missing_desired_state(self):
        self.assertEqual(
            self.client.post(f"/settings/repositories/{self.repo.pk}/toggle/").status_code, 400
        )

    def test_plaintext_token_is_redacted_from_exception_reports(self):
        from django.test import RequestFactory
        from django.views.debug import SafeExceptionReporterFilter

        from core.repository_views import repository_edit

        request = RequestFactory().post(
            "/settings/repositories/add/", self.data(github_token="private-token")
        )
        request.user = self.owner
        with patch(
            "core.repository_views.RepositorySettingsForm", side_effect=RuntimeError("test failure")
        ):
            with self.assertRaises(RuntimeError):
                repository_edit(request)
        with override_settings(DEBUG=False):
            self.assertNotIn(
                "private-token", str(SafeExceptionReporterFilter().get_post_parameters(request))
            )

    def test_new_repository_does_not_inherit_optional_graph_enablement(self):
        self.repo.graph_enabled = True
        self.repo.save()
        response = self.client.get("/settings/repositories/add/")
        self.assertFalse(response.context["form"]["graph_enabled"].value())

    def test_invalid_webhook_origin_is_reported_without_remote_calls(self):
        from core.repository_views import connect_repository

        for origin in [
            "https://[broken",
            "https://example.com/path",
            "https://example.com?secret=x",
            "https://example.com:99999",
        ]:
            with (
                override_settings(WEBHOOK_PUBLIC_URL=origin),
                patch("core.repository_views.GitHub") as github,
            ):
                self.assertFalse(connect_repository(self.repo.pk))
                github.assert_not_called()

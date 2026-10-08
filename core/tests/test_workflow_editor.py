import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core.models import Provider


@override_settings(SECURE_SSL_REDIRECT=False, DASHBOARD_RETRY_USER_ID=1)
class WorkflowEditorTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(pk=1, username="owner", is_superuser=True)
        self.member = get_user_model().objects.create_user(username="member", is_superuser=True)
        self.first = Provider.objects.create(name="Sol", kind="codex_cli", model="sol", order=0)
        self.second = Provider.objects.create(name="Opus", kind="claude_cli", model="opus", order=1)
        self.client.force_login(self.owner)

    def state(self):
        return self.client.get("/workflow/state/").json()

    def save(self, state):
        return self.client.post(
            "/workflow/state/", json.dumps(state), content_type="application/json"
        )

    def test_only_configured_owner_can_read_and_write(self):
        self.assertEqual(self.client.get("/workflow/").status_code, 200)
        self.client.force_login(self.member)
        for url in ["/workflow/", "/workflow/state/"]:
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(
                self.client.post(url, "{}", content_type="application/json").status_code, 403
            )
        self.assertNotContains(self.client.get("/"), 'href="/workflow/"')

    def test_order_and_model_edits_are_atomic_and_conflicts_rejected(self):
        state = self.state()
        stale = self.state()
        state["models"].reverse()
        state["models"][0]["model"] = "new-opus"
        response = self.save(state)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(Provider.objects.values_list("model", flat=True)), ["new-opus", "sol"]
        )
        self.assertEqual(self.save(stale).status_code, 409)

    def test_reject_invalid_model_and_empty_chain_without_partial_save(self):
        state = self.state()
        state["models"][0]["name"] = "Changed"
        state["models"][1]["model"] = "--unsafe"
        self.assertEqual(self.save(state).status_code, 400)
        self.first.refresh_from_db()
        self.assertEqual(self.first.name, "Sol")
        state = self.state()
        for model in state["models"]:
            model["enabled"] = False
        self.assertEqual(self.save(state).status_code, 400)

    def test_add_custom_model_and_disable_without_deleting_history(self):
        state = self.state()
        state["models"][0]["enabled"] = False
        state["models"].append(
            {
                "id": None,
                "name": "Future",
                "kind": "codex_cli",
                "model": "future-model",
                "enabled": True,
                "effort": "",
            }
        )
        self.assertEqual(self.save(state).status_code, 200)
        self.assertTrue(Provider.objects.filter(pk=self.first.pk, enabled=False).exists())
        self.assertTrue(Provider.objects.filter(model="future-model", enabled=True).exists())


class SnapshotTests(TestCase):
    def test_new_jobs_freeze_models_and_credentials_at_creation(self):
        from core.models import Credential, Job, Organization, PullRequest, Repository
        from core.review_config import providers_for_job

        org = Organization.objects.create(name="Org", slug="org")
        credential = Credential.objects.create(name="github", encrypted_value="secret")
        repo = Repository.objects.create(
            organization=org,
            full_name="org/repo",
            reviewer="owner",
            github_credential=credential,
            webhook_credential=credential,
        )
        pr = PullRequest.objects.create(repository=repo, number=1, head_sha="a")
        provider = Provider.objects.create(name="Original", model="old-model", kind="codex_cli")
        job = Job.objects.create(pr=pr, generation=1, head_sha="a", actor="dev")
        provider.model = "new-model"
        provider.enabled = False
        provider.save()
        job.refresh_from_db()
        plan = providers_for_job(job)
        self.assertEqual([p.model for p in plan], ["old-model"])
        self.assertEqual(plan[0].name, "Original")
        self.assertTrue(job.workflow_version)
        newer = Job.objects.create(pr=pr, generation=2, head_sha="a", actor="dev")
        self.assertEqual(providers_for_job(newer), [])
        self.assertNotEqual(newer.workflow_version, job.workflow_version)


@override_settings(SECURE_SSL_REDIRECT=False, DASHBOARD_RETRY_USER_ID=1)
class ProbeTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(pk=1, username="owner", is_superuser=True)
        self.provider = Provider.objects.create(name="Sol", model="sol", kind="codex_cli")
        self.client.force_login(self.owner)

    def test_probe_queued_once_uses_saved_configuration_and_requires_owner(self):
        from core.models import ModelProbe

        url = f"/workflow/probe/{self.provider.pk}/"
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url).status_code, 202)
        self.assertEqual(self.client.post(url).status_code, 409)
        self.assertEqual(ModelProbe.objects.get().configuration["model"], "sol")
        self.owner.is_superuser = False
        self.owner.save()
        self.assertEqual(self.client.post(url).status_code, 403)

    def test_probe_result_and_errors_never_publish_or_expose_provider_details(self):
        from unittest.mock import patch

        from core.model_probe import claim_probe, run_probe
        from core.models import ModelProbe
        from core.providers import ProviderError

        self.client.post(f"/workflow/probe/{self.provider.pk}/")
        probe = claim_probe()
        with patch("core.cli.review_cli", side_effect=ProviderError("auth", detail="private")):
            run_probe(probe)
        probe = ModelProbe.objects.get()
        self.assertEqual(probe.status, "failed")
        self.assertNotIn("private", probe.reason)
        self.assertIsNotNone(probe.finished_at)

    def test_csrf_required_for_save_and_probe(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(
            client.post("/workflow/state/", "{}", content_type="application/json").status_code, 403
        )
        self.assertEqual(client.post(f"/workflow/probe/{self.provider.pk}/").status_code, 403)


class LegacySnapshotTests(TestCase):
    def test_migration_preserves_recorded_model_and_recovers_disabled_api_credential(self):
        import importlib

        from django.apps import apps

        from core.models import Credential, Job, Organization, PullRequest, Repository
        from core.review_config import providers_for_job

        org = Organization.objects.create(name="Org", slug="org")
        credential = Credential.objects.create(name="key", encrypted_value="private")
        repo = Repository.objects.create(
            organization=org,
            full_name="org/repo",
            reviewer="owner",
            github_credential=credential,
            webhook_credential=credential,
        )
        pr = PullRequest.objects.create(repository=repo, number=1, head_sha="a")
        provider = Provider.objects.create(
            name="Renamed", model="new", kind="openai", credential=credential, enabled=False
        )
        job = Job.objects.create(pr=pr, generation=1, head_sha="a", actor="dev")
        Job.objects.filter(pk=job.pk).update(
            workflow_version="",
            provider_plan=[
                {"id": provider.pk, "name": "Original", "model": "old", "kind": "openai"}
            ],
        )
        migration = importlib.import_module(
            "core.migrations.0012_job_workflow_version_provider_effort_modelprobe"
        )
        migration.freeze_existing_jobs(apps, None)
        job.refresh_from_db()
        saved = providers_for_job(job)[0]
        self.assertEqual(saved.model, "old")
        self.assertEqual(saved.credential_id, credential.pk)
        self.assertNotIn("private", json.dumps(job.provider_plan))

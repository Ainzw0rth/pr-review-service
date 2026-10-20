from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core import quota
from core.models import Provider


class QuotaTests(TestCase):
    def test_drain_prevents_claims_and_survives_worker_start_until_resumed(self):
        with (
            TemporaryDirectory() as directory,
            self.settings(DATA_DIR=Path(directory), CLI_RUNNER_IMAGE=""),
        ):
            call_command("worker_control", "drain", stdout=StringIO())
            with patch("core.management.commands.review_worker.claim") as claim:
                call_command("review_worker", once=True, stdout=StringIO())
                claim.assert_not_called()
            self.assertTrue((Path(directory) / "worker.drain").exists())
            call_command("worker_control", "resume", stdout=StringIO())
            self.assertFalse((Path(directory) / "worker.drain").exists())

    def test_exhausted_account_skips_both_opus_models_until_reset(self):
        reset = timezone.now() + timedelta(hours=1)
        quota.store_snapshot(
            "claude_cli",
            {
                "windows": [
                    {"name": "five_hour", "used_percent": 100, "resets_at": reset.timestamp()}
                ]
            },
        )
        for model in ("opus-new", "opus-old"):
            p = Provider(kind="claude_cli", model=model)
            self.assertEqual(quota.blocked_until(p), reset.replace(microsecond=0))
        self.assertIsNone(quota.blocked_until(Provider(kind="codex_cli")))

    def test_malformed_snapshot_never_claims_remaining_quota(self):
        quota.store_snapshot(
            "codex_cli",
            {"windows": [{"name": "primary", "used_percent": "secret", "resets_at": -1}]},
        )
        card = next(c for c in quota.cards() if c["kind"] == "codex_cli")
        self.assertEqual(card["state"], "unknown")
        self.assertEqual(card["windows"], [])

    def test_transient_probe_failure_preserves_explicit_cooldown(self):
        p = Provider(kind="claude_cli")
        until = quota.record_exhausted(p)
        quota.store_snapshot("claude_cli", {"error": "unavailable"})
        self.assertEqual(quota.blocked_until(p), until)

    def test_remaining_percent_is_reported_not_token_estimated(self):
        quota.store_snapshot(
            "codex_cli",
            {
                "windows": [
                    {
                        "name": "primary",
                        "used_percent": 27,
                        "resets_at": (timezone.now() + timedelta(hours=2)).timestamp(),
                    }
                ]
            },
        )
        card = next(c for c in quota.cards() if c["kind"] == "codex_cli")
        self.assertEqual(card["windows"][0]["remaining"], 73)
        self.assertEqual(card["state"], "available")

    def test_busy_auth_volume_skips_refresh_without_changing_observation(self):
        from core.capacity import cli_scope, permit
        from core.models import SubscriptionQuota

        with (
            TemporaryDirectory() as directory,
            self.settings(
                DATA_DIR=Path(directory),
                CLI_RUNNER_IMAGE="sha256:" + "a" * 64,
                CLI_CODEX_AUTH_VOLUME="shared-test-auth",
            ),
        ):
            Provider.objects.create(name="test-cli", kind="codex_cli", model="test")
            quota.store_snapshot("codex_cli", {"error": "unsupported"})
            row = SubscriptionQuota.objects.get(kind="codex_cli")
            row.checked_at = timezone.now() - timedelta(hours=1)
            row.save()
            old_time = row.checked_at
            with (
                permit(cli_scope("codex_cli")[1], 1, lambda: False),
                patch("core.runner._docker") as docker,
            ):
                quota.refresh_due()
            row.refresh_from_db()
            self.assertEqual(row.checked_at, old_time)
            self.assertEqual(row.error, "unsupported")
            docker.assert_not_called()

    def test_quota_inspects_auth_volume_and_removes_immutable_container(self):
        import json
        from types import SimpleNamespace

        from core.tests.docker_fake import DockerFake

        docker = DockerFake()
        with (
            TemporaryDirectory() as directory,
            self.settings(
                DATA_DIR=Path(directory),
                CLI_RUNNER_IMAGE="sha256:" + "a" * 64,
                CLI_CODEX_AUTH_VOLUME="shared-test-auth",
            ),
        ):
            Provider.objects.create(name="test-cli", kind="codex_cli", model="test")
            with (
                patch("core.runner._docker", side_effect=docker),
                patch(
                    "core.quota.subprocess.run",
                    return_value=SimpleNamespace(
                        returncode=0, stdout=json.dumps({"error": "unsupported"})
                    ),
                ) as run,
            ):
                quota.refresh_due()
            self.assertFalse(docker.objects)
            self.assertIn(["volume", "inspect", "shared-test-auth"], docker.commands)
            self.assertRegex(run.call_args.args[0][-1], "^[a-f0-9]{64}$")

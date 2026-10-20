import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.capacity import permit, set_cli_limit
from core.cli import review_cli
from core.models import Provider
from core.quota import refresh_due
from core.tests.docker_fake import DockerFake


class CLIAdmissionIntegrationTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        options = override_settings(
            DATA_DIR=Path(directory.name),
            CLI_RUNNER_IMAGE="sha256:" + "a" * 64,
            CLI_CODEX_AUTH_VOLUME="admission-auth",
        )
        options.enable()
        self.addCleanup(options.disable)
        previous = set_cli_limit(6)
        self.addCleanup(set_cli_limit, previous)
        self.priorities = []

    @contextmanager
    def admitted(self, scope, cancelled, *, priority=1, on_wait=None, wait=True):
        self.priorities.append(priority)
        with permit(scope, 6, cancelled, on_wait=on_wait, wait=wait) as slot:
            yield slot

    def test_adjudication_uses_priority_zero_and_other_reviews_normal_priority(self):
        with (
            patch("core.cli.admit_cli", self.admitted, create=True),
            patch(
                "core.runner._docker",
                side_effect=DockerFake(image_labels={"auto-reviewer.auth-coordination": "v1"}),
            ),
            patch("core.cli._review_cli_active", return_value={"complete": True}),
        ):
            for task in ("review_synthesis", "review_security", "intent"):
                review_cli(SimpleNamespace(kind="codex_cli"), {}, lambda: False, task=task)
        self.assertEqual(self.priorities, [0, 1, 1])

    def test_quota_refresh_uses_low_priority_same_account_pool(self):
        Provider.objects.create(name="cli", kind="codex_cli", model="configured")
        with (
            patch("core.quota.admit_cli", self.admitted, create=True),
            patch(
                "core.runner._docker",
                side_effect=DockerFake(image_labels={"auto-reviewer.auth-coordination": "v1"}),
            ),
            patch(
                "core.quota.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps({"error": "unsupported"})
                ),
            ),
        ):
            refresh_due()
        self.assertEqual(self.priorities, [2])

    def test_supervisor_loads_saved_cli_capacity_before_admission_and_restores_snapshot(self):
        from io import StringIO

        from django.core.management import call_command

        from core.capacity import cli_limit
        from core.models import ServiceSettings

        ServiceSettings.objects.filter(pk=1).update(cli_account_concurrency=6)
        set_cli_limit(3)
        observed = []
        with patch("core.quota.refresh_due", side_effect=lambda: observed.append(cli_limit())):
            call_command("review_worker", allow_sqlite=True, once=True, stdout=StringIO())
        self.assertEqual(observed, [6])
        self.assertEqual(cli_limit(), 3)

    @contextmanager
    def shrinking(self, scope, cancelled, *, priority=1, on_wait=None, wait=True):
        with permit(scope, 6, cancelled, on_wait=on_wait, wait=wait) as slot:

            class Admitted:
                admission_limit = 6

                def __getattr__(self, name):
                    return getattr(slot, name)

            set_cli_limit(1)
            yield Admitted()

    def test_shrink_after_admission_cannot_skip_auth_coordination_gate(self):
        from core.providers import ProviderError

        with (
            patch("core.cli.admit_cli", self.shrinking),
            patch("core.runner._docker", side_effect=DockerFake()),
            patch("core.cli._review_cli_active") as active,
            self.assertRaises(ProviderError) as caught,
        ):
            review_cli(SimpleNamespace(kind="codex_cli"), {}, lambda: False)
        self.assertEqual(caught.exception.detail, "cli_setup")
        active.assert_not_called()

    def test_quota_shrink_after_admission_still_checks_auth_image(self):
        Provider.objects.create(name="cli", kind="codex_cli", model="configured")
        with (
            patch("core.quota.admit_cli", self.shrinking),
            patch("core.cli.require_auth_coordination") as check_image,
            patch("core.runner._docker", side_effect=DockerFake()),
            patch(
                "core.quota.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout='{"error":"unsupported"}'),
            ),
        ):
            refresh_due()
        check_image.assert_called_once()

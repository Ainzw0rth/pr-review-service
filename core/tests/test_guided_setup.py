import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from core.models import Credential, Provider
from core.secrets import decrypt


@override_settings(LIVE_WRITES=False, CREDENTIAL_KEY=Fernet.generate_key().decode())
class SetupProviderTests(TestCase):
    def configure(self, **data):
        out = StringIO()
        with patch("sys.stdin", StringIO(json.dumps(data))):
            call_command("setup_provider", stdout=out)
        return out.getvalue()

    def test_cli_and_repeat_do_not_duplicate(self):
        data = dict(kind="codex_cli", model="test-model")
        self.configure(**data)
        self.configure(**data)
        self.assertEqual(Provider.objects.count(), 1)
        self.assertIsNone(Provider.objects.get().credential_id)

    def test_api_key_encrypted_and_not_printed(self):
        output = self.configure(kind="openai", model="test-model", key="private-api-key")
        credential = Credential.objects.get()
        self.assertEqual(decrypt(credential.encrypted_value), "private-api-key")
        self.assertNotIn("private-api-key", output + credential.encrypted_value)

    def test_repository_setup_already_started_blocks_runner_changes(self):
        with patch(
            "core.management.commands.setup_provider.Repository.objects.exists", return_value=True
        ):
            with self.assertRaisesMessage(CommandError, "Repositories already exist"):
                call_command("setup_provider", check=True, stdout=StringIO())
            with self.assertRaises(CommandError):
                self.configure(kind="codex_cli", model="test-model")

    def test_invalid_input_leaves_no_credentials(self):
        for data in [
            dict(kind="openai", model="m"),
            dict(kind="unknown", model="m", key="secret"),
            dict(kind="codex_cli", model=""),
            dict(kind="codex_cli", model="m", key="secret"),
        ]:
            with self.assertRaises(CommandError):
                self.configure(**data)
        self.assertFalse(Credential.objects.exists())
        self.assertFalse(Provider.objects.exists())

    def test_refuses_existing_routes_and_live_changes(self):
        self.configure(kind="codex_cli", model="first")
        with self.assertRaises(CommandError):
            self.configure(kind="claude_cli", model="second")
        with override_settings(LIVE_WRITES=True), self.assertRaises(CommandError):
            self.configure(kind="codex_cli", model="first")
        self.assertEqual(Provider.objects.get().model, "first")


class GuidedSetupTests(SimpleTestCase):
    def test_env_update_preserves_secrets_and_rejects_injection(self):
        from scripts.setup import update_env

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# comment\nSECRET=keep-me\nCLI_RUNNER_IMAGE=\n")
            update_env(path, {"CLI_RUNNER_IMAGE": "sha256:abc"})
            self.assertIn("SECRET=keep-me", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                update_env(path, {"CLI_RUNNER_IMAGE": "x\nLIVE_WRITES=1"})
            self.assertEqual(before, path.read_bytes())

    def test_resume_rejects_changed_environment_and_completed_setup(self):
        from scripts.setup import load_state, save_state

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "var").mkdir()
            env = root / ".env"
            env.write_text("LIVE_WRITES=0\n")
            state = {"step": "start"}
            save_state(root, state)
            self.assertEqual(load_state(root)["step"], "start")
            env.write_text("LIVE_WRITES=1\n")
            with self.assertRaises(SystemExit):
                load_state(root)
            save_state(root, {"step": "done"})
            with self.assertRaises(SystemExit):
                load_state(root)

    def test_existing_env_is_refused_before_any_docker_command(self):
        from scripts import setup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("production-secret")
            with (
                patch.object(setup, "ROOT", root),
                patch("sys.argv", ["setup.py"]),
                patch("sys.stdin.isatty", return_value=True),
                patch.object(setup, "run") as run,
            ):
                with self.assertRaises(SystemExit):
                    setup.main()
                run.assert_not_called()
            self.assertEqual((root / ".env").read_text(), "production-secret")

    def test_cli_walkthrough_and_interrupted_login_resume(self):
        import subprocess

        from scripts import setup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            fail_login = True

            def fake_run(args, **kwargs):
                nonlocal fail_login
                calls.append((args, kwargs))
                output = ""
                if "scripts/init_local.py" in args:
                    (root / ".env").write_text(
                        "SECRET=preserve\nLIVE_WRITES=0\nCLI_RUNNER_IMAGE=\nDASHBOARD_RETRY_USER_ID=0\n"
                    )
                if "bootstrap_admin" in args:
                    output = json.dumps(
                        dict(owner_id=37, setup_url="http://localhost/private-link")
                    )
                if "inspect" in args:
                    output = "sha256:" + "a" * 64
                if "scripts/cli_login.sh" in args and fail_login:
                    fail_login = False
                    raise subprocess.CalledProcessError(1, args)
                return subprocess.CompletedProcess(args, 0, stdout=output)

            with (
                patch.object(setup, "ROOT", root),
                patch.object(setup, "run", side_effect=fake_run),
                patch("sys.stdin.isatty", return_value=True),
                patch("sys.stdout", StringIO()),
            ):
                with (
                    patch("sys.argv", ["setup.py"]),
                    patch(
                        "builtins.input",
                        side_effect=["1", "", "1", "test-model", "admin", "My workspace"],
                    ),
                ):
                    with self.assertRaises(subprocess.CalledProcessError):
                        setup.main()
                self.assertEqual(setup.load_state(root)["step"], "model")
                with (
                    patch("sys.argv", ["setup.py", "--resume"]),
                    patch("builtins.input", side_effect=["1"]),
                ):
                    setup.main()
            self.assertEqual(sum("bootstrap_admin" in args for args, _ in calls), 1)
            self.assertEqual(sum("scripts/init_local.py" in args for args, _ in calls), 1)
            self.assertIn("DASHBOARD_RETRY_USER_ID=37", (root / ".env").read_text())
            self.assertIn("SECRET=preserve", (root / ".env").read_text())
            self.assertIn("LIVE_WRITES=0", (root / ".env").read_text())
            self.assertTrue(any("probe_provider" in args for args, _ in calls))
            self.assertEqual(
                json.loads((root / "var/setup-state.json").read_text())["step"], "done"
            )


class BootstrapJsonTests(TestCase):
    def test_json_output(self):
        out = StringIO()
        call_command(
            "bootstrap_admin",
            setup_link=True,
            json=True,
            username="guided",
            stdout=out,
            stderr=StringIO(),
        )
        data = json.loads(out.getvalue())
        from django.contrib.auth import get_user_model

        self.assertEqual(data["owner_id"], get_user_model().objects.get(username="guided").pk)
        self.assertIn("/access/", data["setup_url"])


@override_settings(LIVE_WRITES=False, CREDENTIAL_KEY=Fernet.generate_key().decode())
class ProbeProviderTests(TestCase):
    def test_probe_requires_one_provider_and_does_not_publish(self):
        with self.assertRaises(CommandError):
            call_command("probe_provider")
        Provider.objects.create(name="CLI", kind="codex_cli", model="test-model")
        with patch("core.management.commands.probe_provider.call_command") as probe:
            call_command("probe_provider", stdout=StringIO())
            self.assertEqual(probe.call_args.args, ("probe_cli", "codex_cli", "test-model"))

    def test_api_failure_is_clear_without_secret_output(self):
        from core.providers import ProviderError

        Provider.objects.create(name="API", kind="openai", model="test-model")
        with patch(
            "core.management.commands.probe_provider.review", side_effect=ProviderError("auth")
        ):
            with self.assertRaisesMessage(CommandError, "Model probe failed"):
                call_command("probe_provider", stdout=StringIO())

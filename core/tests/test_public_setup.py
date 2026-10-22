"""First-run behavior without touching the checkout's deployment environment."""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from core.models import Membership

ROOT = Path(__file__).resolve().parents[2]


class InitializerTests(SimpleTestCase):
    def test_stdlib_secrets_postgres_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copy(ROOT / "scripts/init_local.py", root / "scripts/init_local.py")
            shutil.copy(ROOT / ".env.example", root / ".env.example")
            command = [sys.executable, "-S", str(root / "scripts/init_local.py")]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            original = (root / ".env").read_bytes()
            if os.name == "posix":
                self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
            env = dict(
                line.split("=", 1)
                for line in original.decode().splitlines()
                if line and not line.startswith("#")
            )
            self.assertEqual(env["DB_ENGINE"], "postgresql")
            self.assertEqual(env["TIME_ZONE"], "UTC")
            self.assertEqual(env["LOCAL_HTTP"], "1")
            self.assertEqual(len(base64.urlsafe_b64decode(env["CREDENTIAL_KEY"])), 32)
            self.assertGreater(len(env["DB_PASSWORD"]), 40)
            self.assertNotIn(env["DB_PASSWORD"], result.stdout)
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / ".env").read_bytes(), original)
            (root / ".env").unlink()
            result = subprocess.run(
                command + ["--mode", "server", "--public-url", "http://example.com"],
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / ".env").exists())
            result = subprocess.run(
                command
                + [
                    "--mode",
                    "server",
                    "--public-url",
                    "https://reviews.example.com",
                    "--webhook-url",
                    "https://hooks.example.com",
                ],
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            content = (root / ".env").read_text()
            self.assertIn("LOCAL_HTTP=0", content)
            self.assertIn("TRUST_PROXY=1", content)
            self.assertIn("WEBHOOK_PUBLIC_URL=https://hooks.example.com", content)

    def test_http_requires_explicit_loopback_and_server_defaults_remain_secure(self):
        code = "import json; from reviewdesk import settings as s; print(json.dumps([s.DEBUG,s.SECURE_SSL_REDIRECT,s.SESSION_COOKIE_SECURE,s.CSRF_COOKIE_SECURE,s.SECURE_HSTS_SECONDS]))"
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "DJANGO_SECRET_KEY": "test",
                "DATA_DIR": directory,
            }
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
            )
            self.assertEqual(json.loads(result.stdout), [False, True, True, True, 31536000])
            env.update(LOCAL_HTTP="1", ALLOWED_HOSTS="localhost,127.0.0.1,[::1]")
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
            )
            self.assertEqual(json.loads(result.stdout), [False, False, False, False, 0])
            for unsafe in (
                {"ALLOWED_HOSTS": "*"},
                {"PUBLIC_URL": "http://example.com"},
                {"TRUST_PROXY": "1"},
            ):
                result = subprocess.run(
                    [sys.executable, "-c", code],
                    cwd=ROOT,
                    env={**env, **unsafe},
                    capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_deployment_checks_allow_only_intentional_local_http_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "DJANGO_SECRET_KEY": "strong-test-deployment-key-with-over-fifty-characters-unique-7319",
                "DATA_DIR": directory,
                "ALLOWED_HOSTS": "localhost,127.0.0.1",
            }
            command = [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"]
            for local in ("0", "1"):
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env={**env, "LOCAL_HTTP": local},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            # Other warnings must still fail even in intentional loopback mode.
            result = subprocess.run(
                command,
                cwd=ROOT,
                env={**env, "LOCAL_HTTP": "1", "DJANGO_SECRET_KEY": "weak"},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("security.W009", result.stderr)
            # HTTPS mode retains the HTTP-related checks.
            code = "import django; django.setup(); from django.conf import settings; from django.core.management import call_command; settings.SECURE_SSL_REDIRECT=False; call_command('check', deploy=True, fail_level='WARNING')"
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=ROOT,
                env={**env, "DJANGO_SETTINGS_MODULE": "reviewdesk.settings"},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("security.W008", result.stderr)


class BootstrapTests(TestCase):
    def test_custom_identity_membership_and_actual_owner_binding(self):
        get_user_model().objects.create_user(username="existing")
        out, err = StringIO(), StringIO()
        call_command(
            "bootstrap_admin",
            setup_link=True,
            username="owner",
            email="owner@example.com",
            organization="My Team",
            stdout=out,
            stderr=err,
        )
        owner = get_user_model().objects.get(username="owner")
        self.assertFalse(owner.has_usable_password())
        self.assertTrue(
            Membership.objects.filter(user=owner, organization__slug="my-team").exists()
        )
        self.assertIn(f"DASHBOARD_RETRY_USER_ID={owner.pk}", err.getvalue())
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        call_command(
            "bootstrap_admin",
            setup_link=True,
            username="owner",
            organization="My Team",
            stdout=StringIO(),
            stderr=StringIO(),
        )
        self.assertEqual(Membership.objects.filter(user=owner).count(), 1)

    def test_invalid_or_existing_interactive_username_is_rejected(self):
        for username in ("", "bad name"):
            with self.assertRaises(CommandError):
                call_command("bootstrap_admin", username=username, setup_link=True)
        get_user_model().objects.create_user(username="existing")
        with self.assertRaises(CommandError):
            call_command("bootstrap_admin", username="existing")

    def test_interactive_membership_failure_rolls_back_user(self):
        def create_user(*args, **kwargs):
            get_user_model().objects.create_superuser(username=kwargs["username"], password=None)

        with patch(
            "core.management.commands.bootstrap_admin.call_command", side_effect=create_user
        ):
            with patch(
                "core.management.commands.bootstrap_admin.Membership.objects.get_or_create",
                side_effect=RuntimeError("failed"),
            ):
                with self.assertRaises(RuntimeError):
                    call_command(
                        "bootstrap_admin", username="rollback", stdout=StringIO(), stderr=StringIO()
                    )
        self.assertFalse(get_user_model().objects.filter(username="rollback").exists())

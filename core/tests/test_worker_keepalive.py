import base64
import json
import subprocess
from datetime import timedelta
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from core import worker_keepalive as keepalive
from core.models import ExecutionWorker
from core.secrets import decrypt
from core.worker_keepalive_models import WorkerKeepalive

KEY = (
    "ssh-ed25519 "
    + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"\x00\x00\x00\x20" + b"a" * 32).decode()
)


@override_settings(CREDENTIAL_KEY=Fernet.generate_key().decode())
class WorkerKeepaliveTests(TestCase):
    def setUp(self):
        self.worker = ExecutionWorker.objects.create(name="Remote", credential_digest="a" * 64)
        self.data = dict(
            enabled=True, host="example.test", port=22, user="private-token", known_host_key=KEY
        )

    def configure(self):
        keepalive.configure(self.worker, self.data)
        return WorkerKeepalive.objects.get(worker=self.worker)

    def test_private_config_encrypted_and_never_returned(self):
        row = self.configure()
        self.assertNotIn("private-token", row.encrypted_config)
        self.assertEqual(json.loads(decrypt(row.encrypted_config))["user"], "private-token")
        public = json.dumps(keepalive.public_status(self.worker))
        for private in ("private-token", "example.test", KEY):
            self.assertNotIn(private, public)
        keepalive.configure(self.worker, dict(enabled=True, user="", known_host_key=""))
        row.refresh_from_db()
        self.assertEqual(json.loads(decrypt(row.encrypted_config))["user"], "private-token")
        self.assertEqual(row.interval_seconds, 1200)

    def test_rejects_option_injection_and_bad_types(self):
        for field, value in (
            ("host", "-oProxyCommand=evil"),
            ("host", "ok\nHost *"),
            ("host", "https://example.test"),
            ("user", "a\nProxyCommand evil"),
            ("port", True),
            ("enabled", "false"),
            ("interval_minutes", 0),
            ("known_host_key", KEY + "\n"),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                keepalive.configure(self.worker, {**self.data, field: value})

    @patch("core.worker_keepalive._ssh_check", return_value=("success", ""))
    def test_success_schedules_without_marking_worker_ready(self, ssh):
        self.configure()
        self.assertEqual(keepalive.run_due_checks(), 1)
        self.assertEqual(keepalive.run_due_checks(), 0)
        row = WorkerKeepalive.objects.get(worker=self.worker)
        self.worker.refresh_from_db()
        self.assertIsNone(self.worker.heartbeat_at)
        self.assertFalse(self.worker.authenticated)
        self.assertEqual(row.status, "success")
        self.assertIsNotNone(row.last_success_at)
        self.assertGreater(row.next_due_at, timezone.now() + timedelta(minutes=19))
        ssh.assert_called_once()

    @patch("core.worker_keepalive._ssh_check")
    def test_overlapping_manual_and_periodic_check_deduplicated(self, ssh):
        self.configure()

        def overlapping(config):
            keepalive.request_check(self.worker)
            self.assertEqual(keepalive.run_due_checks(), 0)
            return "success", ""

        ssh.side_effect = overlapping
        self.assertEqual(keepalive.run_due_checks(), 1)
        ssh.assert_called_once()

    @patch("core.worker_keepalive._ssh_check", return_value=("timeout", "SSH check timed out."))
    def test_overdue_and_expired_lease_recovered_without_retry_loop(self, ssh):
        row = self.configure()
        row.next_due_at = timezone.now() - timedelta(hours=1)
        row.lease_token = "old"
        row.lease_expires_at = timezone.now() - timedelta(seconds=1)
        row.save()
        self.assertEqual(keepalive.run_due_checks(), 1)
        self.assertEqual(keepalive.run_due_checks(), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, "timeout")
        self.assertIsNone(row.last_success_at)
        self.assertEqual(row.lease_token, "")

    @patch("core.worker_keepalive._ssh_check")
    def test_disabled_is_never_executed(self, ssh):
        self.configure()
        keepalive.configure(self.worker, {"enabled": False})
        self.assertEqual(keepalive.run_due_checks(), 0)
        with self.assertRaises(ValueError):
            keepalive.request_check(self.worker)
        ssh.assert_not_called()

    @patch("core.worker_keepalive.subprocess.run")
    def test_fixed_command_private_files_and_redacted_failure(self, run):
        def check(argv, **kwargs):
            from pathlib import Path

            self.assertEqual(argv[0], "ssh")
            self.assertEqual(argv[-2:], ["keepalive-target", "true"])
            self.assertNotIn("private-token", repr(argv))
            config = Path(argv[2])
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertIn("StrictHostKeyChecking yes", config.read_text())
            self.assertEqual(kwargs["timeout"], 30)
            return subprocess.CompletedProcess(
                argv, 255, stderr=b"private-token: Permission denied"
            )

        run.side_effect = check
        self.assertEqual(
            keepalive._ssh_check(self.data),
            ("authentication_failed", "SSH authentication failed; verify credentials."),
        )

    @patch(
        "core.worker_keepalive.subprocess.run",
        side_effect=subprocess.TimeoutExpired("private-token", 30),
    )
    def test_timeout_redacted(self, run):
        self.assertEqual(keepalive._ssh_check(self.data), ("timeout", "SSH check timed out."))


@override_settings(CREDENTIAL_KEY=Fernet.generate_key().decode())
class WorkerKeepaliveRaceTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    @patch("core.worker_keepalive._ssh_check", return_value=("success", ""))
    def test_two_schedulers_claim_one_check(self, ssh):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        from django.db import close_old_connections

        worker = ExecutionWorker.objects.create(name="Race", credential_digest="b" * 64)
        keepalive.configure(
            worker,
            dict(enabled=True, host="example.test", user="private-token", known_host_key=KEY),
        )
        barrier = Barrier(2)

        def run():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return keepalive.run_due_checks()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(sum(results), 1)
        ssh.assert_called_once()

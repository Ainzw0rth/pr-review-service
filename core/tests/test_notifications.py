import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Credential, Job, Organization, PullRequest, Repository, ReviewCheckpoint
from core.secrets import encrypt


class ApprovalNotificationTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings_override = override_settings(
            CREDENTIAL_KEY=Fernet.generate_key().decode(),
            TELEGRAM_APPROVAL_ENABLED=True,
            TELEGRAM_APPROVAL_SINCE=(timezone.now() - timedelta(minutes=1)).isoformat(),
            TELEGRAM_APPROVAL_REPOSITORIES=["org/repo"],
            DATA_DIR=Path(self.directory.name),
            PUBLIC_URL="https://reviews.example.test",
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.credential = Credential.objects.create(
            name="telegram",
            encrypted_value=encrypt(json.dumps({"token": "123:private-token", "chat_id": "456"})),
        )
        self.token_settings = override_settings(TELEGRAM_APPROVAL_CREDENTIAL_ID=self.credential.pk)
        self.token_settings.enable()
        self.addCleanup(self.token_settings.disable)
        org = Organization.objects.create(name="Org", slug="org")
        repo = Repository.objects.create(
            organization=org,
            full_name="org/repo",
            reviewer="me",
            github_credential=self.credential,
            webhook_credential=self.credential,
        )
        self.pr = PullRequest.objects.create(repository=repo, number=7, head_sha="a" * 40)
        self.job = Job.objects.create(
            pr=self.pr,
            generation=1,
            head_sha="a" * 40,
            actor="dev",
            status="completed",
            finished_at=timezone.now(),
        )
        self.checkpoint = ReviewCheckpoint.objects.create(
            job=self.job,
            stage="done",
            encrypted_payload=encrypt(
                json.dumps(
                    {
                        "outcome": "ready",
                        "head_sha": self.job.head_sha,
                        "result": {"complete": True, "blocking": False},
                    }
                )
            ),
        )

    def flush(self, handler):
        from core.notifications import flush_approvals

        factory = httpx.Client
        with patch(
            "core.notifications.httpx.Client",
            side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs),
        ):
            return flush_approvals()

    def test_only_successfully_published_approvals_send_once(self):
        requests = []

        def accept(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 9, "chat": {"id": 456}}}
            )

        self.flush(accept)
        self.flush(accept)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["chat_id"], "456")
        self.assertIn("https://github.com/org/repo/pull/7", requests[0]["text"])
        self.assertIn(str(self.job.pk), requests[0]["text"])
        self.assertNotIn("private-token", requests[0]["text"])
        self.assertNotIn("parse_mode", requests[0])
        self.assertEqual(self.job.approval_notification.status, "sent")

    def test_feedback_incomplete_publication_and_historical_jobs_do_not_send(self):
        def unexpected(request):
            self.fail("Non-approval must not send Telegram messages")

        self.checkpoint.encrypted_payload = encrypt(
            json.dumps({"outcome": "feedback", "head_sha": self.job.head_sha})
        )
        self.checkpoint.save()
        self.flush(unexpected)
        self.job.approval_notification.delete()
        self.checkpoint.stage = "labels"
        self.checkpoint.save()
        self.flush(unexpected)
        self.checkpoint.stage = "done"
        self.checkpoint.save()
        self.job.finished_at = timezone.now() - timedelta(days=1)
        self.job.save()
        self.flush(unexpected)

    def test_rate_limit_retries_but_ambiguous_timeout_does_not_duplicate(self):
        from core.models import ApprovalNotification

        self.flush(
            lambda request: httpx.Response(
                429, json={"ok": False, "parameters": {"retry_after": 120}}
            )
        )
        row = ApprovalNotification.objects.get(job=self.job)
        self.assertEqual(row.status, "pending")
        self.assertGreater(row.next_attempt_at, timezone.now())
        row.next_attempt_at = timezone.now() - timedelta(seconds=1)
        row.save()

        def timeout(request):
            raise httpx.ReadTimeout("secret request URL must never be logged")

        self.flush(timeout)
        row.refresh_from_db()
        self.assertEqual(row.status, "uncertain")
        self.assertNotIn("secret", row.reason)
        self.flush(lambda request: self.fail("Ambiguous send must not be retried"))

    def test_disabled_wrong_repo_and_no_config_send_nothing(self):
        with override_settings(TELEGRAM_APPROVAL_ENABLED=False):
            self.flush(lambda request: self.fail("Disabled"))
        with override_settings(TELEGRAM_APPROVAL_REPOSITORIES=["other/repo"]):
            self.flush(lambda request: self.fail("Wrong repository"))
        with override_settings(TELEGRAM_APPROVAL_SINCE=""):
            self.flush(lambda request: self.fail("Missing cutoff"))
        self.assertFalse(hasattr(self.job, "approval_notification"))

    def test_crash_during_send_is_not_replayed(self):
        from core.models import ApprovalNotification

        row = ApprovalNotification.objects.create(job=self.job, status="sending")
        self.flush(lambda request: self.fail("Potentially delivered send cannot be replayed"))
        row.refresh_from_db()
        self.assertEqual(row.status, "uncertain")

    def test_connection_failure_retries_and_does_not_change_review_status(self):
        from core.models import ApprovalNotification

        def disconnected(request):
            raise httpx.ConnectError("private-token")

        self.flush(disconnected)
        row = ApprovalNotification.objects.get(job=self.job)
        self.assertEqual(row.status, "pending")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "completed")
        row.next_attempt_at = timezone.now() - timedelta(seconds=1)
        row.save()
        self.flush(
            lambda request: httpx.Response(
                200, json={"ok": True, "result": {"message_id": 10, "chat": {"id": 456}}}
            )
        )
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")
        self.assertEqual(row.attempts, 2)

    def test_rate_limit_blocks_new_alerts_until_retry_time(self):
        self.flush(
            lambda request: httpx.Response(
                429, json={"ok": False, "parameters": {"retry_after": 600}}
            )
        )
        newer = Job.objects.create(
            pr=self.pr,
            generation=2,
            head_sha="b" * 40,
            actor="dev",
            status="completed",
            finished_at=timezone.now(),
        )
        ReviewCheckpoint.objects.create(
            job=newer,
            stage="done",
            encrypted_payload=encrypt(json.dumps({"outcome": "ready", "head_sha": newer.head_sha})),
        )
        self.flush(
            lambda request: self.fail("Bot-wide rate limit must apply to newly queued approvals")
        )
        self.assertEqual(newer.approval_notification.status, "pending")

    def test_httpx_info_logging_does_not_expose_bot_token(self):
        import logging
        from io import StringIO

        logger = logging.getLogger("httpx")
        output = StringIO()
        handler = logging.StreamHandler(output)
        previous = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            self.flush(
                lambda request: httpx.Response(
                    200, json={"ok": True, "result": {"message_id": 11, "chat": {"id": 456}}}
                )
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertNotIn("private-token", output.getvalue())
        self.assertNotIn("api.telegram.org/bot", output.getvalue())

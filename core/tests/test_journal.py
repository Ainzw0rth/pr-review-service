import json
import uuid
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.utils import timezone

from core import journal
from core.models import Credential, JournalEntry
from core.secrets import decrypt, encrypt
from core.tests import test_publication, test_workflow
from core.tests.test_providers import RESULT


@override_settings(
    JOURNAL_ENABLED=True,
    JOURNAL_ENDPOINT="https://journal.example/api/v1/sessions",
    JOURNAL_REPOSITORIES=["org/repo"],
    CREDENTIAL_KEY=Fernet.generate_key().decode(),
)
class JournalTests(TestCase):
    setUp = test_workflow.WorkflowTests.setUp
    snapshot = test_workflow.WorkflowTests.snapshot
    event = test_workflow.WorkflowTests.event
    reconcile = test_workflow.WorkflowTests.reconcile
    prepare = test_publication.PublicationTests.prepare

    def queued(self, *, status="completed", release_claim=True):
        job, _ = self.prepare()
        job.status = status
        job.save()
        entry = journal.queue_job(
            job, {**RESULT, "summary": "Verified `guard()` now handles empty inputs."}, "ready"
        )
        from core.claims import release

        if release_claim:
            release(job)
        return entry

    def event_data(self):
        return {
            "schemaVersion": 1,
            "eventId": str(uuid.uuid4()),
            "sessionId": "test-session",
            "project": "org/repo",
            "source": "auto-reviewer",
            "endedAt": timezone.now().isoformat(),
            "summary": "Synthetic journal verification; not a PR review.",
        }

    def transport(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def credential(self):
        return Credential.objects.create(name="journal", encrypted_value=encrypt("a" * 43))

    def test_summary_is_encrypted_and_duplicate_capture_is_idempotent(self):
        entry = self.queued(release_claim=False)
        self.assertNotIn("empty inputs", entry.encrypted_payload)
        event = json.loads(decrypt(entry.encrypted_payload))
        self.assertIn("Final review summary:", event["summary"])
        journal.queue_job(entry.job, RESULT, "ready")
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.get().encrypted_payload, entry.encrypted_payload)

    def test_failed_review_does_not_export_unaccepted_model_text(self):
        entry = self.queued(status="attention")
        summary = json.loads(decrypt(entry.encrypted_payload))["summary"]
        self.assertNotIn("empty inputs", summary)
        self.assertIn("No accepted final review", summary)

    def test_disabled_or_non_allowlisted_projects_are_not_captured(self):
        for config in [{"JOURNAL_ENABLED": False}, {"JOURNAL_REPOSITORIES": ["different/repo"]}]:
            with override_settings(**config):
                self.assertIsNone(journal.enqueue(self.event_data(), "org/repo"))
        self.assertFalse(JournalEntry.objects.exists())

    def test_oversized_summary_rejected_not_truncated(self):
        event = self.event_data()
        event["summary"] = "x" * 65537
        with self.assertRaises(ValueError):
            journal.enqueue(event, "org/repo")
        self.assertFalse(JournalEntry.objects.exists())

    def test_accepted_delivery_purges_payload_keeps_receipt(self):
        entry = self.queued()
        credential = self.credential()

        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer " + "a" * 43)
            self.assertEqual(json.loads(request.content)["eventId"], str(entry.pk))
            return httpx.Response(
                201,
                json={
                    "eventId": str(entry.pk),
                    "status": "accepted",
                    "acceptedAt": timezone.now().isoformat(),
                },
            )

        with (
            override_settings(JOURNAL_CREDENTIAL_ID=credential.pk),
            patch("core.journal.httpx.Client", return_value=self.transport(handler)),
        ):
            self.assertEqual(journal.flush(), {"sent": 1, "failed": 0})
        entry.refresh_from_db()
        self.assertIsNotNone(entry.sent_at)
        self.assertEqual(entry.encrypted_payload, "")
        self.assertEqual(entry.attempts, 1)
        self.assertEqual(journal.flush(), {"sent": 0, "failed": 0})

    def test_failed_or_wrong_receipt_keeps_same_event_for_retry(self):
        entry = self.queued()
        credential = self.credential()
        payload = entry.encrypted_payload
        for response in [
            httpx.Response(503, text="secret internal response"),
            httpx.Response(
                201,
                json={
                    "eventId": str(uuid.uuid4()),
                    "status": "accepted",
                    "acceptedAt": timezone.now().isoformat(),
                },
            ),
            httpx.Response(302, headers={"location": "https://other.example/"}),
        ]:
            with (
                override_settings(JOURNAL_CREDENTIAL_ID=credential.pk),
                patch(
                    "core.journal.httpx.Client",
                    return_value=self.transport(lambda request: response),
                ),
            ):
                self.assertEqual(journal.flush(), {"sent": 0, "failed": 1})
            entry.refresh_from_db()
            self.assertEqual(entry.encrypted_payload, payload)
            self.assertIsNone(entry.sent_at)
            self.assertEqual(entry.last_error, journal.FAILURE)

    def test_disabling_stops_delivery_of_pending_entries(self):
        self.queued()
        with override_settings(JOURNAL_ENABLED=False), patch("core.journal.httpx.Client") as client:
            self.assertEqual(journal.flush(), {"sent": 0, "failed": 0})
            client.assert_not_called()

    def test_unsafe_endpoint_never_receives_credential(self):
        self.queued()
        credential = self.credential()
        for endpoint in [
            "http://remote.example/api/v1/sessions",
            "https://a:b@journal.example/api/v1/sessions",
            "https://journal.example/api/v1/sessions?token=oops",
        ]:
            with (
                override_settings(JOURNAL_ENDPOINT=endpoint, JOURNAL_CREDENTIAL_ID=credential.pk),
                patch("core.journal.httpx.Client") as client,
            ):
                self.assertEqual(journal.flush(), {"sent": 0, "failed": 1})
                client.assert_not_called()

    def test_pending_entry_survives_job_retention(self):
        entry = self.queued()
        entry.job.delete()
        entry.refresh_from_db()
        self.assertIsNone(entry.job_id)
        self.assertTrue(entry.encrypted_payload)

    def test_admin_excludes_encrypted_summary(self):
        from core.admin import JournalEntryAdmin

        self.assertNotIn("encrypted_payload", JournalEntryAdmin.fields)

"""Audited termination attestation releases only explicitly confirmed remote capacity."""

import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from core.execution_dispatch import digest, encoded, slot_blocked
from core.models import ExecutionAssignment, ExecutionOutput, ExecutionWorker, Receipt


class WorkerReconciliationTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", is_superuser=True, is_staff=True)
        settings = override_settings(DASHBOARD_RETRY_USER_ID=self.owner.pk)
        settings.enable()
        self.addCleanup(settings.disable)
        self.worker = ExecutionWorker.objects.create(
            name="Revoked", credential_digest="a" * 64, revoked_at=timezone.now()
        )
        self.assignment = ExecutionAssignment.objects.create(
            worker=self.worker,
            incarnation="a" * 32,
            operation="model",
            state="quarantined",
            scope="s" * 64,
            slot_index=0,
            input_hash="b" * 64,
            encrypted_input="private-input",
            expires_at=timezone.now() - timedelta(days=3),
            output_bytes=7,
            next_sequence=1,
        )
        ExecutionOutput.objects.create(
            assignment=self.assignment, sequence=0, digest="c" * 64, encrypted_data="private-output"
        )
        self.evidence = "Verified worker host node-7 at 2026-10-26T12:00Z: agent stopped; exact worker-labelled container IDs removed and absence checked."

    def reconcile(self, **overrides):
        options = {
            "operator": "owner",
            "evidence": self.evidence,
            "confirm_terminated": True,
            "stdout": io.StringIO(),
            **overrides,
        }
        call_command("reconcile_worker_assignment", str(self.assignment.pk), **options)

    def assert_unresolved(self):
        self.assignment.refresh_from_db()
        self.assertFalse(self.assignment.cleanup_confirmed)
        self.assertEqual(self.assignment.state, "quarantined")
        self.assertTrue(self.assignment.encrypted_input)
        self.assertTrue(self.assignment.output.exists())
        self.assertTrue(slot_blocked(self.assignment.scope, self.assignment.slot_index))

    def test_expiry_revocation_and_evidence_without_explicit_flag_do_not_release_capacity(self):
        with self.assertRaises(CommandError):
            self.reconcile(confirm_terminated=False)
        self.assert_unresolved()

    def test_unknown_inactive_nonowner_and_wrong_owner_rejected(self):
        users = get_user_model()
        users.objects.create_user("member")
        users.objects.create_user("other-admin", is_superuser=True)
        for operator in ("missing", "member", "other-admin"):
            with self.subTest(operator=operator), self.assertRaises(CommandError):
                self.reconcile(operator=operator)
            self.assert_unresolved()
        self.owner.is_active = False
        self.owner.save()
        with self.assertRaises(CommandError):
            self.reconcile()
        self.assert_unresolved()

    def test_unrevoked_worker_rejected(self):
        self.worker.revoked_at = None
        self.worker.save()
        with self.assertRaises(CommandError):
            self.reconcile()
        self.assert_unresolved()

    def test_evidence_required_and_bounded(self):
        for evidence in ("", " \n\t", "x" * 4097):
            with self.subTest(length=len(evidence)), self.assertRaises(CommandError):
                self.reconcile(evidence=evidence)
            self.assert_unresolved()

    def test_explicit_termination_retains_audit_purges_payload_and_never_publishes_or_stops(self):
        receipts = Receipt.objects.count()
        with patch("core.github.GitHub") as github, patch("subprocess.run") as process:
            self.reconcile()
        github.assert_not_called()
        process.assert_not_called()
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.state, "failed")
        self.assertTrue(self.assignment.cleanup_confirmed)
        self.assertFalse(slot_blocked(self.assignment.scope, self.assignment.slot_index))
        self.assertEqual(self.assignment.encrypted_input, "")
        self.assertFalse(self.assignment.output.exists())
        self.assertEqual(self.assignment.output_bytes, 0)
        audit = self.assignment.result["audit"]
        self.assertEqual(audit["operator_id"], self.owner.pk)
        self.assertEqual(audit["operator"], "owner")
        self.assertEqual(audit["evidence"], self.evidence)
        self.assertTrue(audit["confirmed_at"])
        self.assertEqual(self.assignment.result["error"], "operator_confirmed_termination")
        self.assertEqual(self.assignment.result_digest, digest(encoded(self.assignment.result)))
        self.assertEqual(Receipt.objects.count(), receipts)
        self.worker.refresh_from_db()
        self.assertIsNotNone(self.worker.revoked_at)

    def test_repeat_is_idempotent_and_cannot_overwrite_audit(self):
        self.reconcile()
        self.assignment.refresh_from_db()
        original = (
            self.assignment.result,
            self.assignment.result_digest,
            self.assignment.updated_at,
        )
        self.reconcile(evidence="Different evidence must not overwrite the original audit.")
        self.assignment.refresh_from_db()
        self.assertEqual(
            (self.assignment.result, self.assignment.result_digest, self.assignment.updated_at),
            original,
        )

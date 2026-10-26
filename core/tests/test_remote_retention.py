from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models import ExecutionAssignment, ExecutionOutput, ExecutionWorker, Job
from core.tests import test_claims


class RemoteRetentionTests(TestCase):
    setUp = test_claims.ClaimTests.setUp
    job = test_claims.ClaimTests.job

    def assignment(self, number, cleaned):
        job = self.job(number)
        Job.objects.filter(pk=job.pk).update(
            status="completed", finished_at=timezone.now() - timedelta(days=91)
        )
        worker, _ = ExecutionWorker.objects.get_or_create(
            name="Retention", defaults={"credential_digest": "a" * 64}
        )
        row = ExecutionAssignment.objects.create(
            worker=worker,
            job=job,
            incarnation="a" * 32,
            operation="model",
            state="completed" if cleaned else "quarantined",
            cleanup_confirmed=cleaned,
            scope="s" * 64,
            slot_index=0,
            input_hash="h" * 64,
            encrypted_input="",
            expires_at=timezone.now() - timedelta(days=91),
            created_at=timezone.now() - timedelta(days=91),
        )
        ExecutionOutput.objects.create(
            assignment=row, sequence=0, digest="d" * 64, encrypted_data="private spool"
        )
        return row

    def test_history_cleanup_deletes_confirmed_remote_records_but_preserves_quarantine(self):
        cleaned = self.assignment(1, True)
        pending = self.assignment(2, False)
        call_command("cleanup", stdout=StringIO())
        self.assertFalse(Job.objects.filter(pk=cleaned.job_id).exists())
        self.assertFalse(ExecutionAssignment.objects.filter(pk=cleaned.pk).exists())
        self.assertTrue(Job.objects.filter(pk=pending.job_id).exists())
        self.assertTrue(ExecutionOutput.objects.filter(assignment=pending).exists())

from datetime import timedelta

from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import (
    AccessLink,
    ExecutionAssignment,
    ExpectedLabel,
    Job,
    JournalEntry,
    LoginAttempt,
    ModelProbe,
    PhaseCheckpoint,
    ReviewCheckpoint,
    Webhook,
    WorkerEnrollment,
)


class Command(BaseCommand):
    help = "Delete terminal job history older than 90 days and expired operational records."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=90)
        # Retain uncertain remote execution even when its job is terminal/old.
        # Serialize with dispatch so an assignment cannot appear during retention.
        from core.claims import pool

        with transaction.atomic():
            pool()
            expired = Job.objects.filter(
                finished_at__lt=cutoff,
                status__in=["completed", "failed", "cancelled", "superseded"],
                execution_claim__isnull=True,
            ).exclude(
                pk__in=ExecutionAssignment.objects.filter(cleanup_confirmed=False)
                .exclude(job_id=None)
                .values("job_id")
            )
            ExecutionAssignment.objects.filter(job__in=expired, cleanup_confirmed=True).delete()
            jobs, _ = expired.delete()
            ExecutionAssignment.objects.filter(
                job=None, cleanup_confirmed=True, created_at__lt=cutoff
            ).delete()
        WorkerEnrollment.objects.filter(expires_at__lt=cutoff).delete()
        ModelProbe.objects.filter(created_at__lt=cutoff).delete()
        PhaseCheckpoint.objects.filter(
            created_at__lt=cutoff, job__execution_claim__isnull=True
        ).delete()
        ReviewCheckpoint.objects.filter(
            created_at__lt=cutoff, job__execution_claim__isnull=True
        ).delete()
        JournalEntry.objects.filter(sent_at__lt=cutoff).delete()
        Webhook.objects.filter(processed_at__lt=cutoff).delete()
        AccessLink.objects.filter(expires_at__lt=cutoff).delete()
        ExpectedLabel.objects.filter(expires_at__lt=timezone.now()).delete()
        LoginAttempt.objects.filter(window_at__lt=timezone.now() - timedelta(days=1)).delete()
        Session.objects.filter(expire_date__lt=timezone.now()).delete()
        self.stdout.write(f"Removed {jobs} expired job/receipt records.")

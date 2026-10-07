from datetime import timedelta

from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import (
    AccessLink,
    ExpectedLabel,
    Job,
    JournalEntry,
    LoginAttempt,
    ReviewCheckpoint,
    Webhook,
)


class Command(BaseCommand):
    help = "Delete terminal job history older than 90 days and expired operational records."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=90)
        jobs, _ = Job.objects.filter(
            finished_at__lt=cutoff, status__in=["completed", "failed", "cancelled", "superseded"]
        ).delete()
        ReviewCheckpoint.objects.filter(created_at__lt=cutoff).delete()
        JournalEntry.objects.filter(sent_at__lt=cutoff).delete()
        Webhook.objects.filter(processed_at__lt=cutoff).delete()
        AccessLink.objects.filter(expires_at__lt=cutoff).delete()
        ExpectedLabel.objects.filter(expires_at__lt=timezone.now()).delete()
        LoginAttempt.objects.filter(window_at__lt=timezone.now() - timedelta(days=1)).delete()
        Session.objects.filter(expire_date__lt=timezone.now()).delete()
        self.stdout.write(f"Removed {jobs} expired job/receipt records.")

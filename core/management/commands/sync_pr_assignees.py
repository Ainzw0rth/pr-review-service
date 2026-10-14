"""Backfill display metadata; never trigger reviews or write to GitHub."""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.assignees import sync_assignees
from core.github import GitHub, IntegrationError
from core.models import PullRequest


class Command(BaseCommand):
    help = "Read current GitHub assignees for retained PRs; default: previously unsynced only."

    def add_arguments(self, parser):
        parser.add_argument("--repository", help="Exact owner/repository")
        parser.add_argument("--refresh", action="store_true", help="Also refresh known assignments")

    def handle(self, *args, **options):
        prs = (
            PullRequest.objects.filter(
                Q(jobs__created_at__gte=timezone.now() - timedelta(days=90))
                | Q(jobs__status__in=["queued", "running", "quota", "attention"]),
            )
            .select_related("repository__github_credential")
            .distinct()
        )
        if options["repository"]:
            prs = prs.filter(repository__full_name=options["repository"])
        if not options["refresh"]:
            prs = prs.filter(assignees_synced_at__isnull=True)
        count = 0
        for pr in prs.iterator():
            try:
                with GitHub(pr.repository) as github:
                    snapshot = github.pull(pr.number)
                if snapshot.get("number") != pr.number or "assignees" not in snapshot:
                    raise ValueError("Incomplete PR snapshot")
                with transaction.atomic():
                    current = PullRequest.objects.select_for_update().get(pk=pr.pk)
                    if current.assignees_synced_at != pr.assignees_synced_at:
                        continue  # A webhook synced fresher metadata while our fetch ran.
                    sync_assignees(current, snapshot)
            except (IntegrationError, ValueError):
                raise CommandError(
                    f"Could not sync {pr.repository.full_name}#{pr.number}; rerun to resume."
                ) from None
            count += 1
        self.stdout.write(f"Synced assignees for {count} PRs. No reviews triggered.")

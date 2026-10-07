"""Drain job intake without cancelling the active review or touching GitHub."""

import fcntl

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import Job


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["drain", "resume", "status"])

    def handle(self, *args, **options):
        flag = settings.DATA_DIR / "worker.drain"
        with open(settings.DATA_DIR / "worker.intake.lock", "a") as intake:
            fcntl.flock(intake, fcntl.LOCK_EX)
            if options["action"] == "drain":
                flag.touch(mode=0o600, exist_ok=True)
            elif options["action"] == "resume":
                flag.unlink(missing_ok=True)
            active = Job.objects.filter(status="running").count()
        self.stdout.write(
            f"Intake: {'draining' if flag.exists() else 'enabled'}. Active jobs: {active}."
        )
        if flag.exists() and not active:
            self.stdout.write("Idle. Confirm worker supports draining before restarting it.")

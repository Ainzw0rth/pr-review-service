"""Drain job intake without cancelling the active review or touching GitHub."""

import fcntl
import json

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import Job


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["drain", "resume", "status"])
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        flag = settings.DATA_DIR / "worker.drain"
        acknowledged = settings.DATA_DIR / "worker.drained"
        with open(settings.DATA_DIR / "worker.intake.lock", "a") as intake:
            fcntl.flock(intake, fcntl.LOCK_EX)
            if options["action"] == "drain":
                if not flag.exists():
                    acknowledged.unlink(missing_ok=True)
                flag.touch(mode=0o600, exist_ok=True)
            elif options["action"] == "resume":
                flag.unlink(missing_ok=True)
                acknowledged.unlink(missing_ok=True)
            active = Job.objects.filter(status="running").count()
            draining = flag.exists()
            token = acknowledged.read_text().strip() if acknowledged.exists() else ""
            idle = draining and bool(token) and active == 0
        if options.get("json"):
            self.stdout.write(
                json.dumps(
                    {
                        "draining": draining,
                        "active_jobs": active,
                        "idle": idle,
                        "worker_token": token,
                    }
                )
            )
            return
        self.stdout.write(
            f"Intake: {'draining' if flag.exists() else 'enabled'}. Active jobs: {active}."
        )
        if idle:
            self.stdout.write("Worker acknowledged idle; review and cleanup finished.")
        elif draining:
            self.stdout.write("Waiting for worker acknowledgment; do not restart yet.")

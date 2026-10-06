import fcntl
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import Repository
from core.runner import cleanup_orphans
from core.worker import recover_interrupted, run_job
from core.workflow import claim


class Command(BaseCommand):
    help = "Run the single serial review worker. Never start without the shared data volume."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        with open(settings.DATA_DIR / "worker.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if any(
                isinstance(command, list) and command
                for command in Repository.objects.values_list("test_command", flat=True)
            ):
                cleanup_orphans()
            if settings.CLI_RUNNER_IMAGE:
                from core.cli import cleanup_cli_orphans

                cleanup_cli_orphans()
            recover_interrupted()
            while True:
                job = claim()
                if job:
                    run_job(job)
                if options["once"]:
                    return
                if not job:
                    time.sleep(2)

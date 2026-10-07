import fcntl
import time
import uuid

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
            worker_token = uuid.uuid4().hex
            (settings.DATA_DIR / "worker.drained").unlink(missing_ok=True)
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
                with open(settings.DATA_DIR / "worker.intake.lock", "a") as intake:
                    fcntl.flock(intake, fcntl.LOCK_EX)
                    draining = (settings.DATA_DIR / "worker.drain").exists()
                    if draining:
                        # Reached only after run_job and all of its finally cleanup.
                        (settings.DATA_DIR / "worker.drained").write_text(worker_token)
                    else:
                        (settings.DATA_DIR / "worker.drained").unlink(missing_ok=True)
                if draining:
                    if options["once"]:
                        return
                    time.sleep(2)
                    continue
                from core.quota import refresh_due

                refresh_due()
                if (settings.DATA_DIR / "worker.drain").exists():
                    continue
                # Serialize the final drain check and claim with worker_control.
                # Once drain returns, no job can slip through a check/claim race.
                with open(settings.DATA_DIR / "worker.intake.lock", "a") as intake:
                    fcntl.flock(intake, fcntl.LOCK_EX)
                    job = None if (settings.DATA_DIR / "worker.drain").exists() else claim()
                if job:
                    run_job(job)
                if options["once"]:
                    return
                if not job:
                    time.sleep(2)

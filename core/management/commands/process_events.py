import fcntl
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from core.events import process_pending


class Command(BaseCommand):
    help = "Reconcile durable GitHub events (run independently of the review worker)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        with open(settings.DATA_DIR / "events.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while True:
                process_pending()
                if options["once"]:
                    return
                time.sleep(3)

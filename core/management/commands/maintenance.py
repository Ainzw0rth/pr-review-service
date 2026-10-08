import time

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run hourly cleanup and retry optional journal delivery every minute."

    def handle(self, *args, **options):
        from core.journal import flush
        from core.notifications import flush_approvals

        next_cleanup = 0
        while True:
            if time.monotonic() >= next_cleanup:
                call_command("cleanup")
                next_cleanup = time.monotonic() + 3600
            try:
                flush()
            except Exception:
                self.stderr.write("Journal delivery unavailable; retrying later.")
            try:
                result = flush_approvals()
                if result.get("error"):
                    self.stderr.write("Telegram approval notification configuration unavailable.")
            except Exception:
                self.stderr.write("Telegram approval notification maintenance unavailable.")
            time.sleep(60)

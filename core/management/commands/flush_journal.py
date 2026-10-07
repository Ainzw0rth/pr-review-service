from django.core.management.base import BaseCommand

from core.journal import flush


class Command(BaseCommand):
    help = "Retry up to eight pending private journal summaries; never prints their content."

    def handle(self, *args, **options):
        result = flush()
        self.stdout.write(f"Journal: {result['sent']} accepted; {result['failed']} retained for retry.")

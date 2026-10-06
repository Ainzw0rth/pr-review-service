import time

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run hourly retention cleanup; active attempts are never expired."

    def handle(self, *args, **options):
        while True:
            call_command("cleanup")
            time.sleep(3600)

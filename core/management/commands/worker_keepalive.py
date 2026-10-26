import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from core.worker_keepalive import run_due_checks


class Command(BaseCommand):
    help = "Run the dedicated external worker connectivity scheduler."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        while True:
            close_old_connections()
            run_due_checks()
            if options["once"]:
                return
            time.sleep(5)

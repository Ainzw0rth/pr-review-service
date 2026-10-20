"""Durable ingress maintenance gate and database readiness for reviewed deployments."""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("action", choices=["pause", "open", "check"])

    def handle(self, *args, **options):
        flag = settings.DATA_DIR / "deployment.maintenance"
        if options["action"] == "pause":
            flag.touch(mode=0o600, exist_ok=True)
        elif options["action"] == "open":
            flag.unlink(missing_ok=True)
        else:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)
        self.stdout.write(
            "Deployment gate updated" if options["action"] != "check" else "Database ready"
        )

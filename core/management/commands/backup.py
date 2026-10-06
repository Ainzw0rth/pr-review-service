import os
import sqlite3
from datetime import datetime, timezone

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create a consistent private SQLite backup under the persistent data directory."

    def handle(self, *args, **options):
        directory = settings.DATA_DIR / "backups"
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3")
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with (
            sqlite3.connect(settings.DATABASES["default"]["NAME"]) as source,
            sqlite3.connect(target) as destination,
        ):
            source.backup(destination)
        self.stdout.write(f"Backup created: {target}. Preserve CREDENTIAL_KEY separately.")

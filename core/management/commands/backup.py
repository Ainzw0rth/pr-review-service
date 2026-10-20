"""Consistent private backup; never publish partial archives or database errors."""

import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = __doc__

    def handle(self, *args, **options):
        vendor = connection.vendor
        if vendor not in ("sqlite", "postgresql"):
            raise CommandError("Unsupported backup backend")
        directory = settings.DATA_DIR / "backups"
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        suffix = ".sqlite3" if vendor == "sqlite" else ".pgdump"
        target = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + suffix)
        fd, temporary = tempfile.mkstemp(prefix=".partial-", dir=directory)
        os.close(fd)
        published = False
        try:
            config = connection.settings_dict
            if vendor == "sqlite":
                source_path = Path(config["NAME"]).resolve()
                with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source:
                    with sqlite3.connect(temporary) as destination:
                        source.backup(destination)
                        if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                            raise ValueError("integrity")
            else:
                env = dict(
                    os.environ,
                    **{
                        key: str(config.get(value) or "")
                        for key, value in (
                            ("PGDATABASE", "NAME"),
                            ("PGUSER", "USER"),
                            ("PGPASSWORD", "PASSWORD"),
                            ("PGHOST", "HOST"),
                            ("PGPORT", "PORT"),
                        )
                    },
                )
                env["PGCONNECT_TIMEOUT"] = "5"
                subprocess.run(
                    ["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", temporary],
                    env=env,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.run(
                    ["pg_restore", "--list", temporary],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            with open(temporary, "rb") as archive:
                os.fsync(archive.fileno())
            # Exclusive publication: never overwrite a previous archive.
            os.link(temporary, target)
            published = True
            directory_fd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            if published:
                target.unlink(missing_ok=True)
            raise CommandError(
                "Backup failed; no usable archive published. Inspect storage/client configuration."
            ) from None
        finally:
            Path(temporary).unlink(missing_ok=True)
        self.stdout.write(
            f"Backup created: {target}. Preserve CREDENTIAL_KEY and DJANGO_SECRET_KEY separately."
        )

"""Migrate only a new private working copy; validate every original column and PK."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from .transfer_sqlite_to_postgres import fingerprint


def quote(name):
    return '"' + name.replace('"', '""') + '"'


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True)
        parser.add_argument("--directory", required=True)
        parser.add_argument("--offline", action="store_true")

    def handle(self, *args, **options):
        source = Path(options["source"]).resolve()
        directory = Path(options["directory"]).resolve()
        if (
            not options["offline"]
            or source == (settings.DATA_DIR / "reviewdesk.sqlite3").resolve()
            or not source.is_file()
            or any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
        ):
            raise CommandError(
                "Requires closed offline SQLite snapshot and --offline acknowledgment"
            )
        directory.mkdir(mode=0o700)  # Refuse an existing working directory.
        target = directory / "reviewdesk.sqlite3"
        original = fingerprint(source)
        try:
            with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
                if (
                    db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                    or db.execute("PRAGMA foreign_key_check").fetchall()
                ):
                    raise ValueError
                highwater = dict(db.execute("SELECT name, seq FROM sqlite_sequence"))
                with sqlite3.connect(target) as copy:
                    db.backup(copy)
                os.chmod(target, 0o600)
                env = dict(
                    os.environ,
                    DB_ENGINE="sqlite",
                    DATA_DIR=str(directory),
                    DJANGO_SETTINGS_MODULE="reviewdesk.settings",
                    DJANGO_SECRET_KEY=settings.SECRET_KEY,
                    CREDENTIAL_KEY=settings.CREDENTIAL_KEY,
                )
                subprocess.run(
                    [sys.executable, str(settings.BASE_DIR / "manage.py"), "migrate", "--noinput"],
                    env=env,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # SQLite table rebuilds can reset AUTOINCREMENT high-water marks.
                # Restore them on the disposable copy, never on the retained snapshot.
                with sqlite3.connect(target) as copy:
                    for table, high in highwater.items():
                        copy.execute(
                            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?",
                            (high, table),
                        )
                        if not copy.execute(
                            "SELECT 1 FROM sqlite_sequence WHERE name = ?", (table,)
                        ).fetchone():
                            copy.execute(
                                "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                                (table, high),
                            )
                with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as copy:
                    checked = []
                    for (table,) in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ):
                        columns = [
                            r[1] for r in db.execute("PRAGMA table_info(" + quote(table) + ")")
                        ]
                        select = "SELECT " + ",".join(map(quote, columns)) + " FROM " + quote(table)
                        before = db.execute(select).fetchall()
                        after = copy.execute(select).fetchall()
                        # Only migration ledger/permissions/contenttypes may acquire seed rows.
                        if table in ("django_migrations", "auth_permission", "django_content_type"):
                            if not set(before) <= set(after):
                                raise ValueError
                        elif sorted(before, key=repr) != sorted(after, key=repr):
                            raise ValueError
                        checked.append(table)
                    if copy.execute("PRAGMA foreign_key_check").fetchall():
                        raise ValueError
                if fingerprint(source) != original or any(
                    Path(str(source) + s).exists() for s in ("-wal", "-shm", "-journal")
                ):
                    raise ValueError
            report = directory / "staging.json"
            report.write_text(
                json.dumps(
                    {
                        "source_sha256": original,
                        "working_copy_sha256": fingerprint(target),
                        "preserved_tables": checked,
                        "status": "validated",
                    },
                    indent=2,
                )
                + "\n"
            )
            os.chmod(report, 0o600)
        except Exception:
            raise CommandError(
                "Staging failed. Original snapshot untouched; discard working directory and investigate."
            ) from None
        self.stdout.write(
            f"Validated working copy: {target}; original snapshot retained unchanged."
        )

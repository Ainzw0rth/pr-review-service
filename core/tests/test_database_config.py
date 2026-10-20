import os
import subprocess
import sys

from django.test import SimpleTestCase


class DatabaseConfigTests(SimpleTestCase):
    def config(self, **env):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("DB_")}
        return subprocess.run(
            [
                sys.executable,
                "-c",
                'import reviewdesk.settings as s; print(s.DATABASES["default"]["ENGINE"])',
            ],
            env={**clean, "DJANGO_SECRET_KEY": "test", **env},
            capture_output=True,
            text=True,
        )

    def test_sqlite_default(self):
        self.assertIn("sqlite3", self.config().stdout)

    def test_postgres_requires_complete_config(self):
        self.assertNotEqual(self.config(DB_ENGINE="postgresql").returncode, 0)

    def test_unknown_engine_rejected(self):
        self.assertNotEqual(self.config(DB_ENGINE="postgress").returncode, 0)

    def test_port_bounds(self):
        self.assertNotEqual(
            self.config(
                DB_ENGINE="postgresql",
                DB_NAME="a",
                DB_USER="a",
                DB_PASSWORD="x",
                DB_HOST="db",
                DB_PORT="65536",
            ).returncode,
            0,
        )

    def test_postgres(self):
        self.assertIn(
            "postgresql",
            self.config(
                DB_ENGINE="postgresql", DB_NAME="a", DB_USER="a", DB_PASSWORD="x", DB_HOST="db"
            ).stdout,
        )

    def test_sqlite_worker_refuses_start_without_explicit_local_flag(self):
        from unittest.mock import patch

        from django.core.management import call_command
        from django.core.management.base import CommandError

        with (
            patch("core.management.commands.review_worker.connection") as connection,
            patch("core.management.commands.review_worker.register_owner") as register,
        ):
            connection.vendor = "sqlite"
            with self.assertRaises(CommandError):
                call_command("review_worker", once=True)
            register.assert_not_called()

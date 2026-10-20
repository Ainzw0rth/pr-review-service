import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings


class BackupTests(TestCase):
    def test_unsupported_vendor_never_creates_archive(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            override_settings(DATA_DIR=Path(temp)),
            patch("core.management.commands.backup.connection") as connection,
        ):
            connection.vendor = "unknown"
            with self.assertRaises(CommandError):
                call_command("backup")
            self.assertFalse((Path(temp) / "backups").exists())

    def test_failed_dump_never_publishes_partial_or_leaks_error(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            override_settings(DATA_DIR=Path(temp)),
            patch("core.management.commands.backup.connection") as connection,
            patch(
                "core.management.commands.backup.subprocess.run", side_effect=RuntimeError("secret")
            ),
        ):
            connection.vendor = "postgresql"
            connection.settings_dict = {"PASSWORD": "secret"}
            with self.assertRaises(CommandError) as caught:
                call_command("backup")
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(list((Path(temp) / "backups").iterdir()), [])

    def test_dump_and_listing_both_required_before_publication(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            override_settings(DATA_DIR=Path(temp)),
            patch("core.management.commands.backup.connection") as connection,
            patch("core.management.commands.backup.subprocess.run") as run,
        ):
            connection.vendor = "postgresql"
            connection.settings_dict = {"PASSWORD": "secret"}
            call_command("backup")
            self.assertEqual([c.args[0][0] for c in run.call_args_list], ["pg_dump", "pg_restore"])
            self.assertNotIn("secret", repr(run.call_args_list[0].args))
            paths = list((Path(temp) / "backups").iterdir())
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)

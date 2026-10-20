import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings


@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL")
class TransferTests(TransactionTestCase):
    def test_complete_typed_roundtrip_and_populated_target_refusal(self):
        from core.models import Credential, Organization, ServiceSettings

        key = Fernet.generate_key().decode()
        with (
            tempfile.TemporaryDirectory() as temp,
            override_settings(CREDENTIAL_KEY=key),
            patch.object(type(self), "databases", {"default", "_offline_transfer"}),
        ):
            root = Path(temp)
            env = {
                **os.environ,
                "DB_ENGINE": "sqlite",
                "DATA_DIR": temp,
                "DJANGO_SETTINGS_MODULE": "reviewdesk.settings",
                "DJANGO_SECRET_KEY": "transfer-test",
                "CREDENTIAL_KEY": key,
            }
            subprocess.run(
                [sys.executable, "manage.py", "migrate", "--noinput"],
                env=env,
                check=True,
                capture_output=True,
            )
            seed = """import django; django.setup()
from core.models import *
from core.secrets import encrypt
from django.contrib.auth.models import User, Group, Permission
from django.contrib.sessions.backends.db import SessionStore
org=Organization.objects.create(pk=20,name="東京",slug="org")
Organization.objects.create(pk=90,name="deleted",slug="deleted").delete()
u=User.objects.create_user(pk=7,username="test",password="private")
g=Group.objects.create(pk=8,name="group"); u.groups.add(g); g.permissions.add(Permission.objects.first());u.user_permissions.add(Permission.objects.last())
Membership.objects.create(organization=org,user=u)
c=Credential.objects.create(pk=12,name="secret",encrypted_value=encrypt("secret-value"))
r=Repository.objects.create(organization=org,full_name="a/b",reviewer="r",github_credential=c,webhook_credential=c,required_checks=["unicode東京"],related_item_limit=None)
p=PullRequest.objects.create(repository=r,number=3,head_sha="a",labels=["a"],requested=True)
j=Job.objects.create(pr=p,generation=0,head_sha="a",actor="u",provider_diagnostics={"b":False,"a":None})
ReviewCheckpoint.objects.create(job=j,encrypted_payload=encrypt("{}"))
JournalEntry.objects.create(job=j,repository="a/b",encrypted_payload=encrypt("{}"))
Receipt.objects.create(pk=42,job=j,key="publication",remote_id="0001")
User.groups.through.objects.update(id=77)
s=SessionStore();s["_auth_user_id"]=str(u.pk);s.save()
"""
            subprocess.run([sys.executable, "-c", seed], env=env, check=True, capture_output=True)
            source = root / "reviewdesk.sqlite3"
            report = root / "report.json"
            ServiceSettings.objects.update_or_create(
                pk=1,
                defaults={"concurrent_pr_reviews": 2, "draining": False, "drain_generation": 0},
            )
            # Reject malformed inputs without repairing them or publishing imported rows.
            from core.management.commands.transfer_sqlite_to_postgres import fingerprint

            corruptions = {
                "ciphertext": "UPDATE core_credential SET encrypted_value='invalid-secret'",
                "foreign_key": "UPDATE core_membership SET user_id=99999",
                "boolean": "UPDATE core_repository SET enabled=2",
                "json": "UPDATE core_job SET provider_diagnostics='invalid-json'",
                "varchar": "UPDATE core_organization SET name='" + "x" * 101 + "'",
                "unmapped": "CREATE TABLE unmapped_data (id INTEGER PRIMARY KEY)",
                "ledger": "DELETE FROM django_migrations WHERE app='core' AND name='0020_expectedlabel_action_length'",
            }
            for name, statement in corruptions.items():
                with self.subTest(corruption=name):
                    damaged = root / (name + ".sqlite3")
                    shutil.copyfile(source, damaged)
                    with sqlite3.connect(damaged) as db:
                        db.execute("PRAGMA ignore_check_constraints=ON")
                        db.execute(statement)
                    checksum = fingerprint(damaged)
                    with (
                        override_settings(SECRET_KEY="transfer-test"),
                        self.assertRaises(CommandError) as caught,
                    ):
                        call_command(
                            "transfer_sqlite_to_postgres",
                            source=str(damaged),
                            report=str(root / (name + ".json")),
                            offline=True,
                        )
                    self.assertNotIn("invalid-secret", str(caught.exception))
                    self.assertEqual(checksum, fingerprint(damaged))
                    self.assertFalse(Organization.objects.exists())
                    self.assertFalse(Credential.objects.exists())
            # Force a post-insert canonical mismatch: all imported rows must roll back.
            from core.management.commands.transfer_sqlite_to_postgres import manifest

            counter = [0]

            def mismatched(model, rows):
                value = manifest(model, rows)
                counter[0] += 1
                if counter[0] == 2:
                    value["sha256"] = "injected-mismatch"
                return value

            with (
                override_settings(SECRET_KEY="transfer-test"),
                patch(
                    "core.management.commands.transfer_sqlite_to_postgres.manifest",
                    side_effect=mismatched,
                ),
            ):
                with self.assertRaises(CommandError):
                    call_command(
                        "transfer_sqlite_to_postgres",
                        source=str(source),
                        report=str(root / "mismatch.json"),
                        offline=True,
                    )
            self.assertFalse(Organization.objects.exists())
            self.assertFalse(Credential.objects.exists())
            with override_settings(SECRET_KEY="transfer-test"):
                call_command(
                    "transfer_sqlite_to_postgres",
                    source=str(source),
                    report=str(report),
                    offline=True,
                )
            value = json.loads(report.read_text())
            self.assertEqual(value["status"], "validated")
            self.assertEqual(value["encrypted_values_validated"], 3)
            self.assertEqual(value["sessions_validated"], 1)
            self.assertEqual(value["tables"]["core_organization"]["sequence_highwater"], 90)
            self.assertEqual(Organization.objects.create(name="next", slug="next").pk, 91)
            self.assertEqual(Credential.objects.get().pk, 12)
            from django.contrib.auth.models import User

            from core.models import Receipt

            self.assertEqual(User.groups.through.objects.get().pk, 77)
            self.assertEqual(Receipt.objects.get(pk=42).remote_id, "0001")
            with override_settings(SECRET_KEY="transfer-test"), self.assertRaises(CommandError):
                call_command(
                    "transfer_sqlite_to_postgres",
                    source=str(source),
                    report=str(root / "refused.json"),
                    offline=True,
                )
            self.assertEqual(Organization.objects.count(), 2)

            if os.environ.get("RUN_POSTGRES_BACKUP_ROUNDTRIP") == "1":
                self.backup_restore(root)

    def backup_restore(self, root):
        import psycopg
        from django.apps import apps
        from django.db import connections
        from django.db.migrations.recorder import MigrationRecorder
        from psycopg import sql

        from core.management.commands.transfer_sqlite_to_postgres import manifest, rows_for

        config = connection.settings_dict
        envfile = root / "container.env"
        values = dict(
            DB_ENGINE="postgresql",
            DB_NAME=config["NAME"],
            DB_USER=config["USER"],
            DB_PASSWORD=config["PASSWORD"],
            DB_HOST=config["HOST"],
            DB_PORT=config["PORT"],
            DJANGO_SECRET_KEY="transfer-test",
            DATA_DIR="/backup-data",
        )
        envfile.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        envfile.chmod(0o600)
        docker = [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "--user",
            "0:0",
            "--env-file",
            str(envfile),
            "-v",
            str(root) + ":/backup-data",
            "auto-reviewer:concurrency-verification",
        ]
        subprocess.run(docker + ["python", "manage.py", "backup"], check=True, capture_output=True)
        archive = next((root / "backups").glob("*.pgdump"))
        restore_name = "task3_backup_restore"
        admin = psycopg.connect(
            dbname="postgres",
            user=config["USER"],
            password=config["PASSWORD"],
            host=config["HOST"],
            port=config["PORT"],
            autocommit=True,
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restore_name)))
        try:
            values.update(
                PGDATABASE=restore_name,
                PGUSER=config["USER"],
                PGPASSWORD=config["PASSWORD"],
                PGHOST=config["HOST"],
                PGPORT=config["PORT"],
            )
            envfile.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
            subprocess.run(
                docker
                + [
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "--dbname",
                    restore_name,
                    "/backup-data/backups/" + archive.name,
                ],
                check=True,
                capture_output=True,
            )
            connections.databases["_restore"] = dict(config, NAME=restore_name)
            with patch.object(type(self), "databases", set(self.databases) | {"_restore"}):
                for model in list(apps.get_models(include_auto_created=True)) + [
                    MigrationRecorder.Migration
                ]:
                    self.assertEqual(
                        manifest(model, rows_for(model, "default")),
                        manifest(model, rows_for(model, "_restore")),
                    )
            connections["_restore"].close()
            del connections["_restore"]
            del connections.databases["_restore"]
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(restore_name))
            )
            admin.close()
            envfile.unlink()


class StagingTests(SimpleTestCase):
    def test_old_snapshot_is_unchanged_and_deleted_highest_sequence_survives_migration(self):
        import sqlite3

        from core.management.commands.transfer_sqlite_to_postgres import fingerprint

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "source"
            source_dir.mkdir()
            env = {
                **os.environ,
                "DB_ENGINE": "sqlite",
                "DATA_DIR": str(source_dir),
                "DJANGO_SETTINGS_MODULE": "reviewdesk.settings",
                "DJANGO_SECRET_KEY": "test",
            }
            subprocess.run(
                [sys.executable, "manage.py", "migrate", "--noinput"],
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [sys.executable, "manage.py", "migrate", "core", "0018", "--noinput"],
                env=env,
                check=True,
                capture_output=True,
            )
            source = source_dir / "reviewdesk.sqlite3"
            with sqlite3.connect(source) as db:
                db.execute(
                    "INSERT INTO sqlite_sequence (name,seq) VALUES ('core_expectedlabel',999)"
                )
            before = fingerprint(source)
            call_command(
                "stage_sqlite", source=str(source), directory=str(root / "stage"), offline=True
            )
            self.assertEqual(before, fingerprint(source))
            with sqlite3.connect(root / "stage" / "reviewdesk.sqlite3") as db:
                self.assertEqual(
                    db.execute(
                        "SELECT seq FROM sqlite_sequence WHERE name='core_expectedlabel'"
                    ).fetchone(),
                    (999,),
                )
            with self.assertRaises(FileExistsError):
                call_command(
                    "stage_sqlite", source=str(source), directory=str(root / "stage"), offline=True
                )


class FreshTargetCLISettingsTests(TestCase):
    def test_saved_cli_capacity_means_target_is_not_fresh(self):
        from django.apps import apps
        from django.db.migrations.recorder import MigrationRecorder

        from core.management.commands.transfer_sqlite_to_postgres import assert_fresh
        from core.models import ServiceSettings

        models = list(apps.get_models(include_auto_created=True)) + [MigrationRecorder.Migration]
        ServiceSettings.objects.filter(pk=1).update(cli_account_concurrency=6)
        with self.assertRaises(ValueError):
            assert_fresh(models, "default")
        self.assertEqual(ServiceSettings.objects.get(pk=1).cli_account_concurrency, 6)

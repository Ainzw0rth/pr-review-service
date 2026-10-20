"""Offline, current-schema SQLite snapshot to a freshly migrated PostgreSQL database."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from django.apps import apps
from django.conf import settings
from django.contrib.auth.management import _get_all_permissions
from django.core import signing
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.db.migrations.recorder import MigrationRecorder

from core.secrets import decrypt


def fingerprint(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if isinstance(value, UUID):
        return str(value)
    return value


def manifest(model, rows):
    fields = list(model._meta.local_concrete_fields)
    digest = hashlib.sha256(
        json.dumps([(f.column, f.get_internal_type()) for f in fields]).encode()
    )
    for row in rows:
        encoded = json.dumps(
            [canonical(v) for v in row],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {"count": len(rows), "sha256": digest.hexdigest()}


def rows_for(model, alias):
    return list(
        model._base_manager.using(alias)
        .order_by(model._meta.pk.name)
        .values_list(*[f.attname for f in model._meta.local_concrete_fields])
    )


def assert_fresh(models, alias):
    """Only exact migrate-generated seeds allowed; never clear user/application rows."""
    from django.contrib.auth.models import Permission
    from django.contrib.contenttypes.models import ContentType

    from core.models import ServiceSettings

    expected_types = {(m._meta.app_label, m._meta.model_name) for m in apps.get_models()}
    if set(ContentType.objects.using(alias).values_list("app_label", "model")) != expected_types:
        raise ValueError("Target content types are not fresh")
    expected_permissions = {
        (m._meta.app_label, m._meta.model_name, code, name)
        for m in apps.get_models()
        for code, name in _get_all_permissions(m._meta)
    }
    actual = set(
        Permission.objects.using(alias).values_list(
            "content_type__app_label", "content_type__model", "codename", "name"
        )
    )
    if actual != expected_permissions:
        raise ValueError("Target permissions are not fresh")
    if list(
        ServiceSettings.objects.using(alias).values_list(
            "id", "concurrent_pr_reviews", "cli_account_concurrency", "draining", "drain_generation"
        )
    ) != [(1, 2, None, False, 0)]:
        raise ValueError("Target pool settings are not fresh")
    seeded = {"django_content_type", "auth_permission", "core_servicesettings", "django_migrations"}
    for model in models:
        if model._meta.db_table not in seeded and model._base_manager.using(alias).exists():
            raise ValueError("Target contains existing data")


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True)
        parser.add_argument("--report", required=True)
        parser.add_argument(
            "--offline",
            action="store_true",
            help="Confirm all source and target writers are stopped",
        )

    def handle(self, *args, **options):
        target = connections["default"]
        source = Path(options["source"]).resolve()
        report = Path(options["report"]).resolve()
        if target.vendor != "postgresql" or not options["offline"]:
            raise CommandError(
                "Requires PostgreSQL and explicit --offline writer-stop acknowledgment"
            )
        if not source.is_file() or source == (settings.DATA_DIR / "reviewdesk.sqlite3").resolve():
            raise CommandError("Source must be a separate offline SQLite snapshot")
        if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise CommandError(
                "Source has SQLite sidecars; create a closed consistent snapshot first"
            )
        report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(report.parent, 0o700)
        # Reserve report before changing database; never overwrite evidence.
        fd = os.open(report, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        alias = "_offline_transfer"
        config = dict(connections["default"].settings_dict)
        config.update(
            ENGINE="django.db.backends.sqlite3",
            NAME=source.as_uri() + "?mode=ro",
            OPTIONS={"uri": True},
            CONN_MAX_AGE=0,
        )
        connections.databases[alias] = config
        original = fingerprint(source)
        result = {
            "status": "failed",
            "phase": "source_validation",
            "retry": "Recreate dedicated target database before retry",
            "tables": {},
        }
        try:
            db = connections[alias]
            models = list(apps.get_models(include_auto_created=True)) + [
                MigrationRecorder.Migration
            ]
            models.sort(key=lambda m: m._meta.db_table)
            tables = {m._meta.db_table for m in models}
            with db.cursor() as cursor:
                cursor.execute(
                    "BEGIN"
                )  # Hold a coherent read-only snapshot until validation completes.
                if cursor.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Source integrity failure")
                if cursor.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Source foreign key failure")
                sequence = dict(cursor.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
                if set(db.introspection.table_names(cursor)) != tables:
                    raise ValueError("Unmapped source tables")
                for model in models:
                    actual = {
                        c.name
                        for c in db.introspection.get_table_description(
                            cursor, model._meta.db_table
                        )
                    }
                    if actual != {f.column for f in model._meta.local_concrete_fields}:
                        raise ValueError("Source schema mismatch")

            def ledger(alias):
                return set(
                    MigrationRecorder.Migration.objects.using(alias).values_list("app", "name")
                )

            if ledger(alias) != ledger("default"):
                raise ValueError(
                    "Migration ledger mismatch; migrate only a disposable source working copy"
                )
            # SQLite converters are permissive: reject invalid raw booleans/JSON before
            # conversion can silently normalize them into valid PostgreSQL values.
            with db.cursor() as cursor:
                for model in models:
                    for field in model._meta.local_concrete_fields:
                        if field.get_internal_type() in ("BooleanField", "JSONField"):
                            cursor.execute(
                                "SELECT CAST("
                                + db.ops.quote_name(field.column)
                                + " AS TEXT) FROM "
                                + db.ops.quote_name(model._meta.db_table)
                            )
                            for (value,) in cursor.fetchall():
                                if value is None:
                                    continue
                                if field.get_internal_type() == "BooleanField" and value not in (
                                    "0",
                                    "1",
                                ):
                                    raise ValueError("Invalid source boolean")
                                if field.get_internal_type() == "JSONField":
                                    json.loads(value)
            data = {m._meta.db_table: rows_for(m, alias) for m in models}
            result["phase"] = "secret_validation"
            secret_count = session_count = 0
            for model in models:
                fields = list(model._meta.local_concrete_fields)
                for row in data[model._meta.db_table]:
                    for field, value in zip(fields, row):
                        if field.name in ("encrypted_value", "encrypted_payload") and value:
                            decrypt(value)
                            secret_count += 1
                        if (
                            model._meta.db_table == "django_session"
                            and field.name == "session_data"
                        ):
                            signing.loads(
                                value,
                                salt="django.contrib.sessions.SessionStore",
                                serializer=signing.JSONSerializer,
                            )
                            session_count += 1
            result["phase"] = "target_validation"
            with transaction.atomic():
                with target.cursor() as cursor:
                    if set(target.introspection.table_names(cursor)) != tables:
                        raise ValueError("Unmapped target tables")
                    quoted = ", ".join(target.ops.quote_name(t) for t in sorted(tables))
                    cursor.execute("SET LOCAL lock_timeout = '5s'")
                    cursor.execute(f"LOCK TABLE {quoted} IN ACCESS EXCLUSIVE MODE")
                    assert_fresh(models, "default")
                    cursor.execute("SET CONSTRAINTS ALL DEFERRED")
                    for model in models:
                        cursor.execute("DELETE FROM " + target.ops.quote_name(model._meta.db_table))
                    result["phase"] = "typed_insert"
                    for model in models:
                        table = model._meta.db_table
                        fields = list(model._meta.local_concrete_fields)
                        sql = (
                            "INSERT INTO "
                            + target.ops.quote_name(table)
                            + " ("
                            + ",".join(target.ops.quote_name(f.column) for f in fields)
                            + ") VALUES ("
                            + ",".join(["%s"] * len(fields))
                            + ")"
                        )
                        prepared = [
                            [f.get_db_prep_save(v, target) for f, v in zip(fields, row)]
                            for row in data[table]
                        ]
                        if prepared:
                            cursor.executemany(sql, prepared)
                    result["phase"] = "canonical_validation"
                    for model in models:
                        table = model._meta.db_table
                        expected = manifest(model, data[table])
                        if expected != manifest(model, rows_for(model, "default")):
                            raise ValueError("Canonical content mismatch")
                        result["tables"][table] = expected
                    result["phase"] = "constraint_validation"
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                    if fingerprint(source) != original or any(
                        Path(str(source) + s).exists() for s in ("-wal", "-shm", "-journal")
                    ):
                        raise ValueError("Source changed during transfer")
                    result["phase"] = "sequence_reset"
                    for model in models:
                        table = model._meta.db_table
                        cursor.execute(
                            "SELECT pg_get_serial_sequence(%s, %s)", [table, model._meta.pk.column]
                        )
                        seq = cursor.fetchone()[0]
                        if seq:
                            pk_index = list(model._meta.local_concrete_fields).index(model._meta.pk)
                            high = max(
                                [sequence.get(table, 0)] + [r[pk_index] for r in data[table]]
                            )
                            cursor.execute(
                                "SELECT setval(%s, %s, %s)", [seq, max(high, 1), high > 0]
                            )
                            result["tables"][table]["sequence_highwater"] = high
            result.update(
                status="validated",
                phase="complete",
                source_sha256=original,
                migration_sha256=hashlib.sha256(
                    json.dumps(sorted(ledger(alias))).encode()
                ).hexdigest(),
                encrypted_values_validated=secret_count,
                sessions_validated=session_count,
            )
        except Exception:
            raise CommandError(
                f"Transfer refused or failed ({result['phase']}). Target must remain offline; recreate it before retry. No row values logged."
            ) from None
        finally:
            connections[alias].close()
            del connections[alias]
            del connections.databases[alias]
            report.write_text(json.dumps(result, indent=2) + "\n")
            os.chmod(report, 0o600)
        self.stdout.write(
            "Transfer validated. Keep writers stopped; set cap 1 and explicitly drain PostgreSQL before starting workers."
        )

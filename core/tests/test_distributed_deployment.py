"""Distributed drain acknowledges durable cleanup, never remote liveness guesses."""

import json
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core.deployment import MaintenanceMiddleware
from core.models import ExecutionAssignment, ExecutionWorker, ServiceSettings, WorkerOwner


class DistributedDrainTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        setting = override_settings(DATA_DIR=Path(temp.name))
        setting.enable()
        self.addCleanup(setting.disable)
        self.owner = WorkerOwner.objects.create()
        self.status("drain")
        WorkerOwner.objects.filter(pk=self.owner.pk).update(
            drain_ack=ServiceSettings.objects.get(pk=1).drain_generation
        )
        self.worker = ExecutionWorker.objects.create(
            name="Disconnected remote",
            credential_digest="a" * 64,
            heartbeat_at=timezone.now() - timedelta(days=1),
            protocol_version=1,
        )

    def status(self, action="status"):
        output = StringIO()
        call_command("worker_control", action, json=True, stdout=output)
        return json.loads(output.getvalue())

    def assignment(self, state="running", **extra):
        return ExecutionAssignment.objects.create(
            worker=self.worker,
            incarnation="a" * 32,
            operation="model",
            state=state,
            scope="s" * 64,
            slot_index=0,
            input_hash="b" * 64,
            encrypted_input="private",
            expires_at=timezone.now() - timedelta(hours=1),
            **extra,
        )

    def test_orphan_remote_work_prevents_idle_without_any_local_job_claim(self):
        for state in ("queued", "running", "cancelling", "quarantined", "completed", "failed"):
            with self.subTest(state=state):
                row = self.assignment(state)
                result = self.status()
                self.assertEqual(result["active_jobs"], 0)
                self.assertFalse(result["idle"])
                self.assertEqual(result["remote_active"], 1)
                self.assertEqual(result["remote_quarantined"], int(state == "quarantined"))
                row.delete()

    def test_only_confirmed_cleanup_unblocks_even_after_revocation_and_expiry(self):
        row = self.assignment("quarantined")
        self.worker.revoked_at = timezone.now()
        self.worker.save()
        self.assertFalse(self.status()["idle"])
        row.cleanup_confirmed = True
        row.save()
        result = self.status()
        self.assertTrue(result["idle"])
        self.assertEqual(result["remote_active"], 0)
        self.assertEqual(result["remote_quarantined"], 0)

    def test_idle_does_not_require_empty_remote_hosts_to_heartbeat(self):
        self.worker.draining = True
        self.worker.protocol_version = 99
        self.worker.save()
        self.assertTrue(self.status()["idle"])

    def test_global_drain_does_not_cancel_remote_phases_or_mutate_worker_drain(self):
        row = self.assignment("queued")
        token = row.token
        self.status("resume")
        self.status("drain")
        row.refresh_from_db()
        self.worker.refresh_from_db()
        self.assertEqual(row.state, "queued")
        self.assertEqual(row.token, token)
        self.assertFalse(row.cleanup_confirmed)
        self.assertFalse(self.worker.draining)


class DistributedMaintenanceTests(SimpleTestCase):
    def test_worker_routes_are_blocked_before_any_database_access(self):
        # SimpleTestCase forbids DB access, including indirect session/auth middleware.
        with tempfile.TemporaryDirectory() as temp, override_settings(DATA_DIR=Path(temp)):
            (Path(temp) / "deployment.maintenance").touch()
            downstream = Mock(side_effect=AssertionError("Maintenance reached application"))
            middleware = MaintenanceMiddleware(downstream)
            for endpoint in (
                "enroll",
                "heartbeat",
                "claim",
                "input",
                "renew",
                "output",
                "complete",
            ):
                with self.subTest(endpoint=endpoint):
                    response = middleware(RequestFactory().post(f"/api/workers/v1/{endpoint}/"))
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response["Retry-After"], "120")
            downstream.assert_not_called()

    def test_drain_without_maintenance_keeps_worker_callbacks_available(self):
        with tempfile.TemporaryDirectory() as temp, override_settings(DATA_DIR=Path(temp)):
            (Path(temp) / "worker.drain").touch()
            downstream = Mock(return_value=HttpResponse("ok"))
            middleware = MaintenanceMiddleware(downstream)
            for endpoint in ("renew", "complete"):
                self.assertEqual(
                    middleware(RequestFactory().post(f"/api/workers/v1/{endpoint}/")).status_code,
                    200,
                )
            self.assertEqual(downstream.call_count, 2)


class DistributedDeployScriptTests(SimpleTestCase):
    def execute(self, remote_states):
        from scripts.deploy import deploy

        calls = []
        polls = iter(
            [
                dict(
                    draining=False, idle=False, active_jobs=0, remote_active=0, remote_quarantined=0
                ),
                *remote_states,
                dict(
                    draining=True,
                    idle=True,
                    active_jobs=0,
                    remote_active=0,
                    remote_quarantined=0,
                    worker_token="old",
                ),
                dict(
                    draining=True,
                    idle=True,
                    active_jobs=0,
                    remote_active=0,
                    remote_quarantined=0,
                    worker_token="new",
                ),
            ]
        )

        def run(*args):
            calls.append(args)
            if args[-2:] == ("status", "--json"):
                return json.dumps(next(polls))
            if args[-2:] == ("-q", "worker"):
                return "worker-container"
            if args[:2] == ("docker", "inspect"):
                return "sha256:old"
            return ""

        deploy(run=run, sleep=lambda _: None)
        return calls

    def test_remote_cleanup_blocks_stop_even_if_idle_report_is_inconsistent(self):
        calls = self.execute(
            [
                dict(
                    draining=True,
                    idle=True,
                    active_jobs=0,
                    remote_active=1,
                    remote_quarantined=1,
                    worker_token="old",
                ),
            ]
        )
        stopped = next(i for i, call in enumerate(calls) if "stop" in call)
        polls_before_stop = [call for call in calls[:stopped] if call[-2:] == ("status", "--json")]
        self.assertEqual(len(polls_before_stop), 3)

    def test_malformed_remote_counts_fail_before_any_deployment_mutation(self):
        from scripts.deploy import deploy

        for value in (-1, True, "0", None):
            with self.subTest(value=value):
                run = Mock(
                    return_value=json.dumps(
                        dict(
                            draining=False,
                            idle=False,
                            active_jobs=0,
                            remote_active=value,
                            remote_quarantined=0,
                        )
                    )
                )
                with self.assertRaises(RuntimeError):
                    deploy(run=run)
                self.assertEqual(run.call_count, 1)


class RemotePhaseDrainTests(TestCase):
    def setUp(self):
        from cryptography.fernet import Fernet

        from core.tests.test_claims import ClaimTests
        from core.workflow import claim

        ClaimTests.setUp(self)
        setting = override_settings(
            REMOTE_EXECUTION_ENABLED=True, CREDENTIAL_KEY=Fernet.generate_key().decode()
        )
        setting.enable()
        self.addCleanup(setting.disable)
        ClaimTests.job(self, 1)
        self.job = claim()
        self.addCleanup(self.job.execution_lock.close)
        self.addCleanup(self.job.claim_owner.execution_lock.close)
        self.worker = ExecutionWorker.objects.create(
            name="Remote",
            metrics={"disk_free": 8 * 1024**3},
            credential_digest="a" * 64,
            enabled=True,
            allowed_capabilities=["model"],
            capabilities=["model"],
            account_kinds=["codex_cli"],
            authenticated=True,
            protocol_version=1,
            incarnation="a" * 32,
            heartbeat_at=timezone.now(),
        )

    def submit(self):
        from types import SimpleNamespace

        from core.execution_dispatch import submit

        return submit(
            self.job,
            "model",
            {"kind": "codex_cli", "context": {}},
            SimpleNamespace(scope="s" * 64, index=0),
        )

    def test_global_and_worker_drain_allow_current_remote_review_to_finish(self):
        from core import execution_dispatch as dispatch
        from core.workflow import claim

        assignment = self.submit()
        self.assertIsNotNone(assignment)
        call_command("worker_control", "drain", stdout=StringIO())
        self.worker.draining = True
        self.worker.save()
        leased = dispatch.take(self.worker, self.worker.incarnation)
        self.assertEqual(leased["id"], str(assignment.pk))
        keys = dict(
            id=str(assignment.pk), token=str(assignment.token), incarnation=self.worker.incarnation
        )
        self.assertTrue(dispatch.renew(self.worker, keys)["continue"])
        self.assertTrue(
            dispatch.complete(
                self.worker,
                {
                    **keys,
                    "result": {"exit_code": 0, "oom": False},
                    "cleaned": True,
                },
            )["accepted"]
        )
        self.assertIsNotNone(self.submit())  # Next phase of the existing placement.
        self.assertIsNone(claim(self.job.claim_owner))  # No new coordinator admission.

    def test_worker_drain_rejects_new_placement_and_incompatible_protocol(self):
        self.worker.draining = True
        self.worker.save()
        self.assertIsNone(self.submit())
        self.worker.draining = False
        self.worker.protocol_version = 2
        self.worker.save()
        self.assertIsNone(self.submit())


class KeepaliveDeploymentTests(SimpleTestCase):
    def test_keepalive_writer_stops_before_migration_and_starts_after_readiness(self):
        from core.tests.test_deployment import DeployScriptTests

        calls = DeployScriptTests().execute()
        stop_index = next(i for i, call in enumerate(calls) if "stop" in call)
        migrate_index = next(
            i for i, call in enumerate(calls) if "migrate" in call and "--noinput" in call
        )
        self.assertIn("keepalive", calls[stop_index])
        self.assertLess(stop_index, migrate_index)
        self.assertTrue(
            any("ps" in call and "keepalive" in call for call in calls[stop_index:migrate_index])
        )
        start_index = next(
            i for i, call in enumerate(calls) if "up" in call and "keepalive" in call
        )
        readiness_index = max(
            i for i, call in enumerate(calls) if call[-2:] == ("status", "--json")
        )
        self.assertGreater(start_index, readiness_index)
        self.assertLess(start_index, next(i for i, call in enumerate(calls) if "resume" in call))


class LegacyRemoteDrainProtocolTests(SimpleTestCase):
    def test_missing_counters_require_proof_that_remote_ledger_does_not_exist(self):
        from scripts.deploy import deploy

        legacy = json.dumps(dict(draining=False, idle=False, active_jobs=0))
        for marker in ("", "remote-ledger-present", "unexpected"):
            run = Mock(side_effect=[legacy, marker])
            with (
                self.subTest(marker=marker),
                self.assertRaisesRegex(RuntimeError, "unverified schema"),
            ):
                deploy(run=run)
            self.assertEqual(run.call_count, 2)
            self.assertIn("shell", run.call_args.args)

    def test_proven_legacy_schema_reaches_readonly_deployment_preflight(self):
        from scripts.deploy import deploy

        run = Mock(
            side_effect=[
                json.dumps(dict(draining=False, idle=False, active_jobs=0)),
                "legacy-no-remote-ledger",
                "one-off-writer",
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "One-off app containers"):
            deploy(run=run)
        self.assertEqual(run.call_count, 3)

    def test_partial_counter_contract_is_rejected_without_schema_guessing(self):
        from scripts.deploy import deploy

        run = Mock(
            return_value=json.dumps(
                dict(draining=False, idle=False, active_jobs=0, remote_active=0)
            )
        )
        with self.assertRaisesRegex(RuntimeError, "supported drain acknowledgment"):
            deploy(run=run)
        self.assertEqual(run.call_count, 1)

"""PostgreSQL proofs for durable remote capacity and result fencing."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

from cryptography.fernet import Fernet
from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from core import execution_dispatch as dispatch
from core.models import ExecutionAssignment, ExecutionPlacement, ExecutionWorker
from core.tests import test_claims
from core.workflow import claim


@override_settings(REMOTE_EXECUTION_ENABLED=True, CREDENTIAL_KEY=Fernet.generate_key().decode())
class RemoteRaceTests(TransactionTestCase):
    setUp = test_claims.ClaimTests.setUp
    job = test_claims.ClaimTests.job

    def worker(self):
        return ExecutionWorker.objects.create(
            name="Remote",
            credential_digest="a" * 64,
            enabled=True,
            allowed_capabilities=["model"],
            capabilities=["model"],
            account_kinds=["codex_cli"],
            authenticated=True,
            protocol_version=1,
            incarnation="a" * 32,
            heartbeat_at=timezone.now(),
            slots=1,
            max_slots=1,
            metrics={"disk_free": 10 * 1024**3},
        )

    def claimed(self, number):
        self.job(number)
        job = claim()
        self.addCleanup(job.execution_lock.close)
        self.addCleanup(job.claim_owner.execution_lock.close)
        return job

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_coordinators_cannot_reserve_one_remote_slot(self):
        self.worker()
        jobs = [self.claimed(1), self.claimed(2)]
        barrier = Barrier(2)

        def reserve(item):
            index, job = item
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                row = dispatch.submit(
                    job,
                    "model",
                    {"kind": "codex_cli"},
                    SimpleNamespace(scope="s" * 64, index=index),
                )
                return row is not None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, enumerate(jobs)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(ExecutionPlacement.objects.count(), 1)
        self.assertEqual(ExecutionAssignment.objects.count(), 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_completion_retries_are_idempotent(self):
        worker = self.worker()
        row = dispatch.submit(
            self.claimed(1),
            "model",
            {"kind": "codex_cli"},
            SimpleNamespace(scope="s" * 64, index=0),
        )
        dispatch.take(worker, worker.incarnation)
        payload = {
            "id": str(row.pk),
            "token": str(row.token),
            "incarnation": worker.incarnation,
            "cleaned": True,
            "result": {"exit_code": 0, "oom": False},
        }
        barrier = Barrier(2)

        def finish(_):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return dispatch.complete(worker, payload)["accepted"]
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(finish, range(2))), [True, True])
        row.refresh_from_db()
        self.assertTrue(row.cleanup_confirmed)
        self.assertEqual(row.state, "completed")

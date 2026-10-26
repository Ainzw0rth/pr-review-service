import base64
from datetime import timedelta
from types import SimpleNamespace

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.utils import timezone

from core import execution_dispatch as dispatch
from core.models import ExecutionWorker
from core.tests import test_claims
from core.workflow import claim


@override_settings(REMOTE_EXECUTION_ENABLED=True, CREDENTIAL_KEY=Fernet.generate_key().decode())
class AssignmentTests(TestCase):
    setUp = test_claims.ClaimTests.setUp
    job = test_claims.ClaimTests.job

    def test_busy_phase_falls_back_local_without_queueing_active_budget(self):
        worker = self.worker()
        first = self.submit(worker)
        from core.models import JobClaim

        claim_row = JobClaim.objects.get(job_id=first.job_id)
        job = claim_row.job
        job.claim_token = claim_row.token
        job.claim_owner_id = claim_row.owner_id
        second = dispatch.submit(
            job,
            "model",
            {"kind": "codex_cli"},
            SimpleNamespace(scope="s" * 64, index=1),
        )
        self.assertIsNone(second)
        self.assertEqual(worker.assignments.count(), 1)

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
            metrics={"disk_free": 3 * 1024**3},
        )

    def submit(self, worker):
        self.job(1)
        job = claim()
        self.addCleanup(job.execution_lock.close)
        self.addCleanup(job.claim_owner.execution_lock.close)
        return dispatch.submit(
            job,
            "model",
            {"kind": "codex_cli", "context": {}},
            SimpleNamespace(scope="s" * 64, index=0),
        )

    def test_completion_is_idempotent_and_conflicting_repeat_rejected(self):
        worker = self.worker()
        assignment = self.submit(worker)
        leased = dispatch.take(worker, worker.incarnation)
        self.assertEqual(leased["id"], str(assignment.pk))
        keys = {
            "id": str(assignment.pk),
            "token": str(assignment.token),
            "incarnation": worker.incarnation,
        }
        dispatch.append_output(
            worker, {**keys, "sequence": 0, "data": base64.b64encode(b"hello").decode()}
        )
        result = {"exit_code": 0, "oom": False}
        self.assertTrue(
            dispatch.complete(worker, {**keys, "result": result, "cleaned": True})["accepted"]
        )
        self.assertTrue(
            dispatch.complete(worker, {**keys, "result": result, "cleaned": True})["accepted"]
        )
        with self.assertRaises(dispatch.AssignmentConflict):
            dispatch.complete(
                worker, {**keys, "result": {"exit_code": 1, "oom": False}, "cleaned": True}
            )

    def test_expiry_quarantines_capacity_until_cleanup_not_new_lease(self):
        worker = self.worker()
        assignment = self.submit(worker)
        dispatch.take(worker, worker.incarnation)
        type(assignment).objects.filter(pk=assignment.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        dispatch.reconcile()
        assignment.refresh_from_db()
        self.assertEqual(assignment.state, "quarantined")
        self.assertTrue(dispatch.slot_blocked("s" * 64, 0))
        keys = {
            "id": str(assignment.pk),
            "token": str(assignment.token),
            "incarnation": worker.incarnation,
        }
        self.assertFalse(
            dispatch.complete(
                worker, {**keys, "result": {"exit_code": 1, "oom": False}, "cleaned": True}
            )["accepted"]
        )
        self.assertFalse(dispatch.slot_blocked("s" * 64, 0))

    def test_stale_incarnation_cannot_fetch_or_finish(self):
        worker = self.worker()
        assignment = self.submit(worker)
        keys = {"id": str(assignment.pk), "token": str(assignment.token), "incarnation": "b" * 32}
        with self.assertRaises(dispatch.AssignmentConflict):
            dispatch.input_payload(worker, keys)

    def test_output_retry_is_deduplicated_and_conflict_fenced(self):
        worker = self.worker()
        assignment = self.submit(worker)
        dispatch.take(worker, worker.incarnation)
        keys = {
            "id": str(assignment.pk),
            "token": str(assignment.token),
            "incarnation": worker.incarnation,
        }
        event = {**keys, "sequence": 0, "data": base64.b64encode(b"hello").decode()}
        dispatch.append_output(worker, event)
        dispatch.append_output(worker, event)
        assignment.refresh_from_db()
        self.assertEqual(assignment.output_bytes, 5)
        with self.assertRaises(dispatch.AssignmentConflict):
            dispatch.append_output(worker, {**event, "data": base64.b64encode(b"changed").decode()})

    def test_disk_metric_required_and_reported_slot_limit_honored(self):
        worker = self.worker()
        worker.metrics = {}
        self.assertFalse(dispatch._eligible(worker, "model", {"kind": "codex_cli"}))
        worker.metrics = {"disk_free": 3 * 1024**3}
        worker.max_slots = 4
        worker.save()
        self.assertIsNotNone(self.submit(worker))
        self.job(2)
        job = claim()
        self.addCleanup(job.execution_lock.close)
        self.addCleanup(job.claim_owner.execution_lock.close)
        self.assertIsNone(
            dispatch.submit(
                job, "model", {"kind": "codex_cli"}, SimpleNamespace(scope="s" * 64, index=1)
            )
        )

    def test_delayed_old_heartbeat_never_revives_quarantined_assignment(self):
        from core.worker_registry import heartbeat

        worker = self.worker()
        assignment = self.submit(worker)
        dispatch.take(worker, worker.incarnation)
        keys = {
            "id": str(assignment.pk),
            "token": str(assignment.token),
            "incarnation": worker.incarnation,
        }
        report = {
            "version": 1,
            "slots": 1,
            "active": 0,
            "capabilities": ["model"],
            "authenticated": True,
        }
        heartbeat(worker, {**report, "incarnation": "b" * 32})
        heartbeat(worker, {**report, "incarnation": "a" * 32})
        assignment.refresh_from_db()
        self.assertEqual(assignment.state, "quarantined")
        self.assertFalse(assignment.cleanup_confirmed)
        self.assertFalse(dispatch.renew(worker, keys)["continue"])
        self.assertFalse(
            dispatch.complete(
                worker, {**keys, "cleaned": True, "result": {"exit_code": 0, "oom": False}}
            )["accepted"]
        )

    def test_revocation_after_authentication_fences_results(self):
        from core.worker_registry import revoke

        worker = self.worker()
        assignment = self.submit(worker)
        dispatch.take(worker, worker.incarnation)
        keys = {
            "id": str(assignment.pk),
            "token": str(assignment.token),
            "incarnation": worker.incarnation,
        }
        revoke(worker.pk)  # worker argument deliberately retains pre-revocation values.
        self.assertFalse(dispatch.renew(worker, keys)["continue"])
        self.assertFalse(
            dispatch.complete(
                worker, {**keys, "cleaned": True, "result": {"exit_code": 0, "oom": False}}
            )["accepted"]
        )

    def test_disabling_remote_does_not_release_uncertain_claim_or_account(self):
        from core.capacity import CapacityBusy, CapacityError, _permit, recover_resources
        from core.claims import release
        from core.models import JobClaim

        worker = self.worker()
        assignment = self.submit(worker)
        job = assignment.job
        retained = JobClaim.objects.get(job=job)
        job.claim_token = retained.token
        job.claim_owner_id = retained.owner_id
        with override_settings(REMOTE_EXECUTION_ENABLED=False):
            with self.assertRaises(CapacityBusy):
                with _permit(assignment.scope, [assignment.slot_index], lambda: False, wait=False):
                    self.fail("Uncertain remote capacity reused")
            with self.assertRaises(CapacityError):
                release(job)
            with self.assertRaises(CapacityError):
                recover_resources(retained)
        self.assertTrue(JobClaim.objects.filter(pk=retained.pk).exists())

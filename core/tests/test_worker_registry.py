from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from core import worker_registry as registry


class WorkerRegistryTests(TestCase):
    def test_pairing_is_one_use_and_token_is_not_stored_plaintext(self):
        code = registry.create_enrollment()
        worker, token = registry.enroll(code, "Extra worker")
        self.assertEqual(registry.authenticate(token).pk, worker.pk)
        self.assertNotEqual(worker.credential_digest, token)
        with self.assertRaises(registry.WorkerAuthError):
            registry.enroll(code, "Replay")

    def test_expired_enrollment_is_rejected(self):
        from core.models import WorkerEnrollment

        code = registry.create_enrollment()
        WorkerEnrollment.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(registry.WorkerAuthError):
            registry.enroll(code, "Expired")

    def test_revocation_invalidates_authentication(self):
        worker, token = registry.enroll(registry.create_enrollment(), "Extra")
        registry.revoke(worker.pk)
        with self.assertRaises(registry.WorkerAuthError):
            registry.authenticate(token)

    def test_new_worker_is_not_ready_before_valid_heartbeat(self):
        worker, token = registry.enroll(registry.create_enrollment(), "Extra")
        self.assertEqual(registry.availability(worker), "connecting")
        self.assertFalse(registry.ready(worker))

    def test_heartbeat_checks_protocol_capacity_and_freshness(self):
        worker, token = registry.enroll(registry.create_enrollment(), "Extra")
        registry.heartbeat(
            worker,
            {
                "version": 1,
                "incarnation": "a" * 32,
                "slots": 1,
                "active": 0,
                "capabilities": ["model"],
                "authenticated": True,
            },
        )
        worker.refresh_from_db()
        self.assertTrue(registry.ready(worker))
        worker.heartbeat_at = timezone.now() - timedelta(seconds=61)
        self.assertEqual(registry.availability(worker), "offline")
        self.assertFalse(registry.ready(worker))

    def test_invalid_heartbeat_does_not_mark_worker_alive(self):
        worker, token = registry.enroll(registry.create_enrollment(), "Extra")
        for report in ({}, {"version": True}, {"version": 1, "slots": 999}):
            with self.subTest(report=report), self.assertRaises(ValueError):
                registry.heartbeat(worker, report)
        worker.refresh_from_db()
        self.assertIsNone(worker.heartbeat_at)


class WorkerEnrollmentRaceTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_only_one_concurrent_redemption_succeeds(self):
        code = registry.create_enrollment()
        barrier = Barrier(2)

        def redeem():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                try:
                    registry.enroll(code, "Concurrent")
                    return True
                except registry.WorkerAuthError:
                    return False
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: redeem(), range(2)))
        self.assertEqual(sorted(results), [False, True])

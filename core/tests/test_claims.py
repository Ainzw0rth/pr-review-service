from copy import copy
from datetime import timedelta
from tempfile import TemporaryDirectory

from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from core.models import Credential, Job, Organization, PullRequest, Repository
from core.workflow import claim, current, finish


class ClaimTests(TestCase):
    def setUp(self):
        from pathlib import Path

        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        override = override_settings(DATA_DIR=Path(self.directory.name))
        override.enable()
        self.addCleanup(override.disable)
        org = Organization.objects.create(name="Team", slug="team")
        credential = Credential.objects.create(name="test", encrypted_value="x")
        self.repo = Repository.objects.create(
            organization=org,
            full_name="org/repo",
            reviewer="me",
            github_credential=credential,
            webhook_credential=credential,
        )

    def job(self, number):
        pr = PullRequest.objects.create(
            repository=self.repo,
            number=number,
            head_sha="a" * 40,
            generation=1,
            requested=True,
            is_open=True,
        )
        return Job.objects.create(pr=pr, generation=1, head_sha=pr.head_sha)

    def test_cap_retained_through_finish_and_lowering(self):
        from core.claims import release
        from core.models import ServiceSettings

        for number in range(1, 4):
            self.job(number)
        first, second = claim(), claim()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(claim())
        finish(first, "completed")
        self.assertIsNone(claim())
        ServiceSettings.objects.filter(pk=1).update(concurrent_pr_reviews=1)
        release(first)
        self.assertIsNone(claim())
        release(second)
        self.assertIsNotNone(claim())

    def test_same_pr_waits_for_cleanup(self):
        from core.claims import release

        job = self.job(1)
        first = claim()
        Job.objects.filter(pk=job.pk).update(status="cancelled")
        job.pr.generation = 2
        job.pr.save()
        Job.objects.create(pr=job.pr, generation=2, head_sha=job.head_sha)
        self.assertIsNone(claim())
        release(first)
        self.assertEqual(claim().generation, 2)

    def test_stale_token_cannot_finish(self):
        self.job(1)
        first = claim()
        stale = copy(first)
        stale.claim_token = "00000000-0000-0000-0000-000000000000"
        self.assertFalse(current(stale))
        self.assertFalse(finish(stale, "completed"))
        self.assertTrue(current(first))

    def test_live_expired_execution_not_recovered(self):
        from core.claims import recover_dead_owners
        from core.models import JobClaim

        self.job(1)
        first = claim()
        JobClaim.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        recover_dead_owners()
        self.assertTrue(JobClaim.objects.filter(job=first).exists())
        first.refresh_from_db()
        self.assertEqual(first.status, "running")

    def test_stale_callbacks_cannot_write_after_same_job_retry(self):
        from core.activity import record
        from core.claims import attempt_rows, release
        from core.delivery import save_review, update_cursor
        from core.models import ModelAttempt
        from core.publication import StaleReview

        self.job(1)
        old = claim()
        attempt = ModelAttempt.objects.create(job=old, provider_name="test", model="test")
        finish(old, "attention")
        release(old)
        Job.objects.filter(pk=old.pk).update(status="queued")
        replacement = claim()
        self.assertNotEqual(old.claim_token, replacement.claim_token)
        self.assertEqual(attempt_rows(old, pk=attempt.pk).update(diagnostics={"stale": True}), 0)
        record(old, "starting")
        self.assertFalse(old.activity.exists())
        with self.assertRaises(StaleReview):
            update_cursor(old, "done")
        with self.assertRaises(StaleReview):
            save_review(old, {}, {}, {})
        self.assertFalse(finish(old, "completed"))
        self.assertTrue(current(replacement))

    def test_abandoned_publication_needs_attention(self):
        from core.claims import recover_dead_owners
        from core.models import JobClaim, ReviewCheckpoint

        self.job(1)
        old = claim()
        ReviewCheckpoint.objects.create(job=old, stage="review", encrypted_payload="private")
        JobClaim.objects.filter(job=old).update(expires_at=timezone.now() - timedelta(seconds=1))
        old.execution_lock.close()
        recovered = []
        recover_dead_owners(cleanup=lambda row: recovered.append(row.token))
        old.refresh_from_db()
        self.assertEqual(old.status, "attention")
        self.assertEqual(recovered, [old.claim_token])
        self.assertFalse(JobClaim.objects.exists())
        self.assertIsNone(claim())

    def test_expired_heartbeat_never_revives_claim(self):
        from core.claims import heartbeat
        from core.models import JobClaim

        self.job(1)
        old = claim()
        JobClaim.objects.filter(job=old).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertFalse(heartbeat(old))
        self.assertFalse(current(old))

    def test_settings_reject_coercible_nonintegers_and_bounds(self):
        from django.core.exceptions import ValidationError

        from core.models import ServiceSettings

        for value in (0, 9, True, False, 1.5, "2"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ServiceSettings(concurrent_pr_reviews=value).full_clean()

    def test_dead_probe_owner_only_recovered_after_lifetime_lock_released(self):
        from core.claims import recover_owners, register_owner
        from core.models import WorkerOwner

        owner = register_owner()
        WorkerOwner.objects.filter(pk=owner.pk).update(
            heartbeat_at=timezone.now() - timedelta(seconds=100), probe_active=True
        )
        cleanup = []
        recover_owners(cleanup=lambda owner: cleanup.append(owner.pk))
        self.assertEqual(cleanup, [])
        owner.execution_lock.close()
        recover_owners(cleanup=lambda owner: cleanup.append(owner.pk))
        owner.refresh_from_db()
        self.assertEqual(cleanup, [owner.pk])
        self.assertTrue(owner.retired)
        self.assertFalse(owner.probe_active)


class ClaimRaceTests(TransactionTestCase):
    setUp = ClaimTests.setUp
    job = ClaimTests.job

    @skipUnlessDBFeature("has_select_for_update_skip_locked")
    def test_racing_supervisors_obey_global_cap(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        from django.db import connections

        from core.claims import register_owner, release
        from core.models import JobClaim

        for number in range(1, 7):
            self.job(number)
        barrier = Barrier(4)

        def acquire():
            try:
                owner = register_owner()
                barrier.wait(timeout=10)
                return claim(owner)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: acquire(), range(4)))
        occupied = [job for job in results if job]
        self.assertEqual(len(occupied), 2)
        self.assertEqual(len({job.pr_id for job in occupied}), 2)
        self.assertEqual(JobClaim.objects.count(), 2)
        for job in occupied:
            release(job)

    @skipUnlessDBFeature("has_select_for_update_skip_locked")
    def test_two_executions_overlap_and_keep_slots_through_terminal_finally(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, Event
        from unittest.mock import patch

        from core.claims import register_owner, release
        from core.management.commands.review_worker import execute
        from core.models import JobClaim

        for number in range(1, 4):
            self.job(number)
        owner = register_owner()
        first, second = claim(owner), claim(owner)
        entered = Barrier(3)
        cleanup_done = Event()

        def blocked_finally(job):
            finish(job, "completed")
            entered.wait(timeout=10)
            self.assertTrue(cleanup_done.wait(timeout=10))

        with (
            patch("core.worker._run_job", side_effect=blocked_finally),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [executor.submit(execute, job) for job in (first, second)]
            try:
                entered.wait(timeout=10)
                self.assertEqual(JobClaim.objects.count(), 2)
                self.assertIsNone(claim(owner))
            finally:
                cleanup_done.set()
            for future in futures:
                future.result(timeout=10)
        third = claim(owner)
        self.assertIsNotNone(third)
        release(third)

    @skipUnlessDBFeature("has_select_for_update_skip_locked")
    def test_cleanup_exception_quarantines_slot_but_releases_execution_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        from core.claims import execution_lock, register_owner
        from core.management.commands.review_worker import execute
        from core.models import JobClaim

        self.job(1)
        job = claim(register_owner())
        with (
            patch("core.worker._run_job", side_effect=RuntimeError("cleanup unknown")),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(execute, job)
            with self.assertRaises(RuntimeError):
                future.result(timeout=10)
        self.assertTrue(JobClaim.objects.filter(job=job).exists())
        lock = execution_lock(job.claim_token)
        self.assertIsNotNone(lock)
        lock.close()
        job.refresh_from_db()
        self.assertEqual(job.status, "attention")

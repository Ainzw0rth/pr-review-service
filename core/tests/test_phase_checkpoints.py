import copy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from core.claims import claim, release
from core.models import Job, JobClaim, PhaseCheckpoint
from core.phase_checkpoints import PhaseCheckpoints
from core.publication import StaleReview
from core.tests import test_claims
from core.tests.test_review_team import report
from core.tests.test_review_v2 import context, intent


class PhaseCheckpointTests(TestCase):
    job = test_claims.ClaimTests.job

    def setUp(self):
        test_claims.ClaimTests.setUp(self)
        settings = override_settings(CREDENTIAL_KEY=Fernet.generate_key().decode())
        settings.enable()
        self.addCleanup(settings.disable)
        self.job(1)
        self.active = claim()
        self.addCleanup(self.active.execution_lock.close)
        self.evidence = context()
        self.provider = SimpleNamespace(pk=1, model="model-a", kind="openai")
        self.cache = PhaseCheckpoints(self.active, self.evidence)

    def test_restart_and_explicit_retry_reuse_but_unrelated_cycle_does_not(self):
        self.cache.save("intent", intent(), self.provider)
        self.cache.save("code", report(), self.provider, intent())
        self.assertEqual(
            PhaseCheckpoints(self.active, self.evidence).load("code", intent()), report()
        )
        Job.objects.filter(pk=self.active.pk).update(status="attention")
        release(self.active)
        self.active.pr.generation += 1
        self.active.pr.save()
        retry = Job.objects.create(
            pr=self.active.pr, generation=2, head_sha=self.active.head_sha, retry_of=self.active
        )
        self.assertEqual(PhaseCheckpoints(retry, self.evidence).load("intent"), intent())
        retry.retry_of_id = None
        self.assertIsNone(PhaseCheckpoints(retry, self.evidence).load("intent"))

    def test_changed_evidence_configuration_contract_or_intent_prevents_reuse(self):
        self.cache.save("intent", intent(), self.provider)
        self.cache.save("code", report(), self.provider, intent())
        changed = copy.deepcopy(self.evidence)
        changed["pr"]["body"] = "New requirements"
        self.assertIsNone(PhaseCheckpoints(self.active, changed).load("intent"))
        for field, value in [
            ("head_sha", "b" * 40),
            ("provider_plan", [{"model": "changed"}]),
            ("workflow_steps", [{"changed": True}]),
            ("workflow_version", "changed"),
            (
                "review_agents",
                [
                    *self.active.review_agents[:2],
                    {**self.active.review_agents[2], "enabled": False},
                ],
            ),
        ]:
            with self.subTest(field=field):
                job = copy.copy(self.active)
                setattr(job, field, value)
                self.assertIsNone(PhaseCheckpoints(job, self.evidence).load("intent"))
        changed_intent = intent()
        changed_intent["goal"]["text"] = "Different interpretation"
        self.assertIsNone(self.cache.load("code", changed_intent))
        with patch("core.review_v2.INTENT_SYSTEM", "Changed policy"):
            self.assertIsNone(PhaseCheckpoints(self.active, self.evidence).load("intent"))

    def test_incomplete_corrupt_and_expired_results_are_not_reused(self):
        self.cache.save("code", report(complete=False), self.provider, intent())
        self.assertFalse(PhaseCheckpoint.objects.exists())
        self.cache.save("intent", intent(), self.provider)
        PhaseCheckpoint.objects.update(encrypted_payload="corrupt")
        self.assertIsNone(self.cache.load("intent"))
        self.cache.save("intent", intent(), self.provider)
        PhaseCheckpoint.objects.update(created_at=timezone.now() - timedelta(days=91))
        self.assertIsNone(self.cache.load("intent"))
        self.cache.save("intent", intent(), self.provider)
        self.assertEqual(self.cache.load("intent"), intent())
        PhaseCheckpoint.objects.update(created_at=timezone.now() - timedelta(days=91))
        Job.objects.filter(pk=self.active.pk).update(status="attention")
        release(self.active)
        call_command("cleanup", verbosity=0)
        self.assertFalse(PhaseCheckpoint.objects.exists())

    def test_stale_owner_cannot_save(self):
        Job.objects.filter(pk=self.active.pk).update(status="cancelled")
        with self.assertRaises(StaleReview):
            self.cache.save("intent", intent(), self.provider)
        self.assertFalse(PhaseCheckpoint.objects.exists())

    def test_expired_claim_cannot_checkpoint(self):
        JobClaim.objects.filter(job=self.active).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with self.assertRaises(StaleReview):
            self.cache.save("intent", intent(), self.provider)
        self.assertFalse(PhaseCheckpoint.objects.exists())

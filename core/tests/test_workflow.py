from django.test import TestCase
from django.utils import timezone

from core import workflow
from core.models import Credential, Job, Organization, Repository, Webhook


class WorkflowTests(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Team", slug="team")
        c = Credential.objects.create(name="test", encrypted_value="x")
        self.repo = Repository.objects.create(
            organization=org,
            full_name="org/repo",
            reviewer="louis",
            github_credential=c,
            webhook_credential=c,
        )

    def snapshot(self, sha="a" * 40, labels=None, requested=True, state="open"):
        return {
            "number": 1,
            "head": {"sha": sha},
            "state": state,
            "labels": [{"name": x} for x in (["on review"] if labels is None else labels)],
            "requested_reviewers": [{"login": "louis"}] if requested else [],
        }

    def event(self, action, snapshot=None, label="on review"):
        payload = {
            "action": action,
            "number": 1,
            "pull_request": snapshot or self.snapshot(),
            "label": {"name": label},
            "requested_reviewer": {"login": "louis"},
            "sender": {"login": "dev", "type": "User"},
        }
        return Webhook.objects.create(
            delivery=str(Webhook.objects.count()),
            repository=self.repo,
            event="pull_request",
            payload=payload,
        )

    def reconcile(self, event, snapshot, canonical=None):
        from core.models import PullRequest

        pr = PullRequest.objects.filter(repository=self.repo, number=1).first()
        prior = pr.canonical_trigger if pr else ""
        trigger = (
            event.payload.get("action") in ("labeled", "review_requested")
            and event.payload.get("label", {}).get("name") == "on review"
        )
        canonical = canonical or {
            "key": str(event.pk * 10) if trigger else prior,
            "eligible": workflow.eligible(self.repo, snapshot),
            "actor": "dev",
        }
        return workflow.reconcile(event, snapshot, canonical=canonical)

    def test_trigger_requires_both_conditions(self):
        self.reconcile(self.event("labeled"), self.snapshot(requested=False))
        self.assertFalse(Job.objects.exists())
        self.reconcile(self.event("review_requested"), self.snapshot())
        self.assertEqual(Job.objects.count(), 1)

    def test_delivery_processed_twice_does_not_duplicate(self):
        e = self.event("labeled")
        self.reconcile(e, self.snapshot())
        self.reconcile(e, self.snapshot())
        self.assertEqual(Job.objects.count(), 1)

    def test_new_commit_supersedes_without_starting(self):
        self.reconcile(self.event("labeled"), self.snapshot())
        self.reconcile(
            self.event("synchronize", self.snapshot(sha="b" * 40)), self.snapshot(sha="b" * 40)
        )
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(Job.objects.get().status, "superseded")

    def test_same_commit_retoggle_new_attempt(self):
        self.reconcile(self.event("labeled"), self.snapshot())
        self.reconcile(self.event("unlabeled"), self.snapshot(labels=["feedback"]))
        self.reconcile(self.event("labeled"), self.snapshot())
        self.assertEqual(
            list(Job.objects.values_list("status", flat=True)), ["cancelled", "queued"]
        )

    def test_removed_reviewer_cancels(self):
        self.reconcile(self.event("labeled"), self.snapshot())
        self.reconcile(self.event("review_request_removed"), self.snapshot(requested=False))
        self.assertEqual(Job.objects.get().status, "cancelled")

    def test_stale_trigger_payload_cannot_start_latest_commit(self):
        self.reconcile(self.event("labeled"), self.snapshot(sha="b" * 40))
        self.assertFalse(Job.objects.exists())

    def test_quota_releases_worker_and_keeps_provider(self):
        self.reconcile(self.event("labeled"), self.snapshot())
        first = workflow.claim()
        self.assertEqual(first.status, "running")
        self.assertIsNone(workflow.claim())
        workflow.finish(
            first,
            "quota",
            "Quota exhausted",
            retry_at=timezone.now() + timezone.timedelta(minutes=30),
        )
        self.assertIsNone(workflow.claim())

    def test_bot_label_cannot_start_review(self):
        e = self.event("labeled")
        e.payload["sender"]["type"] = "Bot"
        e.save()
        self.reconcile(e, self.snapshot())
        self.assertFalse(Job.objects.exists())

    def timeline(self, identifier, action, label="on review", actor="dev", kind="User"):
        return {
            "id": identifier,
            "event": action,
            "label": {"name": label},
            "actor": {"login": actor, "type": kind},
            "requested_reviewer": {"login": "louis"},
        }

    def test_both_initial_deliveries_use_one_canonical_entry(self):
        timeline = [self.timeline(1, "labeled"), self.timeline(2, "review_requested")]
        canonical = workflow.canonical_cycle(self.repo, timeline, self.snapshot())
        self.reconcile(
            self.event("labeled", self.snapshot(requested=False)), self.snapshot(), canonical
        )
        self.reconcile(self.event("review_requested"), self.snapshot(), canonical)
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(Job.objects.get().status, "queued")

    def test_delayed_old_removal_cannot_cancel_new_cycle(self):
        timeline = [self.timeline(1, "review_requested"), self.timeline(2, "labeled")]
        self.reconcile(
            self.event("labeled"),
            self.snapshot(),
            workflow.canonical_cycle(self.repo, timeline, self.snapshot()),
        )
        old_removal = self.event("unlabeled", self.snapshot(labels=[]))
        timeline += [self.timeline(3, "unlabeled"), self.timeline(4, "labeled")]
        canonical = workflow.canonical_cycle(self.repo, timeline, self.snapshot())
        self.reconcile(self.event("labeled"), self.snapshot(), canonical)
        self.reconcile(old_removal, self.snapshot(), canonical)
        self.assertEqual(Job.objects.latest("generation").status, "queued")
        self.assertEqual(Job.objects.count(), 2)

    def test_late_old_trigger_does_not_replace_current_cycle(self):
        timeline = [
            self.timeline(1, "review_requested"),
            self.timeline(2, "labeled"),
            self.timeline(3, "unlabeled"),
            self.timeline(4, "labeled"),
        ]
        canonical = workflow.canonical_cycle(self.repo, timeline, self.snapshot())
        old_trigger = self.event("labeled")
        self.reconcile(self.event("labeled"), self.snapshot(), canonical)
        self.reconcile(old_trigger, self.snapshot(), canonical)
        self.assertEqual(Job.objects.count(), 1)

    def test_human_feedback_wins_over_conflicting_on_review(self):
        snapshot = self.snapshot(labels=["on review", "feedback"])
        timeline = [
            self.timeline(1, "review_requested"),
            self.timeline(2, "labeled"),
            self.timeline(3, "labeled", "feedback"),
        ]
        canonical = workflow.canonical_cycle(self.repo, timeline, snapshot)
        self.assertFalse(canonical["eligible"])
        self.reconcile(self.event("labeled"), snapshot, canonical)
        self.assertFalse(Job.objects.exists())

    def test_timeline_snapshot_disagreement_retries(self):
        with self.assertRaises(ValueError):
            workflow.canonical_cycle(self.repo, [self.timeline(1, "labeled")], self.snapshot())

    def test_synchronize_consumes_cycle_without_late_enrollment(self):
        canonical = {"key": "12", "eligible": True, "actor": "dev"}
        self.reconcile(
            self.event("synchronize", self.snapshot(sha="b" * 40)),
            self.snapshot(sha="b" * 40),
            canonical,
        )
        self.reconcile(
            self.event("labeled", self.snapshot(sha="b" * 40)),
            self.snapshot(sha="b" * 40),
            canonical,
        )
        self.assertFalse(Job.objects.exists())

    def test_production_reconcile_requires_canonical_timeline(self):
        with self.assertRaises((TypeError, ValueError)):
            workflow.reconcile(self.event("labeled"), self.snapshot())

    def test_old_nontrigger_cannot_consume_new_unprocessed_human_entry(self):
        canonical = {"key": "12", "eligible": True, "actor": "dev"}
        self.reconcile(self.event("unlabeled"), self.snapshot(), canonical)
        self.reconcile(self.event("labeled"), self.snapshot(), canonical)
        self.assertEqual(Job.objects.count(), 1)

    def test_implicit_review_removal_is_ineligible_not_permanent_retry(self):
        timeline = [self.timeline(1, "review_requested"), self.timeline(2, "labeled")]
        state = workflow.canonical_cycle(self.repo, timeline, self.snapshot(requested=False))
        self.assertFalse(state["eligible"])

    def test_bot_reentry_cannot_reuse_old_human_trigger(self):
        timeline = [
            self.timeline(1, "review_requested"),
            self.timeline(2, "labeled"),
            self.timeline(3, "unlabeled"),
            self.timeline(4, "labeled", kind="Bot"),
        ]
        state = workflow.canonical_cycle(self.repo, timeline, self.snapshot())
        self.assertFalse(state["eligible"])

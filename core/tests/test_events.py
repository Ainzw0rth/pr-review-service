from unittest.mock import patch

from core.events import process_event
from core.models import Job
from core.tests.test_workflow import WorkflowTests


class EventTests(WorkflowTests):
    def test_failed_secondary_sync_keeps_event_retryable(self):
        snapshot = self.snapshot(state="closed", requested=False, labels=[])
        event = self.event("closed", snapshot)
        timeline = [self.timeline(1, "closed")]
        with (
            patch("core.github.GitHub") as client,
            patch("core.sheets.sync_status", side_effect=RuntimeError("private upstream text")),
        ):
            client.return_value.pages.return_value = timeline
            client.return_value.pull.return_value = snapshot
            with self.assertRaises(RuntimeError):
                process_event(event)
        event.refresh_from_db()
        self.assertIsNone(event.processed_at)
        self.assertEqual(event.payload["action"], "closed")
        self.assertNotIn("private", event.error)

    def test_initial_webhook_pair_uses_trusted_timeline(self):
        first = self.event("labeled", self.snapshot(requested=False))
        second = self.event("review_requested")
        timeline = [self.timeline(1, "labeled"), self.timeline(2, "review_requested")]
        with patch("core.github.GitHub") as client:
            client.return_value.pages.return_value = timeline
            client.return_value.pull.return_value = self.snapshot()
            process_event(first)
            process_event(second)
        self.assertEqual(Job.objects.count(), 1)

    def test_starting_supervisor_never_bulk_cleans_live_resources(self):
        from core.management.commands import review_worker

        with (
            patch("core.runner.cleanup_orphans") as cleanup,
            patch("core.cli.cleanup_cli_orphans") as cli_cleanup,
            patch.object(review_worker, "claim", return_value=None),
        ):
            review_worker.Command().handle(once=True, allow_sqlite=True)
        cleanup.assert_not_called()
        cli_cleanup.assert_not_called()

    def test_batch_shares_reads_and_defers_inconsistent_history(self):
        from core.events import process_pending

        first = self.event("labeled")
        second = self.event("review_requested")
        with patch("core.github.GitHub") as client:
            client.return_value.pages.return_value = [
                self.timeline(1, "labeled"),
                self.timeline(2, "review_requested"),
            ]
            client.return_value.pull.return_value = self.snapshot()
            process_pending()
            self.assertEqual(client.return_value.pull.call_count, 1)
            self.assertEqual(client.return_value.pages.call_count, 1)
        self.assertEqual(Job.objects.count(), 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNotNone(first.processed_at)
        self.assertIsNotNone(second.processed_at)
        pending = self.event("labeled")
        with patch("core.github.GitHub") as client:
            client.return_value.pages.return_value = []
            client.return_value.pull.return_value = self.snapshot()
            process_pending()
            process_pending()
            self.assertEqual(client.return_value.pull.call_count, 1)
        pending.refresh_from_db()
        self.assertIsNone(pending.processed_at)
        self.assertIn("history", pending.error)
        self.assertIsNotNone(pending.retry_at)

    def test_event_arriving_during_batch_stays_pending(self):
        from core.events import process_pending

        self.event("labeled")
        arrivals = []

        def snapshot(number):
            arrivals.append(self.event("review_requested"))
            return self.snapshot()

        with patch("core.github.GitHub") as client:
            client.return_value.pages.return_value = [
                self.timeline(1, "review_requested"),
                self.timeline(2, "labeled"),
            ]
            client.return_value.pull.side_effect = snapshot
            process_pending()
        arrivals[0].refresh_from_db()
        self.assertIsNone(arrivals[0].processed_at)

    def test_new_webhook_does_not_bypass_pr_backoff(self):
        from datetime import timedelta

        from django.utils import timezone

        from core.events import process_pending

        old = self.event("labeled")
        old.retry_at = timezone.now() + timedelta(minutes=5)
        old.save()
        self.event("review_requested")
        with patch("core.github.GitHub") as client:
            process_pending()
            client.assert_not_called()

    def test_alternate_history_retains_canonical_identity(self):
        from core.events import process_pending

        self.event("labeled")
        with patch("core.github.GitHub") as client:
            client.return_value.pages.side_effect = [
                [],
                [self.timeline(1, "review_requested"), self.timeline(2, "labeled")],
            ]
            client.return_value.pull.return_value = self.snapshot()
            process_pending()
        self.assertEqual(Job.objects.get().pr.canonical_trigger, "2")

    def test_fallback_created_job_passes_publication_guard(self):
        from core.events import process_pending
        from core.publication import guard
        from core.workflow import claim

        self.event("labeled")
        history = [self.timeline(1, "review_requested"), self.timeline(2, "labeled")]
        with patch("core.github.GitHub") as client:
            client.return_value.pages.side_effect = lambda path: (
                history if path.endswith("/events") else []
            )
            client.return_value.pull.return_value = self.snapshot()
            process_pending()
            job = claim()
            guard(client.return_value, job)

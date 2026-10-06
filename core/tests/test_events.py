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

    def test_worker_orphan_cleanup_precedes_recovery(self):
        from core.management.commands import review_worker

        self.repo.test_command = ["true"]
        self.repo.save(update_fields=["test_command"])
        calls = []
        with (
            patch.object(
                review_worker, "cleanup_orphans", side_effect=lambda: calls.append("cleanup")
            ),
            patch.object(
                review_worker, "recover_interrupted", side_effect=lambda: calls.append("recover")
            ),
            patch.object(review_worker, "claim", return_value=None),
        ):
            review_worker.Command().handle(once=True)
        self.assertEqual(calls, ["cleanup", "recover"])

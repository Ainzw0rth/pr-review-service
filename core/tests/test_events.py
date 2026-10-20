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
            review_worker.Command().handle(once=True)
        cleanup.assert_not_called()
        cli_cleanup.assert_not_called()

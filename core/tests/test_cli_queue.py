import multiprocessing
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from django.test import override_settings

from core.capacity import CapacityBusy, CapacityError, digest, permit
from core.providers import ReviewCancelled


def queued_process(directory, priority, name, waiting, result, crash=False, start=None):
    import os

    from core.cli_queue import admit_cli

    if start is not None:
        start.wait(5)
    with (
        override_settings(DATA_DIR=Path(directory)),
        patch("core.cli_queue.cli_limit", return_value=1),
    ):

        def on_wait(state):
            if state:
                waiting.set()
                if crash:
                    os._exit(0)

        with admit_cli("shared", lambda: False, priority=priority, on_wait=on_wait):
            result.put(name)
            # Flush before releasing the resource: assert admission, not feeder scheduling.
            result.close()
            result.join_thread()


class CliQueueTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.override = override_settings(DATA_DIR=Path(self.directory.name))
        self.override.enable()
        self.addCleanup(self.override.disable)
        from core.cli_queue import admit_cli

        self.admit = admit_cli
        self.cap = [1]
        self.limit_patch = patch("core.cli_queue.cli_limit", side_effect=lambda: self.cap[0])
        self.limit_patch.start()
        self.addCleanup(self.limit_patch.stop)

    def tickets(self):
        return list(Path(self.directory.name).rglob("*.ticket"))

    def test_legacy_slots_scope_isolation_and_stable_inode(self):
        with permit("shared", 1, lambda: False) as slot:
            inode = slot.path.stat().st_ino
            with self.assertRaises(CapacityBusy), self.admit("shared", lambda: False, wait=False):
                pass
            with self.admit("other", lambda: False, wait=False):
                pass
        with self.admit("shared", lambda: False) as slot:
            self.assertEqual(slot.scope, digest("shared"))
            self.assertEqual(slot.path.stat().st_ino, inode)
            with self.assertRaises(CapacityBusy), permit("shared", 1, lambda: False, wait=False):
                pass
        self.assertFalse(self.tickets())

    def test_process_priority_then_fifo(self):
        ctx = multiprocessing.get_context("fork")
        result = ctx.Queue()
        processes = []
        starts, waiters = [], []
        for priority, name in [(1, "regular-first"), (1, "regular-second"), (0, "synthesis")]:
            start, waiting = ctx.Event(), ctx.Event()
            process = ctx.Process(
                target=queued_process,
                args=(self.directory.name, priority, name, waiting, result, False, start),
            )
            processes.append(process)
            starts.append(start)
            waiters.append(waiting)
            process.start()
        try:
            # Fork before holding a resource: fork inherits the parent's flock FD.
            with permit("shared", 1, lambda: False):
                for start, waiting in zip(starts, waiters):
                    start.set()
                    self.assertTrue(waiting.wait(5))
            self.assertEqual(
                [result.get(timeout=5) for _ in processes],
                ["synthesis", "regular-first", "regular-second"],
            )
            for process in processes:
                process.join(5)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(5)
        self.assertFalse(self.tickets())

    def test_cancellation_cleans_ticket_and_balances_wait_callback(self):
        cancel, waiting = threading.Event(), threading.Event()
        states, outcome = [], []

        def on_wait(state):
            states.append(state)
            if state:
                waiting.set()

        def work():
            try:
                with self.admit("shared", cancel.is_set, on_wait=on_wait):
                    outcome.append("admitted")
            except ReviewCancelled:
                outcome.append("cancelled")

        with permit("shared", 1, lambda: False):
            thread = threading.Thread(target=work)
            thread.start()
            self.assertTrue(waiting.wait(5))
            cancel.set()
            thread.join(5)
        self.assertEqual(outcome, ["cancelled"])
        self.assertEqual(states, [True, False])
        self.assertFalse(self.tickets())

    def test_crashed_waiter_does_not_block_next_admission(self):
        ctx = multiprocessing.get_context("fork")
        waiting, result = ctx.Event(), ctx.Queue()
        with permit("shared", 1, lambda: False):
            process = ctx.Process(
                target=queued_process,
                args=(self.directory.name, 0, "crashed", waiting, result, True),
            )
            process.start()
            self.assertTrue(waiting.wait(5))
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(self.tickets())
        with self.admit("shared", lambda: False, wait=False):
            pass
        self.assertFalse(self.tickets())

    def test_shrink_counts_high_indices_and_raise_admits_without_preemption(self):
        self.cap[0] = 6
        with ExitStack() as stack:
            sessions = [stack.enter_context(self.admit("shared", lambda: False)) for _ in range(6)]
            self.assertEqual([slot.index for slot in sessions], list(range(6)))
            self.cap[0] = 2
            with self.assertRaises(CapacityBusy), self.admit("shared", lambda: False, wait=False):
                pass
            self.cap[0] = 7
            with self.admit("shared", lambda: False, wait=False) as slot:
                self.assertEqual(slot.index, 6)
        # A high-index preexisting owner alone still consumes the shrunken cap.
        from core.capacity import _permit

        self.cap[0] = 1
        with _permit(digest("shared"), [31], lambda: False):
            with self.assertRaises(CapacityBusy), self.admit("shared", lambda: False, wait=False):
                pass
        self.assertFalse(self.tickets())

    def test_waiter_observes_live_limit_raise_and_callbacks(self):
        states, outcome = [], []
        waiting = threading.Event()

        def on_wait(state):
            states.append(state)
            if state:
                waiting.set()

        def work():
            with self.admit("shared", lambda: False, on_wait=on_wait):
                outcome.append("admitted")

        with self.admit("shared", lambda: False):
            thread = threading.Thread(target=work)
            thread.start()
            self.assertTrue(waiting.wait(5))
            self.cap[0] = 2
            thread.join(5)
            self.assertEqual(outcome, ["admitted"])
        self.assertEqual(states, [True, False])

    def test_cancel_before_admission_and_invalid_priority_leave_no_ticket(self):
        with self.assertRaises(ReviewCancelled), self.admit("shared", lambda: True):
            pass
        with self.assertRaises(CapacityError), self.admit("shared", lambda: False, priority=-1):
            pass
        self.assertFalse(self.tickets())

    def test_ticket_cleanup_failure_fails_closed(self):
        with patch.object(Path, "unlink", side_effect=OSError("unavailable")):
            with self.assertRaises(CapacityError), self.admit("shared", lambda: False):
                self.fail("cleanup was not confirmed before admission")
        with self.admit("shared", lambda: False, wait=False):
            pass
        self.assertFalse(self.tickets())

    def test_nowait_quota_cannot_jump_an_existing_waiter(self):
        waiting, release_callback = threading.Event(), threading.Event()
        outcome = []

        def on_wait(state):
            if state:
                waiting.set()
                release_callback.wait(5)

        def regular():
            with self.admit("shared", lambda: False, on_wait=on_wait):
                outcome.append("regular")

        with permit("shared", 1, lambda: False):
            thread = threading.Thread(target=regular)
            thread.start()
            self.assertTrue(waiting.wait(5))
        try:
            # Resource free, but the registered regular has not retried yet.
            with self.assertRaises(CapacityBusy):
                with self.admit("shared", lambda: False, priority=2, wait=False):
                    self.fail("quota jumped a waiting review")
            self.assertEqual(len(self.tickets()), 1)
        finally:
            release_callback.set()
            thread.join(5)
        self.assertEqual(outcome, ["regular"])
        self.assertFalse(self.tickets())

    def test_body_value_error_is_not_reclassified_as_capacity_failure(self):
        with self.assertRaisesRegex(ValueError, "provider body"):
            with self.admit("shared", lambda: False):
                raise ValueError("provider body")
        with self.admit("shared", lambda: False, wait=False):
            pass

    def test_nowait_does_not_block_on_queue_gate(self):
        from core.capacity import _permit

        finished = threading.Event()
        outcome = []

        def quota():
            try:
                with self.admit("shared", lambda: False, priority=2, wait=False):
                    outcome.append("admitted")
            except CapacityBusy:
                outcome.append("busy")
            finally:
                finished.set()

        with _permit(digest("cli-queue:shared"), [0], lambda: False):
            thread = threading.Thread(target=quota)
            thread.start()
            returned_while_gate_locked = finished.wait(0.3)
        thread.join(5)
        self.assertTrue(returned_while_gate_locked)
        self.assertEqual(outcome, ["busy"])
        self.assertFalse(self.tickets())

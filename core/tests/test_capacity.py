import multiprocessing
import tempfile
import threading
from pathlib import Path
from unittest import TestCase

from django.test import override_settings

from core.capacity import ActiveBudget, CapacityBusy, cli_scope, permit
from core.providers import ReviewCancelled


def contend(directory, ready, result):
    with override_settings(DATA_DIR=Path(directory)):
        ready.set()
        try:
            with permit("shared", 1, lambda: False, wait=False):
                result.put("acquired")
        except CapacityBusy:
            result.put("busy")


def hold_until_exit(directory, ready, stop):
    import os

    with override_settings(DATA_DIR=Path(directory)):
        with permit("crashed", 1, lambda: False):
            ready.set()
            stop.wait(3)
            os._exit(0)


class CapacityTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.override = override_settings(DATA_DIR=Path(self.directory.name))
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_process_contends_and_inode_is_stable(self):
        ctx = multiprocessing.get_context("fork")
        ready, result = ctx.Event(), ctx.Queue()
        with permit("shared", 1, lambda: False) as slot:
            inode = slot.path.stat().st_ino
            process = ctx.Process(target=contend, args=(self.directory.name, ready, result))
            process.start()
            self.assertTrue(ready.wait(3))
            self.assertEqual(result.get(timeout=3), "busy")
            process.join(3)
            self.assertEqual(process.exitcode, 0)
        with permit("shared", 1, lambda: False) as slot:
            self.assertEqual(slot.path.stat().st_ino, inode)

    def test_separate_thread_opens_and_cancellation_clear_wait(self):
        waiting, cancel = threading.Event(), threading.Event()
        states, errors = [], []

        def on_wait(state):
            states.append(state)
            if state:
                waiting.set()

        def work():
            try:
                with permit("shared", 1, cancel.is_set, on_wait=on_wait):
                    errors.append("unexpected admission")
            except ReviewCancelled:
                errors.append("cancelled")

        with permit("shared", 1, lambda: False):
            thread = threading.Thread(target=work)
            thread.start()
            self.assertTrue(waiting.wait(3))
            with permit("separate", 1, lambda: False, wait=False):
                pass
            cancel.set()
            thread.join(3)
        self.assertEqual(errors, ["cancelled"])
        self.assertEqual(states, [True, False])

    def test_limit_and_shared_volume_identity(self):
        with override_settings(CLI_CODEX_AUTH_VOLUME="same", CLI_CLAUDE_AUTH_VOLUME="same"):
            self.assertEqual(cli_scope("codex_cli"), cli_scope("claude_cli"))
        with permit("api", 2, lambda: False), permit("api", 2, lambda: False):
            with self.assertRaises(CapacityBusy), permit("api", 2, lambda: False, wait=False):
                pass

    def test_active_budget_excludes_queue_and_preserves_recovery_time(self):
        clock = [0]
        budget = ActiveBudget(10, clock=lambda: clock[0])
        clock[0] = 1900
        budget.check()
        with budget.active():
            clock[0] += 6
        clock[0] += 1900
        budget.check()
        from core.providers import ProviderError

        with self.assertRaises(ProviderError), budget.active():
            clock[0] += 5
            budget.check()

    def test_native_reconcile_removes_only_abandoned_exact_slot(self):
        from core.capacity import Container
        from core.tests.docker_fake import DockerFake

        docker = DockerFake()
        with permit("native", 1, lambda: False) as slot:
            old = Container(slot, docker)
            identifier = old.create(["create", "--name", slot.name, "image"])
            self.assertIn(identifier, docker.objects)
            with self.assertRaises(CapacityBusy), permit("native", 1, lambda: False, wait=False):
                pass
        with permit("other-deployment-resource", 1, lambda: False) as unrelated:
            other = Container(unrelated, docker)
            other_id = other.create(["create", "--name", unrelated.name, "image"])
        with permit("native", 1, lambda: False) as slot:
            with Container(slot, docker) as current:
                self.assertNotIn(identifier, docker.objects)
                replacement = current.create(["create", "--name", slot.name, "image"])
                self.assertNotEqual(identifier, replacement)
        self.assertEqual(set(docker.objects), {other_id})
        for command in docker.commands:
            if command[0] == "rm":
                self.assertRegex(command[-1], "^[a-f0-9]{64}$")

    def test_uncertain_cleanup_blocks_success_and_replacement(self):
        from core.capacity import CapacityError, Container
        from core.tests.docker_fake import DockerFake

        docker = DockerFake()
        with self.assertRaises(CapacityError), permit("native", 1, lambda: False) as slot:
            with Container(slot, docker) as container:
                container.create(["create", "--name", slot.name, "image"])
                docker.fail_remove = True
        with self.assertRaises(CapacityError), permit("native", 1, lambda: False) as slot:
            with Container(slot, docker):
                self.fail("uncertain abandoned container admitted")
        self.assertEqual(docker.counter, 1)
        docker.fail_remove = False
        with permit("native", 1, lambda: False) as slot, Container(slot, docker):
            pass
        self.assertFalse(docker.objects)

    def test_unverified_container_never_removed(self):
        from core.capacity import CapacityError, Container
        from core.tests.docker_fake import DockerFake

        docker = DockerFake()
        with permit("native", 1, lambda: False) as slot:
            container = Container(slot, docker)
            identifier = container.create(["create", "--name", slot.name, "image"])
            docker.objects[identifier]["Config"]["Labels"]["auto-reviewer.namespace"] = "other"
            with self.assertRaises(CapacityError):
                container.cleanup()
        self.assertFalse(any(command[0] == "rm" for command in docker.commands))

    def test_api_key_fingerprints_and_explicit_account_aliases(self):
        from types import SimpleNamespace

        from core.capacity import api_scope

        first = SimpleNamespace(kind="openai", credential_id=1)
        second = SimpleNamespace(kind="openai", credential_id=2)
        self.assertEqual(api_scope(first, "secret"), api_scope(second, "secret"))
        self.assertNotEqual(api_scope(first, "secret"), api_scope(second, "different"))
        self.assertNotIn("secret", api_scope(first, "secret"))
        with override_settings(API_ACCOUNT_GROUPS={"1": "account", "2": "account"}):
            self.assertEqual(api_scope(first, "secret"), api_scope(second, "different"))

    def test_api_actual_adapter_cap_holds_through_transport_completion(self):
        import time
        from types import SimpleNamespace
        from unittest.mock import patch

        from core.providers import review
        from core.tests.test_providers import RESULT

        active = [0, 0]
        mutex = threading.Lock()
        errors = []

        def transport(*args, **kwargs):
            with mutex:
                active[0] += 1
                active[1] = max(active)
            time.sleep(0.03)
            with mutex:
                active[0] -= 1
            return RESULT

        def run():
            try:
                review(
                    SimpleNamespace(kind="openai", model="test", _capacity_scope="same-key"),
                    {},
                    lambda: False,
                )
            except Exception as exc:
                errors.append(exc)

        with (
            override_settings(API_ACCOUNT_CONCURRENCY=2),
            patch("core.providers._review", side_effect=transport),
        ):
            threads = [threading.Thread(target=run) for _ in range(7)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(active, [0, 2])

    def test_api_wait_events_stay_on_controller_and_wait_not_active_time(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from core.review_orchestration import run_team
        from core.tests.test_review_team import report, synthesis
        from core.tests.test_review_v2 import context

        controller = threading.get_ident()
        callbacks = []
        now = [0]

        def call(provider, evidence, cancelled, **kwargs):
            with permit("api", 1, cancelled, on_wait=kwargs["on_capacity"]):
                # Simulate a very long admission queue before starting this phase.
                now[0] += 10000
                with kwargs["budget"].active():
                    import time

                    time.sleep(0.03)
                    kwargs["budget"].check()
                    return synthesis() if kwargs["task"] == "review_synthesis" else report()

        def progress(code):
            callbacks.append((threading.get_ident(), code))

        with patch("core.capacity.time.monotonic", side_effect=lambda: now[0]):
            result, _ = run_team(
                SimpleNamespace(kind="openai"),
                context(),
                lambda: False,
                call=call,
                progress=progress,
            )
        self.assertTrue(result["complete"])
        self.assertTrue(all(thread == controller for thread, _ in callbacks))
        self.assertTrue(any(code.endswith("_waiting") for _, code in callbacks))
        self.assertTrue(any(code.endswith("_resumed") for _, code in callbacks))

    def test_kernel_releases_lock_after_owner_exits_without_finally(self):
        ctx = multiprocessing.get_context("fork")
        ready, stop = ctx.Event(), ctx.Event()
        process = ctx.Process(target=hold_until_exit, args=(self.directory.name, ready, stop))
        process.start()
        self.assertTrue(ready.wait(3))
        with self.assertRaises(CapacityBusy), permit("crashed", 1, lambda: False, wait=False):
            pass
        stop.set()
        process.join(3)
        self.assertEqual(process.exitcode, 0)
        with permit("crashed", 1, lambda: False, wait=False):
            pass

    def test_waiting_peer_does_not_pause_running_peer_budget(self):
        from core.providers import ProviderError

        clock = [0]
        running = ActiveBudget(1800, clock=lambda: clock[0])
        waiting = ActiveBudget(1800, clock=lambda: clock[0])
        with self.assertRaises(ProviderError), running.active():
            clock[0] = 1801
            waiting.check()
            running.check()

    def test_recovery_uses_claim_labels_and_cannot_clean_healthy_slot(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from core.capacity import CapacityError, Container, recover_resources
        from core.tests.docker_fake import DockerFake

        docker = DockerFake()
        claim = SimpleNamespace(token="opaque-claim")
        with (
            patch("core.claims.current_claim_token", return_value=claim.token),
            patch("core.runner._docker", side_effect=docker),
        ):
            with permit("native", 1, lambda: False) as slot:
                container = Container(slot, docker)
                identifier = container.create(["create", "--name", slot.name, "image"])
                with self.assertRaises(CapacityError):
                    recover_resources(claim)
                self.assertIn(identifier, docker.objects)
            recover_resources(claim)
        self.assertFalse(docker.objects)

    def test_lost_create_reply_is_reconciled_but_still_fails_closed(self):
        from core.capacity import CapacityError, Container
        from core.runner import RunnerError
        from core.tests.docker_fake import DockerFake

        docker = DockerFake()

        def transport(args, **kwargs):
            result = docker(args, **kwargs)
            if args[0] == "create":
                raise RunnerError()
            return result

        with self.assertRaises(CapacityError), permit("native", 1, lambda: False) as slot:
            with Container(slot, transport) as container:
                container.create(["create", "--name", slot.name, "image"])
        self.assertFalse(docker.objects)

    def test_cli_recovery_sessions_share_active_budget(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from core.cli import review_cli
        from core.providers import ProviderError
        from core.tests.docker_fake import DockerFake

        clock = [0]
        calls = []
        budget = ActiveBudget(10, clock=lambda: clock[0])

        def active(provider, context, cancelled, **kwargs):
            calls.append(kwargs["recovery"])
            clock[0] += 6
            if len(calls) == 1:
                error = ProviderError("invalid", detail="cli_coverage")
                error.missing_evidence = ["files_0"]
                raise error
            return {"complete": True}

        with (
            override_settings(CLI_CODEX_AUTH_VOLUME="test-auth"),
            patch("core.runner._docker", side_effect=DockerFake()),
            patch("core.cli._review_cli_active", side_effect=active),
        ):
            with self.assertRaises(ProviderError) as caught:
                review_cli(SimpleNamespace(kind="codex_cli"), {}, lambda: False, budget=budget)
        self.assertEqual(caught.exception.detail, "review_timeout")
        self.assertEqual(calls, [None, ["files_0"]])

    def test_duplicate_key_gate_survives_conflicting_account_aliases(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from core.providers import resolve_capacity_scope

        first = SimpleNamespace(
            kind="openai", credential_id=1, credential=SimpleNamespace(encrypted_value="encrypted")
        )
        second = SimpleNamespace(
            kind="openai", credential_id=2, credential=SimpleNamespace(encrypted_value="encrypted")
        )
        with (
            override_settings(API_ACCOUNT_GROUPS={"1": "one", "2": "two"}),
            patch("core.providers.decrypt", return_value="same-private-key"),
        ):
            scopes = [resolve_capacity_scope(provider) for provider in (first, second)]
        self.assertEqual(scopes[0][0], scopes[1][0])
        self.assertNotEqual(scopes[0][1], scopes[1][1])

import copy
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch

import httpx
from django.test import override_settings

from core.cli import CLIStream
from core.providers import ProviderError, ReviewCancelled, review_contract
from core.tests.test_review_v2 import context, intent, result


def report(*, finding=False, complete=True):
    return {
        "summary": "Checked affected behavior.",
        "complete": complete,
        "coverage": {"reviewed": ["Widget flow and its callers"], "limitations": []},
        "findings": [
            {
                "id": "F1",
                "path": "widget.py",
                "line": 3,
                "severity": "blocking",
                "scenario": "Existing widget is saved.",
                "evidence": [{"source": "files_0", "quote": "secret"}],
                "impact": "Existing widget content is lost.",
                "fix": "Preserve existing widget content.",
            }
        ]
        if finding
        else [],
    }


def synthesis(*, findings=False):
    value = result()
    value["assessments"] = []
    value["reviewer_summaries"] = {
        "code": "Checked requirements and widget behavior.",
        "security": "Checked widget trust boundaries.",
        "tests": "Checked regression and failure-path coverage.",
    }
    if findings:
        value["blocking"] = True
        value["findings"] = [
            {
                "path": "widget.py",
                "line": 3,
                "body": "Saving an existing widget discards its content. Preserve it.",
                "severity": "blocking",
            }
        ]
        value["action_plan"]["inline_findings"]["decision"] = "publish"
        value["assessments"] = [
            {
                "finding_id": "code:F1",
                "decision": "keep",
                "reason": "Current source confirms content loss.",
                "finding_index": 0,
                "evidence": [{"source": "files_0", "quote": "secret"}],
            }
        ]
    return value


class TeamContractTests(TestCase):
    @skipUnless(shutil.which("node"), "CLI artifact serialization requires Node")
    def test_verbatim_cli_json_excerpts_are_valid_original_evidence(self):
        from core.review_team import validate_report

        serialized = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                "import {artifacts} from './cli/evidence.mjs'; let raw=''; for await (const chunk of process.stdin) raw+=chunk; console.log(artifacts(JSON.parse(raw)).find(item=>item.id==='files_0').text);",
            ],
            input=json.dumps(context()),
            text=True,
            capture_output=True,
            check=True,
            cwd=Path(__file__).resolve().parents[2],
        ).stdout.strip()
        value = report(finding=True)
        value["findings"][0]["evidence"][0]["quote"] = serialized
        self.assertEqual(validate_report(value, context()), value)

    def test_all_specialists_and_synthesis_have_validated_transport_contracts(self):
        for task in ("review_code", "review_security", "review_tests", "review_synthesis"):
            with self.subTest(task=task):
                try:
                    schema, policy, validate = review_contract(task)
                except ProviderError:
                    self.fail(f"Required review contract unavailable: {task}")
                self.assertIn("You have no tools or credentials.", policy)
                self.assertIn("Return only the requested structured review.", policy)
                value = synthesis() if task == "review_synthesis" else report()
                self.assertEqual(validate(value), value)
                self.assertFalse(schema["additionalProperties"])

    def test_rejects_fabricated_evidence_locations_empty_claims_and_duplicate_ids(self):
        from core.review_team import validate_report

        mutations = [
            lambda r: r["findings"][0].update(path="../secret"),
            lambda r: r["findings"][0].update(line=4),
            lambda r: r["findings"][0]["evidence"][0].update(source="invented"),
            lambda r: r["findings"][0]["evidence"][0].update(quote="not in the evidence"),
            lambda r: r["findings"][0].update(impact=" "),
            lambda r: r["findings"].append(copy.deepcopy(r["findings"][0])),
            lambda r: r["coverage"].update(reviewed=[]),
        ]
        self.assertEqual(validate_report(report(finding=True), context())["complete"], True)
        for mutate in mutations:
            value = report(finding=True)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ProviderError):
                validate_report(value, context())

    def test_every_candidate_must_be_accounted_for_and_blocks_cannot_be_downgraded(self):
        from core.review_team import finalize

        reports = {"code": report(finding=True), "security": report(), "tests": report()}
        mutations = [
            lambda r: r.update(assessments=[]),
            lambda r: r["assessments"][0].update(finding_id="invented:F1"),
            lambda r: r["assessments"].append(copy.deepcopy(r["assessments"][0])),
            lambda r: r["assessments"][0].update(reason=" "),
            lambda r: r["assessments"][0].update(finding_index=9),
            lambda r: r["findings"][0].update(severity="suggestion"),
            lambda r: r["findings"].append(copy.deepcopy(r["findings"][0])),
        ]
        for mutate in mutations:
            value = synthesis(findings=True)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ProviderError):
                finalize(value, reports, context())

    def test_duplicate_root_cause_publishes_once_with_both_reviewers_attributed(self):
        from core.review_team import finalize

        reports = {
            "code": report(finding=True),
            "security": report(finding=True),
            "tests": report(),
        }
        value = synthesis(findings=True)
        value["assessments"].append(
            {
                "finding_id": "security:F1",
                "decision": "merge",
                "reason": "Same loss.",
                "finding_index": 0,
                "evidence": [{"source": "files_0", "quote": "secret"}],
            }
        )
        final = finalize(value, reports, context())
        self.assertEqual(len(final["findings"]), 1)
        self.assertTrue(final["blocking"])
        self.assertIn("Code", final["findings"][0]["body"])
        self.assertIn("Security", final["findings"][0]["body"])
        self.assertIn("Test", final["summary"])
        self.assertNotIn("assessments", final)

    def test_rejection_requires_reason_and_no_published_finding(self):
        from core.review_team import finalize

        reports = {"code": report(finding=True), "security": report(), "tests": report()}
        value = synthesis()
        value["assessments"] = [
            {
                "finding_id": "code:F1",
                "decision": "reject",
                "reason": "Caller preserves content before saving.",
                "finding_index": -1,
                "evidence": [{"source": "files_0", "quote": "secret"}],
            }
        ]
        self.assertFalse(finalize(value, reports, context())["blocking"])
        value["assessments"][0]["finding_index"] = 0
        with self.assertRaises(ProviderError):
            finalize(value, reports, context())

    def test_incomplete_review_cannot_be_overridden_or_resolve_threads(self):
        from core.review_team import finalize

        reports = {"code": report(complete=False), "security": report(), "tests": report()}
        reports["code"]["coverage"]["limitations"] = ["Required caller behavior remains uncertain."]
        value = synthesis()
        value["replies"][0].update(action="reply", body="Checked this concern.", resolve=True)
        value["action_plan"]["discussion_replies"]["decision"] = "publish"
        final = finalize(value, reports, context())
        self.assertFalse(final["complete"])
        self.assertTrue(final["blocking"])
        self.assertFalse(final["replies"][0]["resolve"])
        self.assertIn("Incomplete review", final["summary"])
        self.assertIn("Required caller behavior remains uncertain.", final["summary"])

    def test_incomplete_synthesis_cannot_become_nonblocking(self):
        from core.review_team import finalize

        value = synthesis()
        value["complete"] = False
        final = finalize(
            value, {role: report() for role in ("code", "security", "tests")}, context()
        )
        self.assertTrue(final["blocking"])
        self.assertFalse(final["complete"])

    def test_generated_coverage_notice_and_perspectives_fit_summary_limit(self):
        from core.review_team import finalize

        value = synthesis()
        value["summary"] = "a" * 11000
        value["reviewer_summaries"] = {
            role: "Perspective " * 1000 for role in ("code", "security", "tests")
        }
        reports = {role: report(complete=False) for role in ("code", "security", "tests")}
        for item in reports.values():
            item["coverage"]["limitations"] = ["Gap " * 250]
        final = finalize(value, reports, context())
        self.assertLessEqual(len(final["summary"]), 12000)
        self.assertTrue(final["summary"].startswith("**Incomplete review"))
        for title in ("Code", "Security", "Test"):
            self.assertIn(f"**{title}:**", final["summary"])
        self.assertEqual(reports["code"]["coverage"]["limitations"], ["Gap " * 250])


class TeamExecutionTests(TestCase):
    def run_team(self, call, *, kind="codex_cli", cancelled=lambda: False, **kwargs):
        from core.review_orchestration import run_team

        evidence = {**context(), "intent": intent()}
        return run_team(SimpleNamespace(kind=kind), evidence, cancelled, call=call, **kwargs)

    def test_cli_roles_are_independent_serial_and_synthesis_retains_original_evidence(self):
        tasks = []
        snapshots = []

        def call(provider, evidence, cancelled, **kwargs):
            tasks.append(kwargs["task"])
            snapshots.append(copy.deepcopy(evidence))
            evidence["pr"]["body"] = "mutated"
            return synthesis() if kwargs["task"] == "review_synthesis" else report()

        final, audit = self.run_team(call)
        self.assertEqual(
            tasks, ["review_code", "review_security", "review_tests", "review_synthesis"]
        )
        for evidence in snapshots:
            self.assertEqual(evidence["pr"]["body"], "Must preserve old widgets")
            self.assertEqual(evidence["intent"], intent())
        self.assertTrue(all("reviewer_reports" not in c for c in snapshots[:3]))
        self.assertEqual(set(snapshots[-1]["reviewer_reports"]), {"code", "security", "tests"})
        self.assertEqual(set(audit["reports"]), {"code", "security", "tests"})
        self.assertTrue(final["complete"])

    def test_api_roles_overlap_but_all_callbacks_run_on_controller_thread(self):
        barrier = threading.Barrier(3)
        controller = threading.get_ident()
        callbacks = []

        def call(provider, evidence, cancelled, **kwargs):
            if kwargs["task"] == "review_synthesis":
                return synthesis()
            barrier.wait(timeout=3)
            kwargs["on_response"]({"input_tokens": 10})
            kwargs["on_activity"]("cli_output_status", "Checking affected flow.")
            return report()

        self.run_team(
            call,
            kind="openai",
            on_response=lambda phase, data: callbacks.append((phase, threading.get_ident())),
            on_activity=lambda phase, code, text: callbacks.append((phase, threading.get_ident())),
        )
        self.assertTrue(callbacks)
        self.assertTrue(all(thread == controller for _, thread in callbacks))
        self.assertEqual(
            {phase for phase, _ in callbacks}, {"code", "security", "tests", "synthesis"}
        )

    @override_settings(CLI_ACCOUNT_CONCURRENCY=3)
    def test_cli_parallel_roles_preserve_claim_context_and_isolated_evidence(self):
        from core.claims import execution_job, supervisor_owner

        for kind in ("codex_cli", "claude_cli"):
            barrier = threading.Barrier(3)
            controller = threading.get_ident()
            callbacks = []
            budgets = []
            job = object()
            job_token = execution_job.set(job)
            owner_token = supervisor_owner.set("test-supervisor")

            def call(provider, evidence, cancelled, **kwargs):
                self.assertIs(execution_job.get(), job)
                self.assertEqual(supervisor_owner.get(), "test-supervisor")
                self.assertEqual(evidence["pr"]["body"], "Must preserve old widgets")
                budgets.append(kwargs["budget"])
                if kwargs["task"] == "review_synthesis":
                    self.assertEqual(
                        set(evidence["reviewer_reports"]), {"code", "security", "tests"}
                    )
                    return synthesis()
                self.assertNotIn("reviewer_reports", evidence)
                evidence["pr"]["body"] = kwargs["task"]
                barrier.wait(timeout=1)
                kwargs["on_response"]({"input_tokens": 10})
                kwargs["on_activity"]("cli_output_status", "Checking affected flow.")
                kwargs["on_capacity"](False)
                return report()

            try:
                with self.subTest(kind=kind):
                    final, _ = self.run_team(
                        call,
                        kind=kind,
                        on_response=lambda *args: callbacks.append(threading.get_ident()),
                        on_activity=lambda *args: callbacks.append(threading.get_ident()),
                        progress=lambda *args: callbacks.append(threading.get_ident()),
                    )
                    self.assertTrue(final["complete"])
                    self.assertEqual(len({id(budget) for budget in budgets}), 4)
                    self.assertTrue(all(thread == controller for thread in callbacks))
            finally:
                execution_job.reset(job_token)
                supervisor_owner.reset(owner_token)

    @override_settings(CLI_ACCOUNT_CONCURRENCY=3)
    def test_cli_failed_reviewer_joins_cancelled_peers_before_returning(self):
        for kind in ("codex_cli", "claude_cli"):
            barrier = threading.Barrier(3)
            exited = set()

            def call(provider, evidence, cancelled, **kwargs):
                task = kwargs["task"]
                self.assertNotEqual(task, "review_synthesis")
                barrier.wait(timeout=1)
                if task == "review_security":
                    raise ProviderError("unavailable")
                deadline = time.monotonic() + 2
                while not cancelled() and time.monotonic() < deadline:
                    threading.Event().wait(0.01)
                self.assertTrue(cancelled())
                exited.add(task)
                raise ReviewCancelled()

            with self.subTest(kind=kind):
                with self.assertRaises(ProviderError) as caught:
                    self.run_team(call, kind=kind)
                self.assertEqual(caught.exception.kind, "unavailable")
                self.assertEqual(exited, {"review_code", "review_tests"})

    @override_settings(CLI_ACCOUNT_CONCURRENCY=3)
    def test_parallel_cli_auth_precedes_peer_quota_and_preserves_diagnostics(self):
        for kind in ("codex_cli", "claude_cli"):
            barrier = threading.Barrier(3)
            responses = []

            def call(provider, evidence, cancelled, **kwargs):
                barrier.wait(timeout=2)
                if kwargs["task"] == "review_code":
                    raise ProviderError("quota")
                if kwargs["task"] == "review_security":
                    threading.Event().wait(0.03)
                    kwargs["on_response"]({"exit_code": 1})
                    raise ProviderError("auth")
                raise ProviderError("unavailable")

            with self.subTest(kind=kind):
                with self.assertRaises(ProviderError) as caught:
                    self.run_team(
                        call, kind=kind, on_response=lambda p, data: responses.append((p, data))
                    )
                self.assertEqual(caught.exception.kind, "auth")
                self.assertEqual(responses[-1], ("security", {"exit_code": 1}))

    @override_settings(CLI_ACCOUNT_CONCURRENCY=3)
    def test_parallel_cli_user_cancellation_joins_all_started_reviewers(self):
        for kind in ("codex_cli", "claude_cli"):
            barrier = threading.Barrier(3)
            cancellation = threading.Event()
            exited = set()

            def call(provider, evidence, cancelled, **kwargs):
                task = kwargs["task"]
                barrier.wait(timeout=2)
                cancellation.set()
                deadline = time.monotonic() + 2
                while not cancelled() and time.monotonic() < deadline:
                    threading.Event().wait(0.01)
                self.assertTrue(cancelled())
                exited.add(task)
                raise ReviewCancelled()

            with self.subTest(kind=kind):
                with self.assertRaises(ReviewCancelled):
                    self.run_team(call, kind=kind, cancelled=cancellation.is_set)
                self.assertEqual(exited, {"review_code", "review_security", "review_tests"})

    @override_settings(CLI_ACCOUNT_CONCURRENCY=3)
    def test_parallel_cli_budgets_count_active_time_per_phase_not_waiting_time(self):
        for kind in ("codex_cli", "claude_cli"):
            for seconds in (1200, 1801):
                clock = threading.local()
                barrier = threading.Barrier(3)

                def call(provider, evidence, cancelled, **kwargs):
                    if kwargs["task"] != "review_synthesis":
                        barrier.wait(timeout=2)
                    with kwargs["budget"].active():
                        clock.now = getattr(clock, "now", 0) + seconds
                        kwargs["budget"].check()
                    # Time spent outside an active subprocess/request does not consume budget.
                    clock.now += 7200
                    kwargs["budget"].check()
                    return synthesis() if kwargs["task"] == "review_synthesis" else report()

                with (
                    self.subTest(kind=kind, seconds=seconds),
                    patch(
                        "core.capacity.time.monotonic", side_effect=lambda: getattr(clock, "now", 0)
                    ),
                ):
                    if seconds == 1200:
                        final, _ = self.run_team(call, kind=kind)
                        self.assertTrue(final["complete"])
                    else:
                        with self.assertRaises(ProviderError) as caught:
                            self.run_team(call, kind=kind)
                        self.assertEqual(caught.exception.detail, "review_timeout")

    def test_failed_required_reviewer_never_reaches_synthesis(self):
        for failure in (
            ProviderError("unavailable"),
            ProviderError("invalid", detail="cli_coverage"),
        ):
            tasks = []

            def call(provider, evidence, cancelled, **kwargs):
                tasks.append(kwargs["task"])
                if kwargs["task"] == "review_security":
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                return report()

            with self.subTest(failure=failure), self.assertRaises(ProviderError):
                self.run_team(call)
            self.assertNotIn("review_synthesis", tasks)

    def test_incomplete_assessment_finishes_all_roles_without_granting_clearance(self):
        for kind in ("codex_cli", "openai"):
            tasks = []

            def call(provider, evidence, cancelled, **kwargs):
                tasks.append(kwargs["task"])
                if kwargs["task"] == "review_synthesis":
                    return synthesis()
                return report(complete=kwargs["task"] != "review_code")

            with self.subTest(kind=kind):
                final, audit = self.run_team(call, kind=kind)
                self.assertCountEqual(tasks[:3], ["review_code", "review_security", "review_tests"])
                self.assertEqual(tasks[-1], "review_synthesis")
                self.assertFalse(final["complete"])
                self.assertTrue(final["blocking"])
                self.assertFalse(audit["reports"]["code"]["complete"])

    def test_cancellation_after_role_prevents_remaining_cli_calls(self):
        stopped = False
        tasks = []

        def call(provider, evidence, cancelled, **kwargs):
            nonlocal stopped
            tasks.append(kwargs["task"])
            stopped = True
            return report()

        with self.assertRaises(ReviewCancelled):
            self.run_team(call, cancelled=lambda: stopped)
        self.assertEqual(tasks, ["review_code"])

    def test_phase_deadline_prevents_clearance_and_exposes_only_safe_error(self):
        def call(provider, evidence, cancelled, **kwargs):
            with kwargs["budget"].active():
                cancelled()
                kwargs["budget"].check()
            return report()

        # First clock read starts a phase; second exceeds its active budget.
        with patch("core.capacity.time.monotonic", side_effect=[0, 10000, 10000]):
            with self.assertRaises(ProviderError) as caught:
                self.run_team(call)
        self.assertEqual(caught.exception.detail, "review_timeout")

    def test_cli_each_phase_can_analyze_for_twenty_minutes(self):
        elapsed = 0
        tasks = []

        def call(provider, evidence, cancelled, **kwargs):
            nonlocal elapsed
            tasks.append(kwargs["task"])
            with kwargs["budget"].active():
                elapsed += 20 * 60
                cancelled()
            return synthesis() if kwargs["task"] == "review_synthesis" else report()

        with patch("core.capacity.time.monotonic", side_effect=lambda: elapsed):
            final, _ = self.run_team(call)
        self.assertTrue(final["complete"])
        self.assertEqual(
            tasks, ["review_code", "review_security", "review_tests", "review_synthesis"]
        )

    def test_api_specialists_and_adjudication_can_each_analyze_for_twenty_minutes(self):
        elapsed = 0
        barrier = threading.Barrier(3)

        def call(provider, evidence, cancelled, **kwargs):
            nonlocal elapsed
            if kwargs["task"] == "review_synthesis":
                elapsed += 20 * 60
                cancelled()
                return synthesis()
            barrier.wait(timeout=3)
            elapsed = 20 * 60
            return report()

        with patch("core.capacity.time.monotonic", side_effect=lambda: elapsed):
            final, _ = self.run_team(call, kind="openai")
        self.assertTrue(final["complete"])

    def test_cli_phase_cannot_exceed_thirty_minutes(self):
        elapsed = 0
        tasks = []

        def call(provider, evidence, cancelled, **kwargs):
            nonlocal elapsed
            tasks.append(kwargs["task"])
            with kwargs["budget"].active():
                elapsed = 30 * 60
                cancelled()
            return report()

        with patch("core.capacity.time.monotonic", side_effect=lambda: elapsed):
            with self.assertRaises(ProviderError) as caught:
                self.run_team(call)
        self.assertEqual(caught.exception.detail, "review_timeout")
        self.assertEqual(tasks, ["review_code"])

    def test_failed_api_reviewer_cancels_and_joins_remaining_reviewers(self):
        barrier = threading.Barrier(3)
        exited = set()
        lock = threading.Lock()

        def call(provider, evidence, cancelled, **kwargs):
            task = kwargs["task"]
            barrier.wait(timeout=3)
            if task == "review_security":
                raise ProviderError("unavailable")
            deadline = time.monotonic() + 3
            while not cancelled() and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            if not cancelled():
                self.fail("Peer reviewer was not cancelled")
            with lock:
                exited.add(task)
            raise ReviewCancelled()

        with self.assertRaises(ProviderError):
            self.run_team(call, kind="openai")
        self.assertEqual(exited, {"review_code", "review_tests"})

    def test_peer_quota_and_auth_errors_take_precedence_over_recoverable_failure(self):
        for fatal in ("quota", "auth"):
            barrier = threading.Barrier(3)

            def call(provider, evidence, cancelled, **kwargs):
                barrier.wait(timeout=3)
                if kwargs["task"] == "review_security":
                    raise ProviderError("unavailable")
                # Simulate another in-flight request receiving a terminal account error.
                threading.Event().wait(0.03)
                if kwargs["task"] == "review_code":
                    kwargs["on_response"]({"input_tokens": 10})
                    raise ProviderError(fatal)
                raise ReviewCancelled()

            callbacks = []
            with self.subTest(fatal=fatal), self.assertRaises(ProviderError) as caught:
                self.run_team(
                    call, kind="openai", on_response=lambda p, data: callbacks.append((p, data))
                )
            self.assertEqual(caught.exception.kind, fatal)
            self.assertEqual(callbacks[-1], ("code", {"input_tokens": 10}))


class TeamTransportTests(TestCase):
    def test_all_tasks_use_role_contracts_in_both_cli_parsers(self):
        for kind in ("codex_cli", "claude_cli"):
            for task in ("review_code", "review_security", "review_tests", "review_synthesis"):
                value = synthesis() if task == "review_synthesis" else report()
                with self.subTest(kind=kind, task=task):
                    parser = CLIStream(kind, task=task)
                    if kind == "codex_cli":
                        parser.feed(
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": json.dumps(value)},
                            }
                        )
                        parser.feed({"type": "turn.completed"})
                    else:
                        parser.feed(
                            {"type": "result", "subtype": "success", "structured_output": value}
                        )
                    self.assertEqual(parser.result(0), value)

    def test_role_and_synthesis_schemas_reach_both_api_adapters(self):
        from core.providers import review

        real_client = httpx.Client
        for kind in ("openai", "anthropic"):
            for task in ("review_code", "review_security", "review_tests", "review_synthesis"):
                value = synthesis() if task == "review_synthesis" else report()
                requests = []

                def respond(request):
                    requests.append(json.loads(request.content))
                    body = (
                        {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [{"type": "output_text", "text": json.dumps(value)}],
                                }
                            ],
                        }
                        if kind == "openai"
                        else {
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": json.dumps(value)}],
                        }
                    )
                    return httpx.Response(200, json=body)

                provider = SimpleNamespace(
                    kind=kind,
                    model="configured",
                    credential=SimpleNamespace(encrypted_value="unused"),
                )
                with (
                    self.subTest(kind=kind, task=task),
                    patch("core.providers.decrypt", return_value="unused"),
                    patch(
                        "core.providers.httpx.Client",
                        return_value=real_client(transport=httpx.MockTransport(respond)),
                    ),
                ):
                    self.assertEqual(review(provider, context(), lambda: False, task=task), value)
                schema = (
                    requests[0]["text"]["format"]["schema"]
                    if kind == "openai"
                    else requests[0]["output_config"]["format"]["schema"]
                )
                self.assertEqual("assessments" in schema["properties"], task == "review_synthesis")
                self.assertEqual("coverage" in schema["properties"], task != "review_synthesis")

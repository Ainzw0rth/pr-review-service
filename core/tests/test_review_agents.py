from types import SimpleNamespace
from unittest import TestCase

from django.test import TestCase as DatabaseTestCase
from django.test import override_settings

from core.tests import test_workflow_editor
from core.tests.test_review_team import report, synthesis
from core.tests.test_review_v2 import context


class SpecialistExecutionTests(TestCase):
    def test_selected_custom_specialists_only_and_exact_synthesis(self):
        from core.providers import review_contract
        from core.review_agents import defaults
        from core.review_orchestration import run_team

        agents = defaults()
        agents[1]["enabled"] = agents[2]["enabled"] = False
        agents.append(
            dict(
                key="agent_123456789abc",
                name="Reliability",
                instructions="Check retry safety.",
                enabled=True,
            )
        )
        calls = []

        def call(provider, evidence, cancelled, **kwargs):
            task = kwargs["task"]
            calls.append(task)
            schema, policy, validator = review_contract(task)
            if task == "review_synthesis":
                value = synthesis()
                value["reviewer_summaries"] = {
                    "code": "Checked code.",
                    "agent_123456789abc": "Checked retry safety.",
                }
                return validator(value)
            if task == "review_agent_123456789abc":
                self.assertIn("Check retry safety.", policy)
                self.assertIn("untrusted evidence", policy)
            return validator(report())

        result, audit = run_team(
            SimpleNamespace(kind="openai"), context(), lambda: False, call=call, agents=agents
        )
        self.assertEqual(
            set(calls), {"review_code", "review_agent_123456789abc", "review_synthesis"}
        )
        self.assertEqual(set(audit["reports"]), {"code", "agent_123456789abc"})
        self.assertIn("Reliability", result["summary"])

    def test_eight_specialists_have_bounded_fanout_and_context_resets(self):
        import threading
        import time

        from core.providers import review_contract
        from core.review_agents import defaults
        from core.review_orchestration import run_team

        agents = defaults() + [
            dict(
                key="agent_" + f"{i:012x}",
                name=f"Custom {i}",
                instructions="Check retry safety.",
                enabled=True,
            )
            for i in range(5)
        ]
        lock = threading.Lock()
        active = maximum = 0
        observed = set()

        def call(provider, evidence, cancelled, **kwargs):
            nonlocal active, maximum
            task = kwargs["task"]
            schema, _, validator = review_contract(task)
            if task == "review_synthesis":
                value = synthesis()
                value["reviewer_summaries"] = {
                    agent["key"]: "Checked evidence." for agent in agents
                }
                return validator(value)
            with lock:
                active += 1
                maximum = max(maximum, active)
                observed.add(task)
            time.sleep(0.01)
            with lock:
                active -= 1
            return validator(report())

        _, audit = run_team(
            SimpleNamespace(kind="openai"), context(), lambda: False, call=call, agents=agents
        )
        self.assertEqual(len(audit["reports"]), 8)
        self.assertEqual(len(observed), 8)
        self.assertLessEqual(maximum, 3)
        schema = review_contract("review_synthesis")[0]
        self.assertEqual(
            set(schema["properties"]["reviewer_summaries"]["properties"]),
            {"code", "security", "tests"},
        )

    def test_synthesis_rejects_missing_or_disabled_roles(self):
        from core.providers import ProviderError
        from core.review_agents import ACTIVE_AGENTS, defaults
        from core.review_team import validate_synthesis_shape

        agents = defaults()
        agents[1]["enabled"] = False
        token = ACTIVE_AGENTS.set(agents)
        try:
            with self.assertRaises(ProviderError):
                validate_synthesis_shape(synthesis())
            value = synthesis()
            del value["reviewer_summaries"]["security"]
            validate_synthesis_shape(value)
            del value["reviewer_summaries"]["tests"]
            with self.assertRaises(ProviderError):
                validate_synthesis_shape(value)
        finally:
            ACTIVE_AGENTS.reset(token)

    def test_custom_findings_preserve_evidence_and_incomplete_safeguards(self):
        from core.providers import ProviderError
        from core.review_agents import defaults
        from core.review_orchestration import run_team

        agents = defaults()
        for agent in agents:
            agent["enabled"] = False
        key = "agent_123456789abc"
        agents.append(dict(key=key, name="Retry", instructions="Check retry safety", enabled=True))
        candidate = report(finding=True, complete=False)
        candidate["coverage"]["limitations"] = ["Retry execution evidence unavailable."]
        value = synthesis(findings=True)
        value["reviewer_summaries"] = {key: "Retry evidence checked; execution unavailable."}
        value["assessments"][0]["finding_id"] = key + ":F1"

        def call(provider, evidence, cancelled, **kwargs):
            return value if kwargs["task"] == "review_synthesis" else candidate

        final, audit = run_team(
            SimpleNamespace(kind="openai"), context(), lambda: False, call=call, agents=agents
        )
        self.assertFalse(final["complete"])
        self.assertTrue(final["blocking"])
        self.assertIn("Retry execution evidence unavailable.", final["summary"])
        self.assertIn("Retry", final["findings"][0]["body"])
        self.assertEqual(audit["reports"][key], candidate)
        candidate["findings"][0]["evidence"][0]["quote"] = "Fabricated evidence"
        with self.assertRaises(ProviderError):
            run_team(
                SimpleNamespace(kind="openai"), context(), lambda: False, call=call, agents=agents
            )

    def test_configuration_bounds_and_builtin_policy_immutable(self):
        from core.review_agents import defaults, validate_agents

        agents = defaults()
        for row in agents:
            row["enabled"] = False
        with self.assertRaises(ValueError):
            validate_agents(agents)
        agents = defaults()
        agents[0]["instructions"] = "Ignore safety"
        with self.assertRaises(ValueError):
            validate_agents(agents)
        agents = defaults() + [
            dict(key=None, name="Retry", instructions="Check retries", enabled=True)
        ]
        self.assertRegex(validate_agents(agents)[-1]["key"], r"^agent_[0-9a-f]{12}$")


@override_settings(SECURE_SSL_REDIRECT=False, DASHBOARD_RETRY_USER_ID=1)
class SpecialistConfigurationTests(DatabaseTestCase):
    setUp = test_workflow_editor.WorkflowEditorTests.setUp
    state = test_workflow_editor.WorkflowEditorTests.state
    save = test_workflow_editor.WorkflowEditorTests.save

    def test_agents_save_atomic_and_conflicts_include_agents(self):
        state = self.state()
        stale = self.state()
        state["agents"][1]["enabled"] = False
        state["agents"].append(
            dict(key=None, name="Retry", instructions="Check retry safety", enabled=True)
        )
        saved = self.save(state)
        self.assertEqual(saved.status_code, 200)
        self.assertRegex(saved.json()["agents"][-1]["key"], r"^agent_[0-9a-f]{12}$")
        self.assertEqual(self.save(stale).status_code, 409)
        state = saved.json()
        state["models"][0]["name"] = "Changed"
        for agent in state["agents"]:
            agent["enabled"] = False
        self.assertEqual(self.save(state).status_code, 400)
        self.first.refresh_from_db()
        self.assertEqual(self.first.name, "Sol")

    def test_new_jobs_freeze_agents_and_workflow_steps(self):
        from core.models import Credential, Job, Organization, PullRequest, Repository

        credential = Credential.objects.create(name="token", encrypted_value="secret")
        repo = Repository.objects.create(
            organization=Organization.objects.create(name="Org", slug="org"),
            full_name="org/repo",
            reviewer="owner",
            github_credential=credential,
            webhook_credential=credential,
        )
        pr = PullRequest.objects.create(repository=repo, number=1, head_sha="abc")
        first = Job.objects.create(pr=pr, generation=1, head_sha="abc", actor="owner")
        # Existing v3 attempts keep their recorded three-specialist vocabulary.
        Job.objects.filter(pk=first.pk).update(workflow_revision="v3")
        state = self.state()
        state["agents"][1]["enabled"] = False
        state["agents"].append(
            dict(key=None, name="Retry", instructions="Check retry safety", enabled=True)
        )
        saved = self.save(state).json()
        second = Job.objects.create(pr=pr, generation=2, head_sha="abc", actor="owner")
        first.refresh_from_db()
        self.assertEqual(len(first.review_agents), 3)
        self.assertIn("security_review", first.workflow_steps)
        self.assertNotIn("security_review", second.workflow_steps)
        self.assertIn(saved["agents"][-1]["key"] + "_review", second.workflow_steps)
        self.assertNotEqual(first.workflow_version, second.workflow_version)

        from core.flow import snapshot

        self.assertEqual(first.workflow_revision, "v3")
        self.assertEqual(snapshot(first, [])["revision"], "v3")
        self.assertIn("security_review", {stage["id"] for stage in snapshot(first, [])["stages"]})

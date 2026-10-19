import copy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch

from core import review_v2
from core.providers import SCHEMA, ProviderError


def context():
    return {
        "head_sha": "a" * 40,
        "complete": True,
        "pr": {"number": 4, "title": "Add widget", "body": "Must preserve old widgets"},
        "comments": [{"body": "Explicitly exclude blue widgets"}],
        "threads": [{"comments": [{"databaseId": 10, "body": "What about old widgets?"}]}],
        "issues": [{"number": 7, "body": "Acceptance criteria"}],
        "related_prs": [{"number": 8, "body": "Dependency", "files": [{"patch": "secret"}]}],
        "files": [{"filename": "widget.py", "valid_lines": [3], "content": "secret"}],
        "tasks": [{"id": "TASK-1", "description": "Preserve old widgets"}],
        "instructions": "Follow acceptance criteria",
    }


def intent():
    return {
        "goal": {"text": "Add widget support", "sources": ["pr"], "certainty": "inferred"},
        "requirements": [
            {"text": "Preserve old widgets", "sources": ["pr"], "certainty": "stated"}
        ],
        "non_goals": [{"text": "Blue widgets", "sources": ["comments_0"], "certainty": "stated"}],
        "unknowns": [{"text": "Behavior for red widgets unspecified", "sources": []}],
    }


def result():
    return {
        "summary": "Reviewed",
        "blocking": False,
        "complete": True,
        "findings": [],
        "replies": [
            {
                "comment_id": 10,
                "body": "",
                "resolve": False,
                "action": "not_needed",
                "reason": "Already discussed",
                "blocking": False,
            }
        ],
        "related_comments": [],
        "action_plan": {
            name: {"decision": "not_needed", "reason": "No new concern"}
            for name in (
                "inline_findings",
                "discussion_replies",
                "related_pr_comments",
                "related_issue_comments",
            )
        },
    }


class IntentTests(TestCase):
    @skipUnless(shutil.which("node"), "Real CLI evidence serialization requires Node")
    def test_cli_manifest_citations_pass_worker_grounding_validation(self):
        from core.cli import CLIStream, review_cli

        captured = review_v2.intent_context(context())
        with patch("core.cli._review_cli_once", return_value={}) as run:
            review_cli(
                SimpleNamespace(kind="codex_cli", model="configured"),
                captured,
                lambda: False,
                task="intent",
            )
        delivered = run.call_args.args[1]
        serialized = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                "import {artifacts} from './cli/evidence.mjs';"
                "let raw=''; for await (const chunk of process.stdin) raw+=chunk;"
                "const [intent,review]=JSON.parse(raw);"
                "console.log(JSON.stringify({intent:artifacts(intent),"
                "review:artifacts(review).map(entry=>entry.id)}));",
            ],
            input=json.dumps([delivered, context()]),
            text=True,
            capture_output=True,
            check=True,
            cwd=Path(__file__).resolve().parents[2],
        )
        manifests = json.loads(serialized.stdout)
        entries = manifests["intent"]
        self.assertGreater(len(entries), 2)
        for entry in entries:
            if entry["id"] in {"head_sha", "complete"}:
                continue
            for kind in ("codex_cli", "claude_cli"):
                with self.subTest(source=entry["id"], backend=kind):
                    value = {
                        "goal": {
                            "text": "A grounded goal",
                            "sources": [entry["id"]],
                            "certainty": "inferred",
                        },
                        "requirements": [],
                        "non_goals": [],
                        "unknowns": [],
                    }
                    parser = CLIStream(kind, delivered, task="intent")
                    parser.feed(
                        {"type": "runner_coverage", "read": len(entries), "total": len(entries)}
                    )
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
                    self.assertTrue(parser.coverage)
                    self.assertEqual(review_v2.validate_intent(parser.result(0), context()), value)
                    self.assertIn(entry["id"], manifests["review"])

    def test_intent_failures_have_safe_specific_reasons(self):
        for mutate, reason in [
            (lambda value: value.update(goal={}), "intent_schema"),
            (lambda value: value["goal"].update(text="   "), "intent_empty_claim"),
            (
                lambda value: value["goal"].update(sources=["private-invented-source"]),
                "intent_unknown_source",
            ),
        ]:
            value = intent()
            mutate(value)
            with self.subTest(reason=reason), self.assertRaises(ProviderError) as caught:
                review_v2.validate_intent(value, context())
            self.assertEqual(caught.exception.validation_error, reason)
            self.assertNotIn("private-invented-source", str(caught.exception))

    def test_intent_is_grounded_in_fixed_non_code_evidence(self):
        evidence = review_v2.intent_context(context())
        self.assertEqual(set(evidence), {"head_sha", "complete", "evidence"})
        self.assertEqual(evidence, review_v2.intent_context(context()))
        self.assertNotIn("secret", str(evidence))
        self.assertEqual(review_v2.validate_intent(intent(), context()), intent())

    def test_unknown_sources_and_inferred_requirements_are_rejected(self):
        for field, mutation in [
            ("goal", {"sources": ["files_0"]}),
            ("requirements", {"certainty": "inferred"}),
            ("non_goals", {"certainty": "inferred"}),
            ("goal", {"sources": []}),
            ("goal", {"sources": ["pr", "pr"]}),
            ("goal", {"text": "   "}),
        ]:
            with self.subTest(field=field, mutation=mutation):
                value = intent()
                claim = value[field] if field == "goal" else value[field][0]
                claim.update(mutation)
                with self.assertRaises(ProviderError):
                    review_v2.validate_intent(value, context())

    def test_contracts_preserve_legacy_schema_and_fixed_prompt_markers(self):
        self.assertEqual(review_v2.contract("review")[0], SCHEMA)
        for task, value in [("intent", intent()), ("review_v2", result())]:
            schema, system, validator = review_v2.contract(task)
            self.assertIn("You have no tools or credentials.", system)
            self.assertIn("Return only the requested structured review.", system)
            self.assertEqual(validator(value), value)
            self.assertIsInstance(schema, dict)


class ActionTests(TestCase):
    def test_optional_thread_assessment_and_related_issue_publication(self):
        value = result()
        value["related_comments"] = [{"number": 7, "body": "Dependency unresolved"}]
        value["action_plan"]["related_issue_comments"]["decision"] = "publish"
        self.assertEqual(review_v2.validate_actions(value, context()), value)

    def test_rejects_invalid_actions_or_targets(self):
        variants = []
        value = result()
        value["replies"][0]["resolve"] = True
        variants.append(value)
        value = result()
        value["replies"][0]["blocking"] = True
        variants.append(value)
        value = result()
        value["replies"] = []
        variants.append(value)
        value = result()
        value["replies"][0]["comment_id"] = 11
        variants.append(value)
        for number in (4, 999):
            value = result()
            value["related_comments"] = [{"number": number, "body": "No"}]
            variants.append(value)
        for line in (2, 3):
            value = result()
            value["findings"] = [
                {"path": "widget.py", "line": line, "body": "Bug", "severity": "suggestion"}
            ]
            variants.append(value)  # line 3 also requires matching publish decision
        value = result()
        value["action_plan"]["discussion_replies"]["reason"] = " "
        variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                review_v2.validate_actions(value, context())

    def test_shape_validation_does_not_require_context(self):
        value = copy.deepcopy(result())
        value["replies"][0].update(action="reply", body="Still broken", blocking=True)
        value["blocking"] = True
        value["action_plan"]["discussion_replies"]["decision"] = "publish"
        self.assertEqual(review_v2.contract("review_v2")[2](value), value)
        self.assertEqual(review_v2.validate_actions(value, context()), value)

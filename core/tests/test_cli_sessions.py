import uuid

from django.test import TestCase

from core.models import Job, Provider
from core.tests import test_claims


class CLISessionTests(TestCase):
    setUp = test_claims.ClaimTests.setUp
    job = test_claims.ClaimTests.job

    def test_same_pr_resumes_across_jobs_but_roles_and_providers_are_isolated(self):
        from core.cli_sessions import prepare_sessions, save_session

        job = self.job(1)
        job.status = "running"
        job.save()
        provider = Provider.objects.create(name="Codex", kind="codex_cli", model="model")
        sessions = prepare_sessions(job, provider, ["review_code", "review_security"])
        session_id = str(uuid.uuid4())
        save_session(
            job, sessions, {**sessions["review_code"], "id": session_id, "task": "review_code"}
        )
        resumed = prepare_sessions(job, provider, ["review_code", "review_security"])
        self.assertEqual(resumed["review_code"]["id"], session_id)
        self.assertIsNone(resumed["review_security"]["id"])
        self.assertNotEqual(resumed["review_security"]["key"], resumed["review_code"]["key"])
        other_job = self.job(2)
        other_job.status = "running"
        other_job.save()
        other = prepare_sessions(other_job, provider, ["review_code"])
        self.assertIsNone(other["review_code"]["id"])
        self.assertNotEqual(other["review_code"]["key"], resumed["review_code"]["key"])
        claude = Provider.objects.create(name="Claude", kind="claude_cli", model="model")
        self.assertIsNone(prepare_sessions(job, claude, ["review_code"])["review_code"]["id"])
        # A later cycle on the same PR (including a new commit) keeps the session.
        job.pr.head_sha = "b" * 40
        job.pr.generation += 1
        job.pr.save()
        later = Job.objects.create(
            pr=job.pr, head_sha=job.pr.head_sha, generation=job.pr.generation, status="running"
        )
        self.assertEqual(
            prepare_sessions(later, provider, ["review_code"])["review_code"]["id"], session_id
        )

    def test_contract_changes_reset_storage_and_stale_or_foreign_writes_are_rejected(self):
        from core.cli_sessions import prepare_sessions, save_session
        from core.publication import StaleReview

        job = self.job(1)
        job.status = "running"
        job.save()
        provider = Provider.objects.create(name="Codex", kind="codex_cli", model="model")
        sessions = prepare_sessions(job, provider, ["review_code"])
        session_id = str(uuid.uuid4())
        save_session(
            job, sessions, {**sessions["review_code"], "id": session_id, "task": "review_code"}
        )
        provider.model = "different"
        changed = prepare_sessions(job, provider, ["review_code"])
        self.assertIsNone(changed["review_code"]["id"])
        self.assertNotEqual(changed["review_code"]["key"], sessions["review_code"]["key"])
        save_session(
            job, changed, {**sessions["review_code"], "id": session_id, "task": "review_code"}
        )
        self.assertIsNone(prepare_sessions(job, provider, ["review_code"])["review_code"]["id"])
        job.status = "cancelled"
        job.save()
        with self.assertRaises(StaleReview):
            save_session(
                job, changed, {**changed["review_code"], "id": session_id, "task": "review_code"}
            )

    def test_native_session_ids_are_validated_and_never_copied_from_model_text(self):
        from core.cli import CLIStream

        for kind, event in [
            ("codex_cli", {"type": "thread.started", "thread_id": str(uuid.uuid4())}),
            ("claude_cli", {"type": "system", "subtype": "init", "session_id": str(uuid.uuid4())}),
        ]:
            parser = CLIStream(kind)
            parser.feed(event)
            self.assertEqual(parser.session_id, event.get("thread_id", event.get("session_id")))
            parser.feed(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": str(uuid.uuid4())},
                }
            )
            self.assertEqual(parser.session_id, event.get("thread_id", event.get("session_id")))
            parser = CLIStream(kind)
            bad = {**event, "thread_id": "../private", "session_id": "../private"}
            parser.feed(bad)
            self.assertIsNone(parser.session_id)

    def test_missing_history_relocation_rotates_key_and_updates_caller_by_compare_and_swap(self):
        from core.cli_sessions import prepare_sessions, save_session

        job = self.job(1)
        job.status = "running"
        job.save()
        provider = Provider.objects.create(name="Codex", kind="codex_cli", model="model")
        sessions = prepare_sessions(job, provider, ["review_code"])
        save_session(
            job,
            sessions,
            {**sessions["review_code"], "id": str(uuid.uuid4()), "task": "review_code"},
        )
        previous = dict(sessions["review_code"])
        replacement = {"key": str(uuid.uuid4()), "id": str(uuid.uuid4())}
        save_session(
            job, sessions, {**replacement, "previous_key": previous["key"], "task": "review_code"}
        )
        self.assertEqual(sessions["review_code"], replacement)
        self.assertEqual(
            prepare_sessions(job, provider, ["review_code"])["review_code"], replacement
        )
        # A delayed callback with the old storage key cannot overwrite or mutate its caller map.
        stale = {"review_code": dict(previous)}
        save_session(
            job,
            stale,
            {
                "key": str(uuid.uuid4()),
                "id": str(uuid.uuid4()),
                "previous_key": previous["key"],
                "task": "review_code",
            },
        )
        self.assertEqual(stale["review_code"], previous)
        self.assertEqual(
            prepare_sessions(job, provider, ["review_code"])["review_code"], replacement
        )

    def test_relocation_rejects_mismatched_previous_key_and_first_use(self):
        from core.cli_sessions import prepare_sessions, save_session

        job = self.job(1)
        job.status = "running"
        job.save()
        provider = Provider.objects.create(name="Codex", kind="codex_cli", model="model")
        sessions = prepare_sessions(job, provider, ["review_code"])
        original = dict(sessions["review_code"])
        for previous_key in (original["key"], str(uuid.uuid4())):
            save_session(
                job,
                sessions,
                {
                    "key": str(uuid.uuid4()),
                    "id": str(uuid.uuid4()),
                    "previous_key": previous_key,
                    "task": "review_code",
                },
            )
        self.assertEqual(prepare_sessions(job, provider, ["review_code"])["review_code"], original)
        save_session(job, sessions, {**original, "id": str(uuid.uuid4()), "task": "review_code"})
        original = dict(sessions["review_code"])
        save_session(
            job,
            sessions,
            {
                "key": str(uuid.uuid4()),
                "id": str(uuid.uuid4()),
                "previous_key": str(uuid.uuid4()),
                "task": "review_code",
            },
        )
        self.assertEqual(prepare_sessions(job, provider, ["review_code"])["review_code"], original)

    def test_runner_relocation_metadata_validated_and_forwarded_only_for_requested_session(self):
        import json

        from core.cli import CLIStream, _cli_result
        from core.providers import ProviderError
        from core.tests.test_providers import RESULT

        session = {"key": str(uuid.uuid4()), "id": str(uuid.uuid4())}
        replacement = str(uuid.uuid4())

        def parser():
            stream = CLIStream("codex_cli")
            stream.feed({"type": "thread.started", "thread_id": str(uuid.uuid4())})
            stream.feed({"type": "runner_coverage", "read": 0, "total": 0})
            stream.feed(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(RESULT)},
                }
            )
            stream.feed({"type": "turn.completed"})
            return stream

        stream = parser()
        stream.feed(
            {
                "type": "runner_session",
                "resumed": False,
                "previous_key": session["key"],
                "key": replacement,
            }
        )
        self.assertEqual(_cli_result(stream, session, "review", 0), RESULT)
        self.assertEqual(stream.diagnostics["cli_session"]["key"], replacement)
        self.assertEqual(stream.diagnostics["cli_session"]["previous_key"], session["key"])
        with self.assertRaises(ProviderError):
            _cli_result(stream, {**session, "key": str(uuid.uuid4())}, "review", 0)
        for changes in ({"key": "../other"}, {"previous_key": "invalid"}, {"resumed": True}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                parser().feed(
                    {
                        "type": "runner_session",
                        "resumed": False,
                        "previous_key": session["key"],
                        "key": replacement,
                        **changes,
                    }
                )
        legacy = parser()
        legacy.feed({"type": "runner_session", "resumed": False})
        _cli_result(legacy, session, "review", 0)
        self.assertEqual(legacy.diagnostics["cli_session"]["key"], session["key"])
        self.assertNotIn("previous_key", legacy.diagnostics["cli_session"])

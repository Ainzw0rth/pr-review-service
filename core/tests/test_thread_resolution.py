import copy
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.models import Receipt
from core.publication import StaleReview, publish
from core.tests import test_publication as publication_tests
from core.tests import test_workflow as workflow_tests


@override_settings(LIVE_WRITES=True)
class ThreadResolutionTests(TestCase):
    setUp = workflow_tests.WorkflowTests.setUp
    snapshot = workflow_tests.WorkflowTests.snapshot
    event = workflow_tests.WorkflowTests.event
    reconcile = workflow_tests.WorkflowTests.reconcile
    prepare = publication_tests.PublicationTests.prepare
    result = publication_tests.PublicationTests.result

    def setup_thread(self, *, resolve=True):
        job, gh = self.prepare()
        thread = {
            "id": "PRRT_one",
            "isResolved": False,
            "viewerCanResolve": True,
            "path": "cart.py",
            "line": 2,
            "comments": [
                {
                    "databaseId": 11,
                    "body": "Empty carts fail",
                    "updatedAt": "t1",
                    "author": {"login": "human"},
                }
            ],
        }
        context = {"threads": [copy.deepcopy(thread)], "complete": True}
        result = self.result()
        result["replies"] = [
            {
                "comment_id": 11,
                "body": "`total([])` now returns `0` in `cart.py`.",
                "resolve": resolve,
            }
        ]
        mutations = []

        def graphql(query, variables):
            if "mutation" in query:
                mutations.append(variables)
                thread["isResolved"] = True
                return {"resolveReviewThread": {"thread": {"id": thread["id"], "isResolved": True}}}
            return {
                "node": {
                    **copy.deepcopy(thread),
                    "pullRequest": {
                        "number": job.pr.number,
                        "headRefOid": job.head_sha,
                        "repository": {"nameWithOwner": job.pr.repository.full_name},
                    },
                    "comments": {
                        "nodes": copy.deepcopy(thread["comments"]),
                        "pageInfo": {"hasNextPage": False},
                    },
                }
            }

        original = gh.post

        def post(path, data):
            row = original(path, data)
            if path.endswith("/replies"):
                thread["comments"].append(
                    {
                        "databaseId": row["id"],
                        "body": row["body"],
                        "author": {"login": "louis"},
                        "updatedAt": "t2",
                    }
                )
            return row

        gh.graphql = graphql
        gh.post = post
        return job, gh, result, context, thread, mutations

    def run_publish(self, gh, job, result, context):
        return publish(gh, job, result, context, {"passed": True, "summary": "Pass"})

    def test_resolution_applied_but_response_lost_is_reconciled(self):
        from core.github import IntegrationError

        job, gh, result, context, thread, mutations = self.setup_thread()
        original = gh.graphql

        def graphql(query, variables):
            response = original(query, variables)
            if "mutation" in query:
                thread["resolvedBy"] = {"login": "louis"}
                raise IntegrationError("GitHub discussion context unavailable.")
            return response

        gh.graphql = graphql
        self.run_publish(gh, job, result, context)
        self.assertEqual(len(mutations), 1)
        self.assertTrue(Receipt.objects.filter(job=job, key="resolve:PRRT_one").exists())

    def test_retry_recovers_missing_resolution_receipt_without_permission_note_mismatch(self):
        from core.delivery import verify_inputs

        job, gh, result, context, thread, mutations = self.setup_thread()
        self.run_publish(gh, job, result, context)
        Receipt.objects.filter(job=job, key="resolve:PRRT_one").delete()
        thread["resolvedBy"] = {"login": "louis"}
        thread["viewerCanResolve"] = False  # GitHub reports false after resolution.
        with patch("core.context.threads", return_value=[]):
            verify_inputs(gh, job, context, result)
            verify_inputs(gh, job, context, result)
        self.assertTrue(Receipt.objects.filter(job=job, key="resolve:PRRT_one").exists())
        self.assertEqual(len(mutations), 1)

    def test_resolution_reconciliation_rejects_changed_discussion(self):
        from core.context import review_thread
        from core.publication import resolution_confirmed

        job, gh, result, context, thread, mutations = self.setup_thread()
        self.run_publish(gh, job, result, context)
        thread["resolvedBy"] = {"login": "louis"}
        fresh = review_thread(gh, thread["id"])
        self.assertTrue(
            resolution_confirmed(gh, job, context["threads"][0], result["replies"][0], fresh)
        )
        fresh["comments"][0]["body"] = "Changed by developer"
        self.assertFalse(
            resolution_confirmed(gh, job, context["threads"][0], result["replies"][0], fresh)
        )
        fresh = review_thread(gh, thread["id"])
        fresh["resolvedBy"] = {"login": "someone-else"}
        self.assertFalse(
            resolution_confirmed(gh, job, context["threads"][0], result["replies"][0], fresh)
        )

    def test_failed_resolution_that_remains_open_has_no_success_receipt(self):
        from core.delivery import safe_error
        from core.github import IntegrationError

        job, gh, result, context, thread, mutations = self.setup_thread()
        original = gh.graphql

        def graphql(query, variables):
            if "mutation" in query:
                raise IntegrationError("GitHub discussion context unavailable.")
            return original(query, variables)

        gh.graphql = graphql
        with self.assertRaises(IntegrationError) as error:
            self.run_publish(gh, job, result, context)
        self.assertEqual(safe_error(error.exception), "GitHub discussion context unavailable.")
        self.assertFalse(Receipt.objects.filter(job=job, key="resolve:PRRT_one").exists())
        self.assertFalse(thread["isResolved"])

    def test_fixed_human_thread_gets_explanation_and_resolution_receipt(self):
        job, gh, result, context, thread, mutations = self.setup_thread()
        self.run_publish(gh, job, result, context)
        self.assertEqual(len(mutations), 1)
        self.assertTrue(thread["isResolved"])
        self.assertTrue(Receipt.objects.filter(job=job, key="resolve:PRRT_one").exists())
        self.assertIn("/pulls/1/comments/11/replies", [p for p, _ in gh.writes])

    def test_unfixed_thread_gets_reply_but_stays_open(self):
        job, gh, result, context, thread, mutations = self.setup_thread(resolve=False)
        result["blocking"] = True
        result["replies"][0]["body"] = (
            "`total([])` still indexes the first item; handle empty carts."
        )
        self.assertEqual(self.run_publish(gh, job, result, context), "feedback")
        self.assertFalse(thread["isResolved"])
        self.assertEqual(mutations, [])

    def test_missing_assessment_rejected_before_writes(self):
        job, gh, result, context, _, mutations = self.setup_thread()
        result["replies"] = []
        with self.assertRaises(ValueError):
            self.run_publish(gh, job, result, context)
        self.assertEqual(gh.writes, [])
        self.assertEqual(mutations, [])

    def test_incomplete_review_cannot_resolve(self):
        job, gh, result, context, _, mutations = self.setup_thread()
        result["complete"] = False
        with self.assertRaises(ValueError):
            self.run_publish(gh, job, result, context)
        self.assertEqual(gh.writes, [])
        self.assertEqual(mutations, [])

    def test_new_or_edited_comment_stops_resolution(self):
        for edit in (False, True):
            with self.subTest(edit=edit):
                job, gh, result, context, thread, mutations = self.setup_thread()
                if edit:
                    thread["comments"][0]["updatedAt"] = "changed"
                else:
                    thread["comments"].append({"databaseId": 25, "body": "Another failing case"})
                with self.assertRaises(StaleReview):
                    self.run_publish(gh, job, result, context)
                self.assertEqual(mutations, [])
                self.assertEqual(gh.writes, [])
                job.status = "superseded"
                job.save()

    def test_comment_arriving_after_reply_stops_resolution(self):
        job, gh, result, context, thread, mutations = self.setup_thread()
        original = gh.post

        def post(path, data):
            row = original(path, data)
            if path.endswith("/replies"):
                thread["comments"].append({"databaseId": 50, "body": "Not fixed"})
            return row

        gh.post = post
        with self.assertRaises(StaleReview):
            self.run_publish(gh, job, result, context)
        self.assertEqual(mutations, [])

    def test_human_resolved_thread_not_replied_to_or_resolved_again(self):
        job, gh, result, context, thread, mutations = self.setup_thread()
        thread["isResolved"] = True
        self.run_publish(gh, job, result, context)
        self.assertEqual(mutations, [])
        self.assertFalse(any(p.endswith("/replies") for p, _ in gh.writes))

    def test_resolution_without_permission_explains_manual_step(self):
        job, gh, result, context, thread, mutations = self.setup_thread()
        thread["viewerCanResolve"] = False
        self.run_publish(gh, job, result, context)
        self.assertEqual(mutations, [])
        reply = next(data["body"] for p, data in gh.writes if p.endswith("/replies"))
        self.assertIn("resolve this thread manually", reply)

    def test_changed_head_after_reply_prevents_resolution(self):
        job, gh, result, context, _, mutations = self.setup_thread()
        original = gh.post

        def post(path, data):
            row = original(path, data)
            if path.endswith("/replies"):
                gh.snapshot["head"]["sha"] = "b" * 40
            return row

        gh.post = post
        with self.assertRaises(StaleReview):
            self.run_publish(gh, job, result, context)
        self.assertEqual(mutations, [])

    def test_thread_from_other_pr_rejected(self):
        job, gh, result, context, _, mutations = self.setup_thread()
        original = gh.graphql

        def graphql(query, variables):
            data = original(query, variables)
            data["node"]["pullRequest"]["number"] = 999
            return data

        gh.graphql = graphql
        with self.assertRaises(StaleReview):
            self.run_publish(gh, job, result, context)
        self.assertEqual(mutations, [])
        self.assertEqual(gh.writes, [])

    def test_crash_after_remote_resolution_recovers_without_second_mutation(self):
        from core.publication import publish_threads, thread_replies

        job, gh, result, context, thread, mutations = self.setup_thread()
        replies = thread_replies(result, context)
        publish_threads(gh, job, replies)
        Receipt.objects.filter(job=job, key="resolve:PRRT_one").delete()
        publish_threads(gh, job, replies)
        self.assertEqual(len(mutations), 1)
        self.assertEqual(sum(p.endswith("/replies") for p, _ in gh.writes), 1)

    def test_duplicate_actions_for_same_thread_rejected(self):
        job, gh, result, context, _, _ = self.setup_thread()
        context["threads"][0]["comments"].append({"databaseId": 22})
        result["replies"].append({"comment_id": 22, "body": "Still broken", "resolve": False})
        with self.assertRaises(ValueError):
            self.run_publish(gh, job, result, context)
        self.assertEqual(gh.writes, [])

    def test_human_reopen_not_overridden_on_publication_resume(self):
        from core.publication import publish_threads, thread_replies

        job, gh, result, context, thread, mutations = self.setup_thread()
        replies = thread_replies(result, context)
        publish_threads(gh, job, replies)
        thread["isResolved"] = False
        with self.assertRaises(StaleReview):
            publish_threads(gh, job, replies)
        self.assertEqual(len(mutations), 1)

    def test_edit_to_own_reply_is_not_ignored(self):
        job, gh, result, context, thread, mutations = self.setup_thread()
        original = gh.post

        def post(path, data):
            row = original(path, data)
            if path.endswith("/replies"):
                thread["comments"][-1]["body"] += " Human correction: not fixed."
            return row

        gh.post = post
        with self.assertRaises(StaleReview):
            self.run_publish(gh, job, result, context)
        self.assertEqual(mutations, [])

    def test_delivery_reconciles_reply_when_post_response_is_lost(self):
        from unittest.mock import patch

        from core.delivery import deliver, save_review
        from core.github import IntegrationError

        job, gh, result, context, thread, mutations = self.setup_thread()
        save_review(job, result, context, {"passed": True, "summary": "Pass"})
        original = gh.post

        def post(path, data):
            row = original(path, data)
            if path.endswith("/replies"):
                raise IntegrationError("GitHub request unavailable or malformed.")
            return row

        gh.post = post
        with (
            patch("core.delivery.time.sleep"),
            patch("core.context.threads", return_value=[thread]),
        ):
            self.assertEqual(deliver(gh, job, publish, lambda *a: None), "ready")
        self.assertEqual(sum(path.endswith("/replies") for path, _ in gh.writes), 1)
        self.assertEqual(len(mutations), 1)

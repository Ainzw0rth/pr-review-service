import base64
import gzip
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx

from core.context import build_context
from core.github import GitHub, IntegrationError, check_required


class GitHubTests(TestCase):
    def test_compressed_response_decoded_once_and_pagination_header_preserved(self):
        link = '<https://api.github.com/repos/acme/app/issues?page=2>; rel="next"'
        client = self.client(
            lambda req: httpx.Response(
                200,
                headers={"Content-Encoding": "gzip", "Link": link},
                stream=httpx.ByteStream(gzip.compress(b'{"login":"reviewer"}')),
            )
        )
        response = client._request("GET", "/user")
        self.assertEqual(response.json(), {"login": "reviewer"})
        self.assertEqual(response.headers["link"], link)
        self.assertNotIn("content-encoding", response.headers)

    def client(self, handler):
        repo = SimpleNamespace(
            full_name="acme/app",
            github_credential=SimpleNamespace(encrypted_value="encrypted"),
        )
        with patch("core.github.decrypt", return_value="private-token"):
            client = GitHub(repo)
        client.client.close()
        client.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        self.addCleanup(client.close)
        return client

    def test_rejects_untrusted_destinations_and_cross_repo_paths(self):
        client = self.client(lambda req: self.fail("must not send"))
        for path in [
            "https://evil.example/x",
            "//evil.example/x",
            "/repos/other/repo/issues",
            "/pulls/../x",
            "/pulls/%2e%2e/x",
        ]:
            with self.subTest(path=path), self.assertRaises(IntegrationError):
                client.get(path)

    def test_pagination_complete_and_secret_never_in_error(self):
        def handler(req):
            self.assertEqual(req.headers["authorization"], "Bearer private-token")
            if req.url.params.get("page") == "2":
                return httpx.Response(200, json=[{"id": 2}])
            return httpx.Response(
                200,
                json=[{"id": 1}],
                headers={
                    "Link": '<https://api.github.com/repos/acme/app/issues?page=2>; rel="next"'
                },
            )

        self.assertEqual(self.client(handler).pages("/issues"), [{"id": 1}, {"id": 2}])
        client = self.client(lambda req: httpx.Response(401, json={"message": "private-token"}))
        with self.assertRaises(IntegrationError) as error:
            client.get("/issues")
        self.assertNotIn("private-token", str(error.exception))

    def test_numeric_repository_pagination_keeps_canonical_endpoint(self):
        def handler(req):
            self.assertEqual(req.url.path, "/repos/acme/app/issues/157/timeline")
            if req.url.params.get("page") == "2":
                return httpx.Response(200, json=[{"id": 101}])
            return httpx.Response(
                200,
                json=[{"id": 100}],
                headers={
                    "Link": '<https://api.github.com/repositories/123/issues/157/timeline?page=2>; rel="next"'
                },
            )

        self.assertEqual(
            self.client(handler).pages("/issues/157/timeline"), [{"id": 100}, {"id": 101}]
        )

    def test_pagination_rejects_changed_resource_or_host(self):
        for destination in [
            "https://api.github.com/repos/other/repo/issues/157/timeline?page=2",
            "https://api.github.com/repositories/123/issues/158/timeline?page=2",
            "https://evil.example/repositories/123/issues/157/timeline?page=2",
        ]:
            with self.subTest(destination=destination):
                requests = []

                def handler(req):
                    requests.append(req.url)
                    return httpx.Response(
                        200, json=[], headers={"Link": f'<{destination}>; rel="next"'}
                    )

                with self.assertRaises(IntegrationError):
                    self.client(handler).pages("/issues/157/timeline")
                self.assertEqual(len(requests), 1)

    def test_pagination_loop_fails_closed(self):
        client = self.client(
            lambda req: httpx.Response(
                200,
                json=[],
                headers={"Link": '<https://api.github.com/repos/acme/app/issues>; rel="next"'},
            )
        )
        with self.assertRaises(IntegrationError):
            client.pages("/issues")

    def test_archive_redirect_strips_authorization(self):
        def handler(req):
            if req.url.host == "api.github.com":
                self.assertIn("authorization", req.headers)
                return httpx.Response(
                    302,
                    headers={
                        "Location": "https://codeload.github.com/acme/app/legacy.tar.gz/" + "a" * 40
                    },
                )
            self.assertEqual(req.url.host, "codeload.github.com")
            self.assertNotIn("authorization", req.headers)
            return httpx.Response(200, content=b"archive")

        self.assertEqual(self.client(handler).archive("a" * 40), b"archive")

    def test_required_checks_latest_status_and_missing_check(self):
        github = Mock()
        github.pages.side_effect = [
            [{"name": "tests", "status": "completed", "conclusion": "success"}],
            [
                {"context": "lint", "state": "pending", "id": 2},
                {"context": "lint", "state": "success", "id": 1},
            ],
        ]
        result = check_required(
            github, SimpleNamespace(required_checks=["tests", "lint"]), "a" * 40
        )
        self.assertFalse(result["passed"])
        self.assertIn("lint", result["summary"])

    def test_no_required_checks_is_not_evidence(self):
        self.assertFalse(
            check_required(Mock(), SimpleNamespace(required_checks=[]), "a" * 40)["passed"]
        )


class ContextTests(TestCase):
    def fixture(self):
        repo = SimpleNamespace(
            full_name="acme/app",
            instructions="",
            sheet_config={},
            sheet_credential=None,
        )
        job = SimpleNamespace(pr=SimpleNamespace(number=1, repository=repo), head_sha="a" * 40)
        github = Mock()
        github.pull.return_value = {
            "number": 1,
            "title": "Change",
            "body": "Fixes #2",
            "head": {"sha": job.head_sha},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        github.pages.side_effect = lambda path, **kw: (
            [
                {
                    "filename": "app.py",
                    "status": "modified",
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ]
            if "/files" in path
            else []
        )

        def get(path):
            if "/git/trees/" in path:
                return {
                    "truncated": False,
                    "tree": [
                        {"path": "app.py", "type": "blob"},
                        {"path": "docs/guide.md", "type": "blob"},
                    ],
                }
            if "/contents/" in path:
                return {
                    "encoding": "base64",
                    "type": "file",
                    "content": base64.b64encode(b"new").decode(),
                }
            if path == "/issues/2":
                return {"number": 2, "body": "Acceptance criteria", "title": "Task"}
            raise AssertionError(path)

        github.get.side_effect = get
        github.graphql.return_value = {
            "repository": {
                "pullRequest": {"reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
            }
        }
        return github, job

    def test_graph_selected_source_is_read_at_reviewed_head(self):
        github, job = self.fixture()
        with patch(
            "core.graph.collect",
            return_value={"paths": ["docs/guide.md"], "head_sha": job.head_sha},
        ):
            result = build_context(github, job)
        self.assertTrue(result["complete"], result)
        self.assertIn("docs/guide.md", [item["path"] for item in result["sources"]])
        self.assertEqual(result["graph"]["head_sha"], job.head_sha)
        self.assertTrue(
            any(
                "docs/guide.md?ref=" + job.head_sha in call.args[0]
                for call in github.get.call_args_list
            )
        )

    def test_graph_failure_blocks_clearance(self):
        github, job = self.fixture()
        with patch(
            "core.graph.collect",
            side_effect=IntegrationError(
                "Code graph unavailable or incomplete; no model review was started."
            ),
        ):
            result = build_context(github, job)
        self.assertFalse(result["complete"])
        self.assertIn("Code graph", result["reason"])

    def test_graph_cannot_select_file_outside_current_tree(self):
        github, job = self.fixture()
        with patch("core.graph.collect", return_value={"paths": ["missing.py"]}):
            self.assertFalse(build_context(github, job)["complete"])

    def test_context_pins_source_and_includes_issue(self):
        github, job = self.fixture()
        result = build_context(github, job)
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["issues"][0]["body"], "Acceptance criteria")
        for call in github.get.call_args_list:
            if "/contents/" in call.args[0]:
                self.assertIn("ref=" + job.head_sha, call.args[0])
        self.assertEqual(result["files"][0]["filename"], "app.py")

    def test_head_change_prevents_clearance(self):
        github, job = self.fixture()
        github.pull.return_value["head"]["sha"] = "c" * 40
        self.assertFalse(build_context(github, job)["complete"])

    def test_changed_files_are_not_capped_at_150(self):
        github, job = self.fixture()
        github.pull.return_value["changed_files"] = 175
        github.pages.side_effect = lambda path, **kw: (
            [
                {
                    "filename": f"file{i}.py",
                    "status": "modified",
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
                for i in range(175)
            ]
            if "/files" in path
            else []
        )
        result = build_context(github, job)
        self.assertTrue(result["complete"], result["reason"])
        self.assertEqual(len(result["files"]), 175)

    def test_relevant_documents_are_not_capped_at_150(self):
        github, job = self.fixture()
        original = github.get.side_effect
        github.get.side_effect = lambda path: (
            {
                "truncated": False,
                "tree": [{"path": f"docs/app-{i}.md", "type": "blob"} for i in range(175)],
            }
            if "/git/trees/" in path
            else original(path)
        )
        result = build_context(github, job)
        self.assertTrue(result["complete"], result["reason"])
        self.assertEqual(len(result["docs"]), 175)

    def test_added_text_without_api_patch_reconstructs_complete_diff(self):
        github, job = self.fixture()
        github.pages.side_effect = lambda path, **kw: (
            [
                {
                    "filename": "app.py",
                    "status": "added",
                    "additions": 0,
                    "deletions": 0,
                    "changes": 0,
                }
            ]
            if "/files" in path
            else []
        )
        result = build_context(github, job)
        self.assertTrue(result["complete"], result["reason"])
        self.assertEqual(result["files"][0]["patch"], "@@ -0,0 +1,1 @@\n+new\n")
        self.assertEqual(result["files"][0]["valid_lines"], [1])
        self.assertTrue(result["files"][0]["patch_reconstructed"])

    def test_added_patchless_binary_is_not_accepted_as_text(self):
        github, job = self.fixture()
        github.pages.side_effect = lambda path, **kw: (
            [
                {
                    "filename": "app.py",
                    "status": "added",
                    "additions": 0,
                    "deletions": 0,
                    "changes": 0,
                }
            ]
            if "/files" in path
            else []
        )
        original = github.get.side_effect
        github.get.side_effect = lambda path: (
            {"type": "file", "encoding": "base64", "content": base64.b64encode(b"a\0b").decode()}
            if "/contents/" in path
            else original(path)
        )
        result = build_context(github, job)
        self.assertFalse(result["complete"])
        self.assertIn("not reviewable text", result["reason"])

    def rename_fixture(self, **overrides):
        github, job = self.fixture()
        file = {
            "filename": "app.py",
            "previous_filename": "old/app.py",
            "status": "renamed",
            "additions": 0,
            "deletions": 0,
            "changes": 0,
            **overrides,
        }
        github.pages.side_effect = lambda path, **kw: [file] if "/files" in path else []
        original = github.get.side_effect
        github.get.side_effect = lambda path: (
            {"merge_base_commit": {"sha": "c" * 40}}
            if path.startswith("/compare/")
            else original(path)
        )
        return github, job

    def test_unchanged_rename_without_patch_is_complete_and_has_no_inline_lines(self):
        github, job = self.rename_fixture()
        result = build_context(github, job)
        self.assertTrue(result["complete"], result["reason"])
        entry = result["files"][0]
        self.assertEqual(entry["previous_filename"], "old/app.py")
        self.assertTrue(entry["rename_only"])
        self.assertEqual(entry["patch"], "")
        self.assertEqual(entry["valid_lines"], [])
        self.assertEqual(entry["content"], "new")
        github.get.assert_any_call("/contents/old/app.py?ref=" + "c" * 40)
        github.get.assert_any_call("/contents/app.py?ref=" + "a" * 40)

    def test_patchless_rename_with_different_content_is_blocked(self):
        github, job = self.rename_fixture()
        original = github.get.side_effect
        github.get.side_effect = lambda path: (
            {"type": "file", "encoding": "base64", "content": base64.b64encode(b"old").decode()}
            if path.startswith("/contents/old/")
            else original(path)
        )
        result = build_context(github, job)
        self.assertFalse(result["complete"])
        self.assertIn("no reviewable diff", result["reason"])

    def test_patchless_non_rename_or_incomplete_metadata_stays_blocked(self):
        for metadata in (
            {"status": "modified"},
            {"additions": 1},
            {"changes": None},
            {"previous_filename": None},
            {"previous_filename": "../app.py"},
        ):
            with self.subTest(metadata=metadata):
                github, job = self.rename_fixture(**metadata)
                self.assertFalse(build_context(github, job)["complete"])

    def test_edited_rename_retains_both_paths_and_real_diff(self):
        github, job = self.rename_fixture(
            additions=1, deletions=1, changes=2, patch="@@ -1 +1 @@\n-old\n+new"
        )
        result = build_context(github, job)
        self.assertTrue(result["complete"], result["reason"])
        self.assertEqual(result["files"][0]["previous_filename"], "old/app.py")
        self.assertNotIn("rename_only", result["files"][0])

    def test_size_limit_prevents_clearance(self):
        github, job = self.fixture()
        with patch("core.context.MAX_CONTEXT_BYTES", 20):
            self.assertFalse(build_context(github, job)["complete"])

    def test_nested_thread_and_comment_pagination(self):
        from core.context import threads

        github = Mock()

        def page(nodes, cursor=None):
            return {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
            }

        thread = {
            "id": "thread-1",
            "isResolved": False,
            "path": "app.py",
            "line": 2,
            "comments": page([{"id": "c1", "databaseId": 1, "body": "First"}], "comments-next"),
        }
        github.graphql.side_effect = [
            {"repository": {"pullRequest": {"reviewThreads": page([thread], "threads-next")}}},
            {"node": {"comments": page([{"id": "c2", "databaseId": 2, "body": "Reply"}])}},
            {
                "repository": {
                    "pullRequest": {"reviewThreads": page([{"id": "resolved", "isResolved": True}])}
                }
            },
        ]
        result = threads(github, "acme/app", 1)
        self.assertEqual([c["databaseId"] for c in result[0]["comments"]], [1, 2])
        self.assertEqual(len(result), 1)

    def test_cross_repo_reference_never_read(self):
        from core.context import _references

        self.assertEqual(
            _references(
                "Closes other/repo#3 and https://github.com/other/repo/pull/4; see acme/app#5 and #6",
                "acme/app",
            ),
            {5, 6},
        )

    def test_truncated_tree_blocks_clearance(self):
        github, job = self.fixture()
        original = github.get.side_effect
        github.get.side_effect = lambda path: (
            {"truncated": True} if "/git/trees/" in path else original(path)
        )
        self.assertFalse(build_context(github, job)["complete"])

    def test_unrelated_documents_not_fetched(self):
        github, job = self.fixture()
        original = github.get.side_effect
        github.get.side_effect = lambda path: (
            {
                "truncated": False,
                "tree": [
                    {"path": "app.py", "type": "blob"},
                    *({"path": f"docs/irrelevant-{i}.md", "type": "blob"} for i in range(200)),
                ],
            }
            if "/git/trees/" in path
            else original(path)
        )
        result = build_context(github, job)
        self.assertTrue(result["complete"], result["reason"])
        self.assertEqual(result["docs"], [])

    def test_linked_issue_references_related_pr(self):
        github, job = self.fixture()
        original = github.get.side_effect

        def get(path):
            if path == "/issues/2":
                return {"number": 2, "body": "Frontend depends on #3", "title": "Task"}
            if path == "/issues/3":
                return {"number": 3, "pull_request": {}}
            return original(path)

        github.get.side_effect = get
        current = github.pull.return_value
        github.pull.side_effect = lambda number: (
            current if number == 1 else {"number": 3, "head": {"sha": "c" * 40}}
        )
        result = build_context(github, job)
        self.assertTrue(result["complete"], result["reason"])
        self.assertEqual([pr["number"] for pr in result["related_prs"]], [3])

    def test_related_issue_does_not_recursively_expand_repository_backlog(self):
        github, job = self.fixture()
        original = github.get.side_effect

        def get(path):
            if path == "/issues/2":
                return {"number": 2, "body": "Depends on #3; backend PR #4", "title": "Task"}
            if path == "/issues/3":
                return {"number": 3, "body": "Backlog " + " ".join(f"#{n}" for n in range(5, 80))}
            if path == "/issues/4":
                return {"number": 4, "pull_request": {}}
            return original(path)

        github.get.side_effect = get
        current = github.pull.return_value
        github.pull.side_effect = lambda number: (
            current if number == 1 else {"number": number, "head": {"sha": "c" * 40}}
        )
        result = build_context(github, job)
        self.assertTrue(result["complete"], result["reason"])
        self.assertEqual([issue["number"] for issue in result["issues"]], [2, 3])
        self.assertEqual([pr["number"] for pr in result["related_prs"]], [4])
        self.assertNotIn("/issues/5", [call.args[0] for call in github.get.call_args_list])

    def test_truncated_diff_blocks_clearance(self):
        github, job = self.fixture()
        github.pages.side_effect = lambda path, **kw: (
            [
                {
                    "filename": "app.py",
                    "status": "modified",
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                    "additions": 2,
                    "deletions": 1,
                }
            ]
            if "/files" in path
            else []
        )
        self.assertFalse(build_context(github, job)["complete"])

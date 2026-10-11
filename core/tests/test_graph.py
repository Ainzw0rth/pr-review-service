from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings


class GraphTests(SimpleTestCase):
    def test_disabled_does_not_download_or_start_container(self):
        from core.graph import collect

        github = Mock()
        with override_settings(GRAPH_REPOSITORIES=[]):
            self.assertIsNone(collect(github, "org/repo", "a" * 40, "b" * 40, []))
        github.archive.assert_not_called()

    @override_settings(GRAPH_REPOSITORIES=["org/repo"])
    def test_output_must_match_head_and_contain_safe_paths(self):
        from core.github import IntegrationError
        from core.graph import validate

        data = {
            "version": "crg-2.3.9-adapter-1",
            "repository": "org/repo",
            "head_sha": "a" * 40,
            "paths": ["src/x.py"],
            "depth": 2,
            "snapshots": [],
        }
        self.assertEqual(validate(data, "org/repo", "a" * 40), data)
        with self.assertRaises(IntegrationError):
            validate(data, "org/repo", "b" * 40)
        for path in ["../secret", "/etc/passwd", "x/../secret", "x\\secret"]:
            with self.subTest(path=path), self.assertRaises(IntegrationError):
                validate({**data, "paths": [path]}, "org/repo", "a" * 40)

    @override_settings(GRAPH_REPOSITORIES=["org/repo"])
    def test_base_then_head_and_old_filename_are_supplied(self):
        from core.graph import collect

        github = Mock()
        github.archive.return_value = b"tar"
        with patch(
            "core.graph.execute",
            return_value={
                "version": "crg-2.3.9-adapter-1",
                "repository": "org/repo",
                "head_sha": "b" * 40,
                "paths": [],
                "depth": 2,
                "snapshots": [],
            },
        ) as execute:
            collect(
                github,
                "org/repo",
                "b" * 40,
                "a" * 40,
                [{"filename": "new.py", "previous_filename": "old.py"}],
            )
        request = execute.call_args.args[0]
        self.assertEqual([s["sha"] for s in request["snapshots"]], ["a" * 40, "b" * 40])
        self.assertEqual(set(request["changed"]), {"new.py", "old.py"})

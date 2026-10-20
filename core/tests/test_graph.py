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

    @override_settings(GRAPH_REPOSITORIES=["org/repo"])
    def test_collection_reports_real_build_metrics(self):
        from core.graph import VERSION, collect

        progress = Mock()
        github = Mock()
        github.archive.return_value = b"archive"
        result = {
            "version": VERSION,
            "repository": "org/repo",
            "head_sha": "b" * 40,
            "depth": 2,
            "paths": ["x.py"],
            "snapshots": [
                {"mode": "full", "files_parsed": 4, "indexed_files": 4},
                {"mode": "incremental", "files_parsed": 0, "indexed_files": 4},
            ],
        }
        with patch("core.graph.execute", return_value=result):
            collect(github, "org/repo", "b" * 40, "a" * 40, [], progress=progress)
        progress.assert_any_call("graph_full", count=4)
        progress.assert_any_call("graph_incremental", count=0)
        progress.assert_any_call("graph_indexed", count=4)
        progress.assert_any_call("graph_impacted", count=1)

    def test_native_graph_uses_slot_labels_and_immutable_id_and_removes(self):
        import json
        import tempfile
        from pathlib import Path

        from core.graph import execute
        from core.tests.docker_fake import DockerFake

        docker = DockerFake()
        process = Mock(returncode=0)
        process.poll.return_value = 0

        def spawn(args, **kwargs):
            kwargs["stdout"].write(json.dumps({"paths": []}).encode())
            self.assertRegex(args[-1], "^[a-f0-9]{64}$")
            return process

        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(
                DATA_DIR=Path(directory),
                GRAPH_RUNNER_IMAGE="sha256:" + "a" * 64,
                GRAPH_CACHE_VOLUME="test-cache",
                GRAPH_TIMEOUT_SECONDS=10,
            ),
            patch("core.runner._docker", side_effect=docker),
            patch("core.graph.subprocess.Popen", side_effect=spawn),
        ):
            self.assertEqual(execute({}), {"paths": []})
        self.assertFalse(docker.objects)
        create = next(command for command in docker.commands if command[0] == "create")
        for flag in (
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=10001:10001",
        ):
            self.assertIn(flag, create)
        self.assertTrue(
            any(flag.startswith("--label=auto-reviewer.generation=") for flag in create)
        )

    def test_graph_wait_cancellation_starts_no_container(self):
        import tempfile
        from pathlib import Path

        from core.capacity import permit
        from core.graph import execute
        from core.providers import ReviewCancelled

        cancelled = []
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(DATA_DIR=Path(directory)),
        ):
            with permit("graph", 1, lambda: False), patch("core.runner._docker") as docker:
                with self.assertRaises(ReviewCancelled):
                    execute(
                        {},
                        lambda: bool(cancelled),
                        on_capacity=lambda waiting: cancelled.append(waiting),
                    )
                docker.assert_not_called()

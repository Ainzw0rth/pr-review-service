import io
import json
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from core import runner


def archive(entries=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for name, kind in entries or [("repo-sha/a.py", tarfile.REGTYPE)]:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "/etc/passwd" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            member.size = 1 if kind == tarfile.REGTYPE else 0
            tar.addfile(member, io.BytesIO(b"x") if member.size else None)
    return stream.getvalue()


class RunnerTests(TestCase):
    def test_archive_rejects_traversal_links_devices_and_multiple_roots(self):
        cases = [
            [("../outside", tarfile.REGTYPE)],
            [("repo/../../outside", tarfile.REGTYPE)],
            [("/outside", tarfile.REGTYPE)],
            [("repo/link", tarfile.SYMTYPE)],
            [("repo/link", tarfile.LNKTYPE)],
            [("repo/dev", tarfile.CHRTYPE)],
            [("repo/a", tarfile.REGTYPE), ("other/b", tarfile.REGTYPE)],
        ]
        for entries in cases:
            with (
                self.subTest(entries=entries),
                tempfile.TemporaryDirectory() as directory,
                self.assertRaises(runner.RunnerError),
            ):
                runner.extract_archive(archive(entries), Path(directory))

    def test_archive_limits_and_root_strip(self):
        with tempfile.TemporaryDirectory() as directory:
            runner.extract_archive(archive(), Path(directory))
            self.assertEqual((Path(directory) / "a.py").read_bytes(), b"x")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(runner, "MAX_ARCHIVE_BYTES", 1),
            self.assertRaises(runner.RunnerError),
        ):
            runner.extract_archive(archive(), Path(directory))

    def repo(self):
        return SimpleNamespace(
            test_image="example/tests@sha256:" + "a" * 64,
            test_command=["python", "-m", "unittest"],
        )

    def test_unpinned_image_rejected_before_docker(self):
        repository = self.repo()
        repository.test_image = "python:latest"
        with (
            patch.object(runner, "_docker") as docker,
            self.assertRaises(runner.RunnerError),
        ):
            runner.run_tests(repository, archive(), lambda: False)
        docker.assert_not_called()

    def test_hardened_container_executes_and_is_removed(self):
        commands = []

        def docker(args, **kwargs):
            commands.append(args)
            if args[:2] == ["image", "inspect"]:
                return json.dumps([{"Config": {"Volumes": None}}])
            if args[0] == "inspect":
                return json.dumps(
                    {"Running": False, "ExitCode": 0, "OOMKilled": False, "Error": ""}
                )
            return ""

        process = MagicMock()
        process.poll.return_value = 0
        with (
            patch.object(runner, "_docker", side_effect=docker),
            patch.object(runner.subprocess, "Popen", return_value=process) as spawn,
        ):
            result = runner.run_tests(self.repo(), archive(), lambda: False)
        self.assertTrue(result["passed"])
        self.assertIn("-ai", spawn.call_args.args[0])
        self.assertFalse(any(command[0] == "cp" for command in commands))
        create = next(command for command in commands if command[0] == "create")
        for flag in [
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network=none",
            "--user=65534:65534",
            "--pids-limit=128",
            "--memory=1g",
            "--cpus=1",
            "--log-driver=none",
        ]:
            self.assertIn(flag, create)
        self.assertFalse(any("docker.sock" in arg for arg in create))
        self.assertEqual(commands[-1][:3], ["rm", "-f", "-v"])

    def test_cancellation_removes_container(self):
        started = False
        commands = []

        def docker(args, **kwargs):
            nonlocal started
            commands.append(args)
            if args[:2] == ["image", "inspect"]:
                return json.dumps([{"Config": {"Volumes": None}}])
            return ""

        def spawn(*args, **kwargs):
            nonlocal started
            started = True
            return MagicMock()

        with (
            patch.object(runner, "_docker", side_effect=docker),
            patch.object(runner.subprocess, "Popen", side_effect=spawn),
            self.assertRaises(runner.ReviewCancelled),
        ):
            runner.run_tests(self.repo(), archive(), lambda: started)
        self.assertEqual(commands[-1][:3], ["rm", "-f", "-v"])

    def test_image_declared_volumes_fail_closed(self):
        with (
            patch.object(
                runner,
                "_docker",
                return_value=json.dumps([{"Config": {"Volumes": {"/data": {}}}}]),
            ),
            self.assertRaises(runner.RunnerError),
        ):
            runner.run_tests(self.repo(), archive(), lambda: False)

    def test_duplicate_and_expanded_limits_rejected(self):
        for entries in [
            [("repo/a", tarfile.REGTYPE), ("repo/a", tarfile.REGTYPE)],
            [("repo/a", tarfile.REGTYPE), ("repo/b", tarfile.REGTYPE)],
        ]:
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.object(runner, "MAX_EXTRACTED_BYTES", 1),
                self.assertRaises(runner.RunnerError),
            ):
                runner.extract_archive(archive(entries), Path(directory))

    def test_failed_tests_do_not_pass(self):
        def docker(args, **kwargs):
            if args[:2] == ["image", "inspect"]:
                return json.dumps([{"Config": {"Volumes": None}}])
            if args[0] == "inspect":
                return json.dumps({"Running": False, "ExitCode": 1, "OOMKilled": False})
            return ""

        process = MagicMock()
        process.poll.return_value = 1
        with (
            patch.object(runner, "_docker", side_effect=docker),
            patch.object(runner.subprocess, "Popen", return_value=process),
        ):
            result = runner.run_tests(self.repo(), archive(), lambda: False)
        self.assertFalse(result["passed"])

    def test_uncompressed_stream_limit_also_bounds_tar_metadata(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(runner, "MAX_TAR_BYTES", 100),
            self.assertRaises(runner.RunnerError),
        ):
            runner.extract_archive(archive(), Path(directory))

    def test_orphan_cleanup_selects_only_owned_label(self):
        with patch.object(runner, "_docker", side_effect=["a" * 64 + "\n", ""]) as docker:
            runner.cleanup_orphans()
        self.assertEqual(
            docker.call_args_list[0].args[0],
            ["ps", "-aq", "--filter=label=auto-reviewer.test=true"],
        )
        self.assertEqual(docker.call_args_list[1].args[0], ["rm", "-f", "-v", "a" * 64])

    def test_orphan_cleanup_rejects_non_ids(self):
        with (
            patch.object(runner, "_docker", return_value="--all\n") as docker,
            self.assertRaises(runner.RunnerError),
        ):
            runner.cleanup_orphans()
        self.assertEqual(docker.call_count, 1)

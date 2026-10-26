"""Worker installer tests: no Docker mutation, network access or provider login."""

import argparse
import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import setup_worker as setup


class WorkerSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state"
        setup.private_directory(self.state)
        self.config = {
            "coordinator": "https://review.example",
            "worker_id": "4a4961d3-e401-48b6-b951-02a1c8d5bdab",
            "token": "never-print-this-worker-secret",
            "state_dir": "/state",
            "slots": 1,
            "images": {},
            "auth_volumes": {"codex_cli": "worker-test-codex"},
            "account_kinds": ["codex_cli"],
            "test_images": [],
        }

    def args(self, **kwargs):
        values = dict(
            state_dir=str(self.state),
            resume=False,
            noninteractive=False,
            skip_login=False,
            no_start=True,
        )
        values.update(kwargs)
        return argparse.Namespace(**values)

    def test_running_agent_blocks_resume_before_build_login_or_compose(self):
        setup.private_write(self.state / "worker.json", self.config)
        with (
            (self.state / "agent.lock").open("a") as running,
            patch.object(setup, "preflight") as preflight,
            patch.object(setup, "build_images", return_value="sha256:" + "a" * 64) as build,
            patch.object(setup, "login") as login,
            patch.object(setup, "compose") as compose,
        ):
            setup.fcntl.flock(running, setup.fcntl.LOCK_EX | setup.fcntl.LOCK_NB)
            with self.assertRaisesRegex(setup.SetupError, "Drain.*idle.*stop.*resume"):
                setup.install(self.args(resume=True))
        preflight.assert_not_called()
        build.assert_not_called()
        login.assert_not_called()
        compose.assert_not_called()
        self.assertEqual(setup.load_config(self.state / "worker.json"), self.config)

    def test_installer_holds_agent_lock_until_immediately_before_start(self):
        setup.private_write(self.state / "worker.json", self.config)
        runtime = "sha256:" + "a" * 64
        calls = []

        def verify_lock(*args):
            calls.append(args)
            with (self.state / "agent.lock").open("a") as other:
                if "up" in args:
                    setup.fcntl.flock(other, setup.fcntl.LOCK_EX | setup.fcntl.LOCK_NB)
                    self.assertTrue((self.state / "runtime.json").exists())
                else:
                    with self.assertRaises(BlockingIOError):
                        setup.fcntl.flock(other, setup.fcntl.LOCK_EX | setup.fcntl.LOCK_NB)
            return runtime

        with (
            patch.object(setup, "preflight", side_effect=verify_lock),
            patch.object(setup, "build_images", side_effect=verify_lock),
            patch.object(setup, "login", side_effect=verify_lock),
            patch.object(setup, "compose", side_effect=verify_lock),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            setup.install(self.args(resume=True, no_start=False))
        self.assertTrue(any("--check-config" in args for args in calls))
        self.assertTrue(any("up" in args for args in calls))
        self.assertEqual(stat.S_IMODE((self.state / "agent.lock").stat().st_mode), 0o600)

    def test_worker_name_respects_registry_limit_before_enrollment(self):
        with (
            patch("builtins.input", side_effect=["https://review.example", "x" * 81]),
            patch.object(setup, "enroll") as enroll,
            self.assertRaisesRegex(setup.SetupError, "1–80"),
        ):
            setup.install(self.args())
        enroll.assert_not_called()

    def test_http_requests_identify_the_worker_application(self):
        response = Mock()
        response.read.return_value = b'{"status":"ok"}'
        opener = Mock()
        opener.open.return_value = contextlib.nullcontext(response)
        with patch.object(setup.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(
                setup.request_json("https://review.example/healthz/"), {"status": "ok"}
            )
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), "auto-reviewer-worker/1")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 20)

    def test_https_origin_rejects_credentials_paths_and_insecure_urls(self):
        for value in [
            "http://host",
            "https://user:secret@host",
            "https://host/path",
            "https://host/?token=secret",
            "https://host/#frag",
            "https://host:bad",
            "https://host\n",
        ]:
            with self.subTest(value=value), self.assertRaises(setup.SetupError):
                setup.coordinator_origin(value)
        self.assertEqual(setup.coordinator_origin("https://host:8443/"), "https://host:8443")

    def test_private_identity_exclusive_and_persistent(self):
        path = self.state / "worker.json"
        setup.private_write(path, self.config, create=True)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        with self.assertRaises(FileExistsError):
            setup.private_write(path, {"token": "replacement"}, create=True)
        self.assertEqual(setup.load_config(path), self.config)
        path.chmod(0o644)
        with self.assertRaises(setup.SetupError):
            setup.load_config(path)

    def test_rejects_shared_or_symlink_state(self):
        self.state.chmod(0o755)
        with self.assertRaises(setup.SetupError):
            setup.private_directory(self.state)
        link = Path(self.temporary.name) / "link"
        link.symlink_to(self.state)
        with self.assertRaises(setup.SetupError):
            setup.private_directory(link)

    def test_no_reenrollment_or_overwrite_without_resume(self):
        setup.private_write(self.state / "worker.json", self.config)
        with patch.object(setup, "enroll") as enroll, self.assertRaises(setup.SetupError):
            setup.install(self.args())
        enroll.assert_not_called()

    def test_enrollment_persisted_before_build_failure(self):
        with (
            patch("builtins.input", side_effect=["https://review.example", "worker", "codex"]),
            patch.object(setup.getpass, "getpass", return_value="secret-pairing-code"),
            patch.object(setup, "preflight"),
            patch.object(setup, "request_json", return_value={"ok": True}),
            patch.object(setup, "enroll", return_value=self.config),
            patch.object(setup, "build_images", side_effect=setup.SetupError("build failed")),
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaises(setup.SetupError),
        ):
            setup.install(self.args())
        self.assertEqual(setup.load_config(self.state / "worker.json"), self.config)
        self.assertNotIn(self.config["token"], output.getvalue())
        self.assertNotIn("secret-pairing-code", output.getvalue())

    def test_resume_preserves_identity_and_completed_login(self):
        setup.private_write(self.state / "worker.json", self.config)
        setup.private_write(self.state / "setup.json", {"logged_in": ["codex_cli"]})
        with (
            patch.object(setup, "preflight"),
            patch.object(setup, "enroll") as enroll,
            patch.object(setup, "build_images", return_value="sha256:" + "a" * 64),
            patch.object(setup, "login") as login,
            patch.object(setup, "compose") as compose,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            setup.install(self.args(resume=True, noninteractive=True))
        enroll.assert_not_called()
        login.assert_not_called()
        self.assertIn("--check-config", compose.call_args.args)
        self.assertEqual(setup.load_config(self.state / "worker.json"), self.config)

    def test_automation_never_initiates_provider_login(self):
        setup.private_write(self.state / "worker.json", self.config)
        with (
            patch.object(setup, "preflight"),
            patch.object(setup, "build_images", return_value="sha256:" + "a" * 64),
            patch.object(setup, "login") as login,
            self.assertRaises(setup.SetupError),
        ):
            setup.install(self.args(resume=True, noninteractive=True))
        login.assert_not_called()

    def test_source_images_resolved_to_immutable_ids_before_login(self):
        with (
            patch.object(setup, "run", return_value="sha256:" + "a" * 64),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            runtime = setup.build_images(self.config, self.state / "worker.json")
        self.assertEqual(runtime, "sha256:" + "a" * 64)
        self.assertEqual(setup.load_config(self.state / "worker.json")["images"]["model"], runtime)
        with (
            patch.object(setup, "run", return_value="mutable:latest"),
            self.assertRaises(setup.SetupError),
        ):
            setup.image_id("tag")

    def test_compose_paths_do_not_depend_on_callers_working_directory(self):
        with (
            patch.object(setup, "run") as run,
            patch.object(setup.os, "stat", return_value=SimpleNamespace(st_gid=987)),
        ):
            setup.compose(self.config, self.state, "sha256:" + "a" * 64, "up", "-d")
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("-f") + 1], str(setup.ROOT / "compose.worker.yaml"))
        self.assertEqual(argv[argv.index("--project-directory") + 1], str(setup.ROOT))
        self.assertEqual(run.call_args.kwargs["env"]["WORKER_STATE_DIR"], str(self.state))
        self.assertEqual(run.call_args.kwargs["env"]["WORKER_UID"], str(os.getuid()))
        self.assertEqual(run.call_args.kwargs["env"]["WORKER_GID"], str(os.getgid()))
        self.assertEqual(
            run.call_args.kwargs["env"]["WORKER_DOCKER_GID"],
            "987",
        )
        self.assertNotIn(self.config["token"], str(run.call_args))

    def test_process_errors_do_not_echo_captured_output(self):
        failure = subprocess.CalledProcessError(1, ["docker"], output="secret", stderr="secret")
        with (
            patch.object(setup.subprocess, "run", side_effect=failure),
            self.assertRaises(setup.SetupError) as caught,
        ):
            setup.run(["docker", "info"])
        self.assertNotIn("secret", str(caught.exception))

    def test_enrollment_checks_version_and_identity(self):
        response = dict(version=1, worker_id=self.config["worker_id"], token=self.config["token"])
        with patch.object(setup, "request_json", return_value=response):
            config = setup.enroll("https://example", "worker", "code", ["codex_cli"])
        self.assertEqual(config["slots"], 1)
        self.assertEqual(config["token"], self.config["token"])
        response["version"] = 2
        with (
            patch.object(setup, "request_json", return_value=response),
            self.assertRaises(setup.SetupError),
        ):
            setup.enroll("https://example", "worker", "code", [])

    def test_preflight_checks_both_state_and_docker_storage(self):
        enough = Mock(free=setup.MIN_FREE_BYTES + 1)
        low = Mock(free=1)
        info = json.dumps(
            {"OSType": "linux", "Architecture": "x86_64", "DockerRootDir": str(self.state)}
        )
        with (
            patch.object(setup.platform, "system", return_value="Linux"),
            patch.object(setup.platform, "machine", return_value="x86_64"),
            patch.object(setup.shutil, "disk_usage", side_effect=[enough, low]),
            patch.object(setup, "run", side_effect=[info, "unix:///var/run/docker.sock"]),
            patch.dict(os.environ, {"DOCKER_HOST": ""}),
            self.assertRaisesRegex(setup.SetupError, "Docker image storage"),
        ):
            setup.preflight(self.state)

    def test_pairing_never_falls_back_to_visible_input(self):
        with (
            patch.object(setup.getpass, "getpass", side_effect=setup.getpass.GetPassWarning),
            self.assertRaisesRegex(setup.SetupError, "hidden input"),
        ):
            setup.pairing_code()

    def test_redirects_never_forward_pairing_secret(self):
        with self.assertRaises(setup.SetupError):
            setup.NoRedirect().redirect_request(None, None, 307, "", {}, "https://other")

    def test_source_resume_rebuilds_existing_model_and_preserves_identity(self):
        self.config["images"]["model"] = "sha256:" + "a" * 64
        original = json.loads(json.dumps(self.config))
        path = self.state / "worker.json"
        setup.private_write(path, self.config)
        with (
            patch.object(setup, "run") as run,
            patch.object(
                setup, "image_id", side_effect=["sha256:" + "b" * 64, "sha256:" + "c" * 64]
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            runtime = setup.build_images(self.config, path)
        self.assertEqual(runtime, "sha256:" + "c" * 64)
        builds = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(builds), 2)
        self.assertIn("cli/Dockerfile", builds[0])
        self.assertIn("execution/Dockerfile", builds[1])
        saved = setup.load_config(path)
        self.assertEqual(saved["images"]["model"], "sha256:" + "b" * 64)
        saved.pop("images")
        original.pop("images")
        self.assertEqual(saved, original)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def release_manifest(self):
        return {
            "version": "v1.2.3",
            "protocol": 1,
            "platform": "linux/amd64",
            "revision": "f" * 40,
            "images": {
                role: f"{setup.RELEASE_REGISTRY}/{component}@sha256:" + "a" * 64
                for role, component in {
                    "agent": "agent",
                    "model": "cli",
                    "coordinator": "coordinator",
                }.items()
            },
        }

    def test_release_manifest_strict_schema_and_matching_revision(self):
        manifest = self.release_manifest()
        path = self.state / "release.json"
        path.write_text(json.dumps(manifest))
        with patch.object(setup, "_checkout_revision", return_value="f" * 40):
            self.assertEqual(setup.load_release_manifest(path), manifest)
        with patch.object(setup, "_checkout_revision", return_value="b" * 40):
            with self.assertRaisesRegex(setup.SetupError, "revision does not match"):
                setup.load_release_manifest(path)

    def test_release_manifest_rejects_malformed_schema_and_image_trust_violations(self):
        path = self.state / "release.json"
        changes = [
            {"version": "latest"},
            {"version": "v1.2.3\n"},
            {"protocol": True},
            {"protocol": 2},
            {"platform": "linux/arm64"},
            {"revision": "short"},
            {"unexpected": True},
            {"images": []},
        ]
        for change in changes:
            with self.subTest(change=change):
                path.write_text(json.dumps({**self.release_manifest(), **change}))
                with self.assertRaises(setup.SetupError):
                    setup.load_release_manifest(path)
        for ref in [
            "ghcr.io/ainzw0rth/pr-review-service/agent:latest",
            "ghcr.io/attacker/project/agent@sha256:" + "a" * 64,
            "ghcr.io/ainzw0rth/pr-review-service/agent-extra@sha256:" + "a" * 64,
            "ghcr.io/ainzw0rth/pr-review-service/cli@sha256:" + "a" * 64,
            "sha256:" + "a" * 64,
            None,
        ]:
            with self.subTest(ref=ref):
                manifest = self.release_manifest()
                manifest["images"]["agent"] = ref
                path.write_text(json.dumps(manifest))
                with self.assertRaises(setup.SetupError):
                    setup.load_release_manifest(path)
        for raw in ["not json", "[]", '{"version":1,"version":2}', " " * 65537]:
            path.write_text(raw)
            with self.assertRaises(setup.SetupError):
                setup.load_release_manifest(path)

    def test_release_pulls_only_required_immutable_images_and_preserves_identity(self):
        manifest = self.release_manifest()
        before = json.loads(json.dumps(self.config))
        path = self.state / "worker.json"
        setup.private_write(path, self.config)
        with (
            patch.object(setup, "run") as run,
            patch.object(
                setup, "image_id", side_effect=["sha256:" + "b" * 64, "sha256:" + "c" * 64]
            ),
        ):
            runtime = setup.pull_release_images(self.config, path, manifest)
        self.assertEqual(runtime, "sha256:" + "b" * 64)
        self.assertEqual(
            run.call_args_list,
            [
                unittest.mock.call(
                    ["docker", "pull", "--platform=linux/amd64", manifest["images"]["agent"]]
                ),
                unittest.mock.call(
                    ["docker", "pull", "--platform=linux/amd64", manifest["images"]["model"]]
                ),
            ],
        )
        saved = setup.load_config(path)
        self.assertEqual(saved.pop("images"), {"model": "sha256:" + "c" * 64})
        before.pop("images")
        self.assertEqual(saved, before)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_release_no_provider_needs_only_agent_not_cli_or_coordinator(self):
        self.config["account_kinds"] = []
        self.config["auth_volumes"] = {}
        manifest = self.release_manifest()
        path = self.state / "worker.json"
        setup.private_write(path, self.config)
        with (
            patch.object(setup, "run") as run,
            patch.object(setup, "image_id", return_value="sha256:" + "b" * 64),
        ):
            setup.pull_release_images(self.config, path, manifest)
        run.assert_called_once_with(
            ["docker", "pull", "--platform=linux/amd64", manifest["images"]["agent"]]
        )
        self.assertEqual(setup.load_config(path)["images"], {})

    def test_release_install_skips_build_and_records_provenance(self):
        setup.private_write(self.state / "worker.json", self.config)
        manifest = self.release_manifest()
        with (
            patch.object(setup, "load_release_manifest", return_value=manifest) as load,
            patch.object(setup, "preflight"),
            patch.object(setup, "pull_release_images", return_value="sha256:" + "a" * 64) as pull,
            patch.object(setup, "build_images") as build,
            patch.object(setup, "compose"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            setup.install(self.args(resume=True, skip_login=True, release_manifest="release.json"))
        load.assert_called_once_with("release.json")
        pull.assert_called_once()
        build.assert_not_called()
        receipt = json.loads((self.state / "runtime.json").read_text())
        self.assertEqual(receipt["version"], manifest["version"])
        self.assertEqual(receipt["revision"], manifest["revision"])
        self.assertNotIn(self.config["token"], json.dumps(receipt))

    def test_bad_manifest_rejected_before_enrollment(self):
        with (
            patch.object(setup, "load_release_manifest", side_effect=setup.SetupError("invalid")),
            patch.object(setup, "enroll") as enroll,
            patch("builtins.input") as prompt,
        ):
            with self.assertRaises(setup.SetupError):
                setup.install(self.args(release_manifest="invalid.json"))
        enroll.assert_not_called()
        prompt.assert_not_called()

    def test_release_pull_failure_keeps_saved_configuration(self):
        manifest = self.release_manifest()
        path = self.state / "worker.json"
        setup.private_write(path, self.config)
        before = path.read_bytes()
        with (
            patch.object(setup, "run", side_effect=["", setup.SetupError("pull failed")]),
            patch.object(setup, "image_id", return_value="sha256:" + "b" * 64),
        ):
            with self.assertRaises(setup.SetupError):
                setup.pull_release_images(self.config, path, manifest)
        self.assertEqual(path.read_bytes(), before)


class WorkerModeRoutingTests(unittest.TestCase):
    def test_worker_mode_bypasses_standalone_env_and_bootstrap_checks(self):
        from scripts import setup as standalone

        with (
            patch.object(setup, "main", return_value=7) as worker,
            patch.object(standalone, "run") as coordinator_command,
            patch("sys.stdin.isatty", return_value=False),
        ):
            result = standalone.main(
                [
                    "--mode",
                    "worker",
                    "--resume",
                    "--state-dir",
                    "/data/worker",
                    "--noninteractive",
                ]
            )
        self.assertEqual(result, 7)
        worker.assert_called_once_with(
            ["--resume", "--state-dir", "/data/worker", "--noninteractive"]
        )
        coordinator_command.assert_not_called()

    def test_standalone_default_rejects_worker_options(self):
        from scripts import setup as standalone

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            standalone.main(["--state-dir", "/data/worker"])


if __name__ == "__main__":
    unittest.main()

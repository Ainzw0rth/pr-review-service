#!/usr/bin/env python3
"""Deploy reviewed migrations only after all writers stop; failures remain paused."""

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ("docker", "compose")
CONTROL = (*COMPOSE, "exec", "-T", "web", "python", "manage.py", "worker_control")


def command(*args):
    print("+ " + " ".join(args), flush=True)
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def deploy(*, run=command, sleep=time.sleep, journal=None):
    receipt = {}

    def phase(name, **values):
        receipt.update(phase=name, **values)
        if journal:
            temporary = journal.with_suffix(".partial")
            fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(receipt, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, journal)

    def candidate(*args):
        return run(*COMPOSE, "run", "--rm", "--no-deps", "web", "python", "manage.py", *args)

    def status():
        value = json.loads(run(*CONTROL, "status", "--json"))
        if (
            not isinstance(value, dict)
            or any(type(value.get(k)) is not bool for k in ("draining", "idle"))
            or type(value.get("active_jobs")) is not int
        ):
            raise RuntimeError("Worker does not provide a supported drain acknowledgment.")
        return value

    if status()["draining"]:
        raise RuntimeError("Intake already paused. Resolve the prior operation before deploying.")
    if run(
        "docker",
        "ps",
        "-q",
        "--filter",
        "label=com.docker.compose.project=auto-reviewer",
        "--filter",
        "label=com.docker.compose.oneoff=True",
    ):
        raise RuntimeError("One-off app containers running; stop every writer before deploying.")
    container = run(*COMPOSE, "ps", "-q", "worker")
    if not container:
        raise RuntimeError("Running worker not found; use the initial-deployment procedure.")
    old_image = run("docker", "inspect", "--format", "{{.Image}}", *container.split())
    images = set(old_image.splitlines())
    if len(images) != 1:
        raise RuntimeError("Mixed worker images; refusing deployment.")
    old_image = images.pop()
    rollback = "auto-reviewer:rollback-" + str(time.time_ns())
    run("docker", "image", "tag", old_image, rollback)
    print(f"Rollback image saved: {rollback}", flush=True)
    run(*COMPOSE, "build", "web")
    # No automatic schema changes: the old web/events processes remain online.
    run(*COMPOSE, "run", "--rm", "--no-deps", "web", "python", "manage.py", "migrate", "--plan")
    run(
        *COMPOSE,
        "run",
        "--rm",
        "--no-deps",
        "web",
        "python",
        "manage.py",
        "check",
        "--deploy",
        "--fail-level",
        "WARNING",
    )
    phase("preflight", previous_image=old_image, rollback_tag=rollback)
    paused = False
    try:
        # Mark intent before invocation: a connection can fail after the flag is set.
        paused = True
        phase("draining")
        run(*CONTROL, "drain")
        while True:
            state = status()
            if not state["draining"]:
                raise RuntimeError("Drain was removed by another operator; stopping deployment.")
            if state["idle"] and state["active_jobs"] == 0:
                previous_supervisors = set(state.get("supervisors", []))
                previous_worker = state.get("worker_token")
                if not previous_worker:
                    raise RuntimeError("Worker acknowledgment has no process identity.")
                break
            print("Waiting for active review and cleanup; new requests remain queued.", flush=True)
            sleep(5)
        candidate("deployment_control", "pause")
        phase("maintenance")
        # Infinite graceful stop: never turn an elapsed timeout into SIGKILL.
        # Durable ingress gate remains closed throughout schema changes/readiness.
        run(*COMPOSE, "stop", "--timeout", "-1", "web", "events", "maintenance", "worker")
        if run(
            *COMPOSE, "ps", "--status", "running", "-q", "web", "events", "maintenance", "worker"
        ):
            raise RuntimeError("Application writers still running; refusing backup/migration.")
        for label in ("auto-reviewer.cli", "auto-reviewer.test", "auto-reviewer.namespace"):
            if run("docker", "ps", "-q", "--filter", "label=" + label):
                raise RuntimeError(
                    "Native review containers remain running; do not start mixed-version workers."
                )
        if run("docker", "ps", "-q", "--filter", "name=^/pr-review-graph-[0-9a-f]{32}$"):
            raise RuntimeError(
                "Legacy unlabelled graph container still running; verify old owner quiescence."
            )
        phase("writers_stopped")
        backup = candidate("backup")
        phase("backed_up", backup=backup)
        print(backup, flush=True)
        phase("migrating")
        run(
            *COMPOSE,
            "run",
            "--rm",
            "--no-deps",
            "web",
            "python",
            "manage.py",
            "migrate",
            "--noinput",
        )
        run(
            *COMPOSE, "run", "--rm", "--no-deps", "web", "python", "manage.py", "migrate", "--check"
        )
        phase("migrated", migrations=candidate("showmigrations", "--list"))
        # Reassert durable DB drain before a new supervisor can claim anything.
        run(
            *COMPOSE,
            "run",
            "--rm",
            "--no-deps",
            "web",
            "python",
            "manage.py",
            "worker_control",
            "drain",
        )
        run(
            *COMPOSE,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "web",
            "worker",
        )
        run(
            *COMPOSE,
            "exec",
            "-T",
            "web",
            "python",
            "manage.py",
            "check",
            "--deploy",
            "--fail-level",
            "WARNING",
        )
        candidate("deployment_control", "check")
        phase("awaiting_readiness")
        # Do not resume until the replacement worker has actually entered its loop.
        for _ in range(24):
            state = status()
            if not state["draining"]:
                raise RuntimeError("Drain removed during rollout; inspect before continuing.")
            if (
                state["idle"]
                and state["active_jobs"] == 0
                and not previous_supervisors.intersection(state.get("supervisors", []))
                and state.get("worker_token")
                and state["worker_token"] != previous_worker
            ):
                break
            sleep(5)
        else:
            raise RuntimeError(
                "Replacement worker did not acknowledge readiness within 120 seconds."
            )
        run(*COMPOSE, "up", "-d", "--no-deps", "--wait", "events", "maintenance")
        run(*CONTROL, "resume")
        candidate("deployment_control", "open")
        phase("healthy")
        paused = False
        print("Deployment healthy. Intake resumed; queued reviews will run.", flush=True)
    finally:
        if paused:
            # A failure may occur after resume/open reached the server. Reassert pause,
            # never infer that a failed client call left server state unchanged.
            for action in (("deployment_control", "pause"), ("worker_control", "drain")):
                try:
                    candidate(*action)
                except Exception:
                    pass  # Original failure remains primary; recovery is manual.
            print(
                "Deployment did not finish. Intake was NOT automatically resumed.\n"
                "Inspect service health before resuming. No active review was forcibly stopped.\n"
                f"Previous image retained: {rollback}\n"
                "Do NOT blindly roll back the image after migrations. Keep all writers stopped\n"
                "until image/schema compatibility is established. Never restore a database\n"
                "without reconciling post-backup writes and external publication.\n",
                file=sys.stderr,
            )


def main():
    if len(sys.argv) != 1:
        print(__doc__)
        return 0 if sys.argv[1:] == ["--help"] else 2
    os.chdir(ROOT)
    (ROOT / "var").mkdir(mode=0o700, exist_ok=True)
    try:
        with (ROOT / "var" / "deploy.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deploy(journal=ROOT / "var" / "deployment.json")
    except (RuntimeError, ValueError, subprocess.CalledProcessError, BlockingIOError) as exc:
        print(f"Deployment stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Deployment interrupted; see recovery instructions above.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

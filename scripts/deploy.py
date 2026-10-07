#!/usr/bin/env python3
"""Deploy a schema-compatible app update after the worker acknowledges drain.

Does not push Git, change CLI images, apply migrations, or kill active reviews.
"""

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


def deploy(*, run=command, sleep=time.sleep):
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
    container = run(*COMPOSE, "ps", "-q", "worker")
    if not container:
        raise RuntimeError("Running worker not found; use the initial-deployment procedure.")
    old_image = run("docker", "inspect", "--format", "{{.Image}}", container)
    rollback = "auto-reviewer:rollback-" + str(time.time_ns())
    run("docker", "image", "tag", old_image, rollback)
    print(f"Rollback image saved: {rollback}", flush=True)
    run(*COMPOSE, "build", "web")
    # No automatic schema changes: the old web/events processes remain online.
    run(*COMPOSE, "run", "--rm", "--no-deps", "web", "python", "manage.py", "migrate", "--check")
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
    paused = False
    try:
        # Mark intent before invocation: a connection can fail after the flag is set.
        paused = True
        run(*CONTROL, "drain")
        while True:
            state = status()
            if not state["draining"]:
                raise RuntimeError("Drain was removed by another operator; stopping deployment.")
            if state["idle"] and state["active_jobs"] == 0:
                previous_worker = state.get("worker_token")
                if not previous_worker:
                    raise RuntimeError("Worker acknowledgment has no process identity.")
                break
            print("Waiting for active review and cleanup; new requests remain queued.", flush=True)
            sleep(5)
        print(run(*COMPOSE, "exec", "-T", "web", "python", "manage.py", "backup"), flush=True)
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
            "events",
            "maintenance",
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
        # Do not resume until the replacement worker has actually entered its loop.
        for _ in range(24):
            state = status()
            if not state["draining"]:
                raise RuntimeError("Drain removed during rollout; inspect before continuing.")
            if (
                state["idle"]
                and state.get("worker_token")
                and state["worker_token"] != previous_worker
            ):
                break
            sleep(5)
        else:
            raise RuntimeError(
                "Replacement worker did not acknowledge readiness within 120 seconds."
            )
        run(*CONTROL, "resume")
        paused = False
        print("Deployment healthy. Intake resumed; queued reviews will run.", flush=True)
    finally:
        if paused:
            print(
                "Deployment did not finish. Intake was NOT automatically resumed.\n"
                "Inspect service health before resuming. No active review was forcibly stopped.\n"
                f"If containers were replaced, rollback image: {rollback}\n"
                f"  docker image tag {rollback} auto-reviewer:local\n"
                "  docker compose up -d --no-deps --wait web worker events maintenance\n"
                "Once healthy (or if cancelled before replacement):\n"
                "  docker compose exec web python manage.py worker_control resume",
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
            deploy()
    except (RuntimeError, ValueError, subprocess.CalledProcessError, BlockingIOError) as exc:
        print(f"Deployment stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Deployment interrupted; see recovery instructions above.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

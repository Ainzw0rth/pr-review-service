"""Allowlisted operational events only; never model text, HTTP bodies or exceptions."""

import threading
import time

from django.db import DatabaseError, close_old_connections, connections
from django.utils import timezone

from .models import Job, JobActivity
from .workflow import current

MESSAGES = {
    "starting": "Worker started; verifying reviewer, labels and commit.",
    "context": "Fetching PR changes and review context from GitHub.",
    "files": "Changed-file context loaded",
    "threads": "Loading open review threads and PR discussion.",
    "related": "Loading linked issues and related pull requests.",
    "docs": "Relevant documentation loaded",
    "context_ready": "Review context collected.",
    "checks": "Checking required CI for the reviewed commit.",
    "checks_passed": "Required CI evidence passed.",
    "checks_blocked": "Required CI is missing, pending or unsuccessful; clearance blocked.",
    "tests": "Running configured tests in an isolated container.",
    "tests_passed": "Isolated tests passed.",
    "tests_failed": "Isolated tests did not pass.",
    "model": "Calling the selected model; awaiting provider response.",
    "unavailable": "Provider unavailable; checking the next configured connection.",
    "quota": "Provider quota exhausted; waiting for the next availability check.",
    "model_attention": "Provider response needs administrator attention.",
    "result": "Structured review response received and validated.",
    "findings": "Findings returned (details remain on GitHub)",
    "recheck": "Rechecking CI and review eligibility before publication.",
    "publishing": "Publishing review comments and workflow labels on GitHub.",
    "published": "GitHub publication finished.",
    "sheet": "Synchronizing the configured timeline status.",
    "sheet_attention": "Timeline synchronization needs administrator attention.",
    "ended": "Worker attempt ended; see the current job status.",
}


def message(event):
    text = MESSAGES.get(event.code, "Operational event.")
    return text if event.count is None else f"{text}: {event.count}."


def record(job, code, *, count=None):
    if code not in MESSAGES or (
        count is not None and (type(count) is not int or not 0 <= count <= 1_000_000)
    ):
        raise ValueError("Activity accepts only known event codes and bounded integer counts.")
    JobActivity.objects.create(job=job, code=code, count=count)
    # Bound repeated quota/retry activity independently of 90-day job retention.
    cutoff = list(job.activity.order_by("-id").values_list("id", flat=True)[299:300])
    if cutoff:
        job.activity.filter(id__lt=cutoff[0]).delete()
    Job.objects.filter(pk=job.pk, status="running").update(heartbeat_at=timezone.now())


class Progress:
    def __init__(self, job):
        self.job = job
        self.last_heartbeat = None
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        # A process heartbeat, not a claim that the remote model emitted tokens.
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def _heartbeat(self):
        try:
            while not self.stop.wait(10):
                try:
                    close_old_connections()
                    Job.objects.filter(pk=self.job.pk, status="running").update(
                        heartbeat_at=timezone.now()
                    )
                except DatabaseError:
                    # Leave the old timestamp visible; never fabricate a fresh heartbeat.
                    pass
        finally:
            connections.close_all()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)

    def __call__(self, code, *, count=None):
        record(self.job, code, count=count)

    def cancelled(self):
        if not current(self.job):
            return True
        now = time.monotonic()
        if self.last_heartbeat is None or now - self.last_heartbeat >= 10:
            Job.objects.filter(pk=self.job.pk, status="running").update(heartbeat_at=timezone.now())
            self.last_heartbeat = now
        return False

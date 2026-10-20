"""Operational messages and filtered public CLI output, never raw provider bodies."""

import threading
import time

from django.db import DatabaseError, close_old_connections, connections
from django.utils import timezone

from .claims import fenced, heartbeat
from .models import Job, JobActivity
from .workflow import current

MESSAGES = {
    "starting": "Worker started; verifying reviewer, labels and commit.",
    "context": "Fetching PR changes and review context from GitHub.",
    "files": "Changed-file context loaded",
    "threads": "Loading open review threads and PR discussion.",
    "related": "Loading linked issues and related pull requests.",
    "docs": "Relevant documentation loaded",
    "docs_started": "Gathering relevant documentation and supporting source.",
    "graph_started": "Updating isolated code graph for PR base and reviewed commit.",
    "graph_full": "Full graph build completed; files parsed",
    "graph_incremental": "Incremental graph update completed; files parsed",
    "graph_indexed": "Files in the pinned head graph",
    "graph_impacted": "Impacted source paths selected from base/head graphs",
    "graph_ready": "Commit-pinned code graph checked; loading impacted source files.",
    "context_ready": "Review context collected.",
    "context_failed": "Context collection stopped; see the operational note for the cause.",
    "checks": "Checking required CI for the reviewed commit.",
    "checks_waiting": "CI is still running; waiting for current-commit results before calling the model.",
    "checks_passed": "Required CI evidence passed.",
    "checks_blocked": "Required CI is missing, pending or unsuccessful; clearance blocked.",
    "tests": "Running configured tests in an isolated container.",
    "tests_passed": "Isolated tests passed.",
    "tests_failed": "Isolated tests did not pass.",
    "cli_starting": "Subscription CLI session starting.",
    "cli_reading": "CLI is requesting review evidence through the read-only tool.",
    "cli_evidence": "Complete evidence items supplied to CLI",
    "cli_receiving": "CLI output activity received; contents remain private.",
    "cli_reconnecting": "CLI connection interrupted; native client reconnecting.",
    "cli_completed": "CLI finished its response; validating review output.",
    "intent_started": "Synthesizing grounded intent from collected evidence.",
    "intent_ready": "Structured intent validated; evidence remains private.",
    "model": "Calling the selected model; awaiting provider response.",
    "unavailable": "Provider unavailable; checking the next configured connection.",
    "quota": "Provider quota exhausted; waiting for the next availability check.",
    "quota_fallback": "Subscription quota exhausted; checking the next available account.",
    "model_skipped": "Skipped model: its subscription account is waiting for quota availability.",
    "model_attention": "Provider response needs administrator attention.",
    "model_failed": "Model review failed validation or execution; trying the next configured model.",
    "model_incomplete": "Evidence coverage remained incomplete after recovery; trying the next configured model.",
    "model_refused": "Model declined the review; checking the next configured connection.",
    "models_exhausted": "All remaining model connections declined or were unavailable.",
    "result": "Structured review response received and validated.",
    "findings": "Findings returned (details remain on GitHub)",
    "recheck": "Rechecking CI and review eligibility before publication.",
    "delivery_retry": "GitHub delivery interrupted; rechecking receipts and retrying without a model call.",
    "delivery_resume": "Resuming saved review delivery; no model call required.",
    "delivery_attention": "Saved review retained; GitHub delivery needs attention.",
    "publishing": "Publishing review comments and workflow labels on GitHub.",
    "published": "GitHub publication finished.",
    "labels_attention": "Review published; workflow-label cleanup needs administrator attention.",
    "sheet": "Synchronizing the configured timeline status.",
    "sheet_attention": "Timeline synchronization needs administrator attention.",
    "journal_queued": "Final run summary queued for the private journal.",
    "journal_attention": "Private journal capture needs administrator attention; review outcome unchanged.",
    "ended": "Worker attempt ended; see the current job status.",
}


for _prefix, _title in {
    "summary": "PR summary",
    "inline": "Inline findings",
    "discussion": "Discussion replies",
    "related_pr": "Related-PR comments",
    "related_issue": "Related-issue comments",
    "labels": "Workflow labels",
}.items():
    MESSAGES[f"{_prefix}_started"] = f"{_title}: checking saved receipts and GitHub delivery."
    MESSAGES[f"{_prefix}_done"] = f"{_title}: delivery confirmed"
    if _prefix not in {"summary", "labels"}:
        MESSAGES[f"{_prefix}_unused"] = f"{_title}: assessed; no write needed"

for _phase, _title in {
    "code": "Code reviewer",
    "security": "Security reviewer",
    "tests": "Test reviewer",
    "synthesis": "Review adjudication",
}.items():
    MESSAGES[f"{_phase}_started"] = f"{_title}: examining commit-pinned evidence."
    MESSAGES[f"{_phase}_ready"] = (
        f"{_title}: structured assessment validated; content remains private."
    )
    MESSAGES[f"{_phase}_incomplete"] = (
        f"{_title}: assessment has coverage limitations; clearance remains blocked."
    )


def cli_progress(progress, data, state):
    if data.get("transport") not in {"claude_cli", "codex_cli"}:
        return
    stages = {
        "starting": "cli_starting",
        "reading_evidence": "cli_reading",
        "receiving": "cli_receiving",
        "reconnecting": "cli_reconnecting",
        "completed": "cli_completed",
    }
    stage = data.get("last_event")
    if stage in stages and state.get("stage") != stage:
        progress(stages[stage])
        state["stage"] = stage
    count = data.get("evidence_read")
    if type(count) is int and 0 <= count <= 1_000_000 and state.get("count") != count:
        progress("cli_evidence", count=count)
        state["count"] = count


def message(event):
    from .transcript import CODES

    if event.code in CODES and event.detail:
        return event.detail
    text = MESSAGES.get(event.code, "Operational event.")
    return text if event.count is None else f"{text}: {event.count}."


@fenced(terminal=True, silent=True)
def record(job, code, *, count=None, detail="", source=""):
    from .transcript import CODES

    if (detail or source) and (
        code not in CODES
        or not isinstance(detail, str)
        or len(detail) > 1000
        or not isinstance(source, str)
        or len(source) > 100
    ):
        raise ValueError("Invalid public CLI event.")
    if (
        code not in MESSAGES
        and code not in CODES
        or (count is not None and (type(count) is not int or not 0 <= count <= 1_000_000))
    ):
        raise ValueError("Activity accepts only known event codes and bounded integer counts.")
    event = JobActivity.objects.create(
        job=job, code=code, count=count, detail=detail, source=source
    )
    if code in {"files", "docs", "cli_evidence"}:
        # Preserve phase evidence for large PRs instead of filling history with counters.
        job.activity.filter(code=code).exclude(pk=event.pk).delete()
    if code.startswith("cli_"):
        old_cli = list(
            job.activity.filter(code__startswith="cli_")
            .order_by("-id")
            .values_list("id", flat=True)[199:200]
        )
        if old_cli:
            job.activity.filter(code__startswith="cli_", id__lt=old_cli[0]).delete()
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
                    if heartbeat(self.job):
                        from .claims import job_rows

                        job_rows(self.job).update(heartbeat_at=timezone.now())
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
            if heartbeat(self.job):
                from .claims import job_rows

                job_rows(self.job).update(heartbeat_at=timezone.now())
            self.last_heartbeat = now
        return False

"""Explicit CLI probe. Never enqueues work or writes to GitHub/Sheets."""

import json
from pathlib import Path
from types import SimpleNamespace

from django.core.management.base import BaseCommand, CommandError

from core.cli import review_cli
from core.providers import MAX_CLI_CONTEXT_BYTES, ProviderError


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("kind", choices=["codex_cli", "claude_cli"])
        parser.add_argument("model")
        parser.add_argument("--context", type=Path)
        parser.add_argument("--result", type=Path)
        parser.add_argument(
            "--activity",
            action="store_true",
            help="Print filtered public CLI commentary and tool metadata.",
        )

    def handle(self, *args, **options):
        context = {
            "task": "Connectivity probe only, no PR exists. Return complete false, blocking true, and empty findings, replies and related_comments."
        }
        if options["context"]:
            raw = options["context"].read_bytes()
            if len(raw) > MAX_CLI_CONTEXT_BYTES:
                raise CommandError("Context exceeds request-size limit.")
            context = json.loads(raw)
        previous = None

        def progress(data):
            nonlocal previous
            current = (data.get("events_received"), data.get("last_event"))
            if current != previous:
                self.stdout.write(json.dumps(data))
                self.stdout.flush()
                previous = current

        def activity(code, text):
            self.stdout.write(json.dumps({"cli_event": code, "text": text}))
            self.stdout.flush()

        try:
            result = review_cli(
                SimpleNamespace(kind=options["kind"], model=options["model"]),
                context,
                lambda: False,
                on_response=progress,
                on_activity=activity if options["activity"] else None,
            )
        except ProviderError as exc:
            raise CommandError(f"{exc.kind}: {exc}") from None
        if options["result"]:
            with options["result"].open("w") as target:
                options["result"].chmod(0o600)
                json.dump(result, target)
        self.stdout.write(
            json.dumps(
                {
                    "validated": True,
                    "complete": result["complete"],
                    "blocking": result["blocking"],
                    "findings": len(result["findings"]),
                    "publication": False,
                }
            )
        )

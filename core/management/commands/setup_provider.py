"""Register the first model connection; JSON arrives on stdin, never command arguments."""

import json
import sys

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Credential, Provider, Repository
from core.secrets import encrypt


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Check that initial setup is still safe, without reading credentials.",
        )

    def handle(self, *args, **options):
        if settings.LIVE_WRITES:
            raise CommandError("Initial provider setup is disabled while LIVE_WRITES is enabled.")
        if Repository.objects.exists():
            raise CommandError(
                "Repositories already exist. Finish provider changes using the manual guide and drain active reviews first."
            )
        if options["check"]:
            self.stdout.write("Initial setup is safe to continue.")
            return
        try:
            data = json.loads(sys.stdin.read(16385))
            kind, model, key = data["kind"], data["model"], data.get("key", "")
            if (
                kind not in dict(Provider._meta.get_field("kind").choices)
                or not isinstance(model, str)
                or not model.strip()
                or len(model) > 120
                or any(c.isspace() for c in model)
                or not isinstance(key, str)
                or len(key) > 8192
                or (kind.endswith("_cli") and key)
                or (not kind.endswith("_cli") and not key.strip())
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise CommandError(
                "Choose a valid provider/model and supply an API key only for API providers."
            ) from None
        with transaction.atomic():
            existing = list(Provider.objects.all())
            if existing:
                if (
                    len(existing) == 1
                    and existing[0].name == "Setup provider"
                    and existing[0].kind == kind
                    and existing[0].model == model
                    and existing[0].enabled
                ):
                    self.stdout.write("Provider already configured; no changes made.")
                    return
                raise CommandError(
                    "Providers already exist. Manage them in Workflow or admin; setup will not replace them."
                )
            credential = None
            if key:
                credential = Credential.objects.create(
                    name="Setup API key", encrypted_value=encrypt(key)
                )
            provider = Provider(
                name="Setup provider", kind=kind, model=model, credential=credential
            )
            try:
                provider.full_clean()
            except ValidationError:
                raise CommandError("Invalid provider configuration.") from None
            provider.save()
        self.stdout.write("Model connection saved. Account access must still be verified.")

"""Test one configured provider without enqueuing reviews or publishing to GitHub."""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from core.capacity import ActiveBudget, CapacityError
from core.models import Provider
from core.providers import ProviderError, review


class Command(BaseCommand):
    help = __doc__

    def handle(self, *args, **options):
        providers = list(Provider.objects.filter(enabled=True)[:2])
        if len(providers) != 1:
            raise CommandError("This initial-setup probe requires exactly one enabled provider.")
        provider = providers[0]
        if provider.kind.endswith("_cli"):
            call_command(
                "probe_cli", provider.kind, provider.model, stdout=self.stdout, stderr=self.stderr
            )
            return
        try:
            review(
                provider,
                {
                    "task": "Connectivity probe only, no PR exists. Return complete false, blocking true, and empty findings, replies and related_comments."
                },
                lambda: False,
                budget=ActiveBudget(90),
            )
        except (ProviderError, CapacityError):
            raise CommandError(
                "Model probe failed. Check API key, model access, quota and network; no review was published."
            ) from None
        self.stdout.write("Model returned valid structured output. Nothing was published.")

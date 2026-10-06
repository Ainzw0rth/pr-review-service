from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.models import Organization
from core.views import issue_access_link


class Command(BaseCommand):
    help = (
        "Print a private one-use invitation or password-reset link. Never paste into public logs."
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--organization", help="Organization slug to invite into")
        group.add_argument("--user", help="Username whose password should be reset")
        parser.add_argument("--hours", type=int)

    def handle(self, *args, **options):
        try:
            if options["organization"]:
                url = issue_access_link(
                    organization=Organization.objects.get(slug=options["organization"]),
                    hours=24 if options["hours"] is None else options["hours"],
                )
            else:
                url = issue_access_link(
                    user=get_user_model().objects.get(username=options["user"], is_active=True),
                    hours=1 if options["hours"] is None else options["hours"],
                )
        except (Organization.DoesNotExist, get_user_model().DoesNotExist, ValueError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(url)

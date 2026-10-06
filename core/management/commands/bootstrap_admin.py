from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.views import issue_access_link


class Command(BaseCommand):
    help = "Privately bootstrap an admin with an interactive password or a one-time setup link."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="louis")
        parser.add_argument("--email", default="louiscaesapdf@gmail.com")
        parser.add_argument(
            "--setup-link",
            action="store_true",
            help="Create an admin with no usable password and print a one-hour setup link.",
        )

    def handle(self, *args, **options):
        if not options["setup_link"]:
            call_command(
                "createsuperuser",
                username=options["username"],
                email=options["email"],
                interactive=True,
            )
            return
        with transaction.atomic():
            User = get_user_model()
            user = User.objects.filter(username=options["username"]).first()
            if user:
                if user.has_usable_password() or not user.is_superuser or not user.is_active:
                    raise CommandError(
                        "Account already exists. Use access_link --user for explicit password recovery."
                    )
            else:
                user = User.objects.create_superuser(
                    username=options["username"], email=options["email"], password=None
                )
            url = issue_access_link(user=user, hours=1)
        self.stdout.write(url)

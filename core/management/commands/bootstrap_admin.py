import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from core.models import Membership, Organization
from core.views import issue_access_link


class Command(BaseCommand):
    help = "Privately bootstrap an admin with an interactive password or a one-time setup link."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin")
        parser.add_argument(
            "--json",
            action="store_true",
            help="Return setup URL and owner ID as JSON (requires --setup-link).",
        )
        parser.add_argument("--email", default="")
        parser.add_argument(
            "--organization", default="Workspace", help="Workspace name for the administrator"
        )
        parser.add_argument(
            "--setup-link",
            action="store_true",
            help="Create an admin with no usable password and print a one-hour setup link.",
        )

    def handle(self, *args, **options):
        if options["json"] and not options["setup_link"]:
            raise CommandError("--json requires --setup-link")
        name = options["organization"].strip()
        slug = slugify(name)
        if not name or len(name) > 100 or not slug or len(slug) > 50:
            raise CommandError(
                "Organization requires a name up to 100 characters and an ASCII slug up to 50 characters."
            )
        User = get_user_model()
        try:
            for field in ("username", "email"):
                options[field] = User._meta.get_field(field).clean(options[field], None)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        if not options["setup_link"]:
            if User.objects.filter(username=options["username"]).exists():
                raise CommandError(
                    "Account already exists. Use access_link --user for explicit password recovery."
                )
            with transaction.atomic():
                call_command(
                    "createsuperuser",
                    username=options["username"],
                    email=options["email"],
                    interactive=True,
                )
                user = User.objects.filter(username=options["username"]).first()
                if user is None:
                    raise CommandError(
                        "Administrator was not created. Use an interactive terminal or --setup-link."
                    )
                self.attach_workspace(user, name, slug)
            self.owner_instruction(user)
            return
        with transaction.atomic():
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
            self.attach_workspace(user, name, slug)
            url = issue_access_link(user=user, hours=1)
        self.owner_instruction(user)
        self.stdout.write(
            json.dumps({"owner_id": user.pk, "setup_url": url}) if options["json"] else url
        )

    def attach_workspace(self, user, name, slug):
        with transaction.atomic():
            organization, _ = Organization.objects.get_or_create(slug=slug, defaults={"name": name})
            Membership.objects.get_or_create(organization=organization, user=user)

    def owner_instruction(self, user):
        self.stderr.write(
            f"Set DASHBOARD_RETRY_USER_ID={user.pk} in .env, then recreate web/workers with your Compose files. "
            "This explicitly grants this administrator Settings and Workflow ownership."
        )

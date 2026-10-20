"""Prepare only the isolated local Settings browser fixture; no external calls."""

import json
import os
from pathlib import Path

import django


def main():
    expected = Path(__file__).resolve().parent / "task-4-browser-data"
    if Path(os.environ["DATA_DIR"]).resolve() != expected or os.environ.get("DEBUG") != "1":
        raise RuntimeError(
            "This fixture requires the isolated task-4-browser-data directory and DEBUG=1"
        )
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "reviewdesk.settings")
    django.setup()
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.test import Client

    from core.models import Credential, Organization, Repository, ServiceSettings

    call_command("migrate", verbosity=0)
    owner, _ = get_user_model().objects.get_or_create(
        pk=1, defaults={"username": "local-owner", "is_superuser": True, "is_staff": True}
    )
    org, _ = Organization.objects.get_or_create(slug="local-team", defaults={"name": "Local team"})
    credential, _ = Credential.objects.get_or_create(
        name="local-no-external-access", defaults={"encrypted_value": "unused"}
    )
    Repository.objects.get_or_create(
        full_name="local-team/review-service",
        defaults={
            "organization": org,
            "reviewer": "local-owner",
            "github_credential": credential,
            "webhook_credential": credential,
        },
    )
    ServiceSettings.objects.filter(pk=1).update(concurrent_pr_reviews=2)
    client = Client()
    client.force_login(owner)
    Path(os.environ["DATA_DIR"], "browser-cookie.json").write_text(
        json.dumps(
            {
                "name": "sessionid",
                "value": client.cookies["sessionid"].value,
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": True,
                "sameSite": "Lax",
            }
        )
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Create a private deployment environment once; never print generated secrets."""

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

root = Path(__file__).resolve().parent.parent
path = root / ".env"
content = (
    (root / ".env.example")
    .read_text()
    .replace(
        "DJANGO_SECRET_KEY=generate-with-scripts-init-local",
        "DJANGO_SECRET_KEY=" + secrets.token_urlsafe(64),
    )
    .replace(
        "CREDENTIAL_KEY=generate-with-scripts-init-local",
        "CREDENTIAL_KEY=" + Fernet.generate_key().decode(),
    )
)
if Path("/var/run/docker.sock").exists():
    content = content.replace(
        "DOCKER_GID=999", "DOCKER_GID=" + str(Path("/var/run/docker.sock").stat().st_gid)
    )
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write(content)
print("Created private .env. Keep a separate encrypted backup of its keys.")

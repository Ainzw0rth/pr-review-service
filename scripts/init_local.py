#!/usr/bin/env python3
"""Create a private deployment environment once using only Python's standard library."""

import argparse
import base64
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit


def https_origin(value):
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate malformed/out-of-range ports.
    except ValueError:
        raise argparse.ArgumentTypeError(
            "Expected an HTTPS origin, e.g. https://reviews.example.com"
        ) from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in value)
        or any(c in value for c in "$#\\\"'")
    ):
        raise argparse.ArgumentTypeError(
            "Expected an HTTPS origin without credentials, path, query or fragment"
        )
    return value.rstrip("/")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("local", "server"), default="local")
    parser.add_argument(
        "--public-url", type=https_origin, help="Required HTTPS origin for server mode"
    )
    parser.add_argument(
        "--webhook-url",
        type=https_origin,
        help="Public HTTPS forwarding origin for GitHub webhooks",
    )
    args = parser.parse_args()
    if args.mode == "server" and not args.public_url:
        parser.error("--mode server requires --public-url")
    if args.mode == "local" and args.public_url:
        parser.error("--public-url is for server mode; local forwarding uses --webhook-url")
    root = Path(__file__).resolve().parent.parent
    public_url = args.public_url or "http://127.0.0.1:8040"
    hostname = urlsplit(public_url).hostname
    allowed_host = f"[{hostname}]" if ":" in hostname else hostname
    values = {
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
        "CREDENTIAL_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "DB_PASSWORD": secrets.token_urlsafe(48),
        "LOCAL_HTTP": "1" if args.mode == "local" else "0",
        "TRUST_PROXY": "0" if args.mode == "local" else "1",
        "PUBLIC_URL": public_url,
        "WEBHOOK_PUBLIC_URL": args.webhook_url or "",
        "ALLOWED_HOSTS": "localhost,127.0.0.1,[::1]"
        if args.mode == "local"
        else allowed_host + ",localhost,127.0.0.1",
        "CSRF_TRUSTED_ORIGINS": "" if args.mode == "local" else public_url,
    }
    socket = Path("/var/run/docker.sock")
    if socket.exists():
        values["DOCKER_GID"] = str(socket.stat().st_gid)
    lines = (root / ".env.example").read_text(encoding="utf-8").splitlines()
    content = (
        "\n".join(
            f"{key}={values[key]}" if (key := line.partition("=")[0]) in values else line
            for line in lines
        )
        + "\n"
    )
    try:
        fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.exit(1, "Refusing to overwrite existing .env; its secrets remain unchanged.\n")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    print("Created private .env. Keep an encrypted backup of its keys.")
    print("Use: docker compose -f compose.yaml -f compose.postgres.yaml up -d --build")
    if args.mode == "local":
        print(
            "Local HTTP: keep the Compose port bound to loopback. Public forwarding must preserve the loopback Host header."
        )
    print("Runtime requires Linux Docker; Windows/macOS hosts need a Linux VM with Docker Engine.")


if __name__ == "__main__":
    main()

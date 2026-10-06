"""GitHub transport: fixed hosts, finite calls, complete-or-fail pagination."""

import json
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import httpx
from cryptography.fernet import InvalidToken

from .secrets import decrypt

API = "https://api.github.com"
TIMEOUT = httpx.Timeout(45, connect=10)
MAX_PAGES = 100
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


class IntegrationError(Exception):
    """Only static, credential-free operational descriptions belong here."""


class GitHub:
    def __init__(self, repository):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository.full_name):
            raise IntegrationError("Invalid repository configuration.")
        self.repository = repository
        self.prefix = "/repos/" + repository.full_name
        try:
            token = decrypt(repository.github_credential.encrypted_value)
        except (InvalidToken, ValueError, TypeError, RuntimeError, AttributeError):
            raise IntegrationError("GitHub credential unavailable; reconnect account.") from None
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.client = httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _url(self, path):
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise IntegrationError("GitHub destination rejected.")
        parsed = urlsplit(path)
        decoded = unquote(parsed.path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or "\\" in decoded
            or any(p in (".", "..") for p in decoded.split("/"))
            or any(ord(c) < 32 for c in decoded)
        ):
            raise IntegrationError("GitHub destination rejected.")
        if parsed.path.startswith("/repos/"):
            if not parsed.path.startswith(self.prefix + "/"):
                raise IntegrationError("Cross-repository destination rejected.")
        elif parsed.path not in ("/graphql", "/user"):
            path = self.prefix + path
        return API + path

    def _request(self, method, path, data=None):
        try:
            with self.client.stream(
                method,
                self._url(path),
                headers=self.headers,
                json=data,
                timeout=TIMEOUT,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise IntegrationError(f"GitHub request failed (HTTP {response.status_code}).")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise IntegrationError("GitHub response exceeds safe context limit.")
                # Streaming responses are reconstructed after the bounded read.
                # iter_bytes() already decoded compression; do not decode it twice.
                headers = dict(response.headers)
                headers.pop("content-encoding", None)
                headers.pop("content-length", None)
                return httpx.Response(
                    response.status_code,
                    headers=headers,
                    content=bytes(raw),
                    request=response.request,
                )
        except (httpx.HTTPError, ValueError, TypeError):
            raise IntegrationError("GitHub request unavailable or malformed.") from None

    def _json(self, response):
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise IntegrationError("GitHub returned malformed data.") from None

    def get(self, path):
        return self._json(self._request("GET", path))

    def post(self, path, data):
        return self._json(self._request("POST", path, data))

    def patch(self, path, data):
        return self._json(self._request("PATCH", path, data))

    def delete(self, path):
        return self._json(self._request("DELETE", path))

    def pages(self, path, key=None):
        self._url(path)
        parsed = urlsplit(path)
        query = dict(parse_qsl(parsed.query))
        query["per_page"] = "100"
        current = parsed.path + "?" + urlencode(query)
        seen, output = set(), []
        for _ in range(MAX_PAGES):
            if current in seen:
                raise IntegrationError("GitHub pagination did not terminate.")
            seen.add(current)
            response = self._request("GET", current)
            value = self._json(response)
            rows = value.get(key) if key and isinstance(value, dict) else value
            if not isinstance(rows, list):
                raise IntegrationError("GitHub returned malformed pagination.")
            output.extend(rows)
            if len(json.dumps(output).encode()) > MAX_RESPONSE_BYTES:
                raise IntegrationError("GitHub pagination exceeds supported context size.")
            next_url = response.links.get("next", {}).get("url")
            if not next_url:
                if (
                    key
                    and isinstance(value.get("total_count"), int)
                    and len(output) < value["total_count"]
                ):
                    raise IntegrationError("GitHub pagination is incomplete.")
                return output
            next_parts = urlsplit(next_url)
            if (
                next_parts.scheme != "https"
                or next_parts.netloc != "api.github.com"
                or next_parts.fragment
            ):
                raise IntegrationError("GitHub pagination destination rejected.")
            current = next_parts.path + ("?" + next_parts.query if next_parts.query else "")
        raise IntegrationError("GitHub pagination exceeds supported context limit.")

    def pull(self, number):
        if type(number) is not int or number < 1:
            raise IntegrationError("Invalid pull request number.")
        return self.get(f"/pulls/{number}")

    def identity(self):
        data = self.get("/user")
        if not isinstance(data, dict) or not isinstance(data.get("login"), str):
            raise IntegrationError("GitHub identity unavailable.")
        return data["login"]

    def graphql(self, query, variables):
        result = self.post("/graphql", {"query": query, "variables": variables})
        if (
            not isinstance(result, dict)
            or result.get("errors")
            or not isinstance(result.get("data"), dict)
        ):
            raise IntegrationError("GitHub discussion context unavailable.")
        return result["data"]

    def archive(self, sha):
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", sha):
            raise IntegrationError("Invalid source commit.")
        url = self._url("/tarball/" + sha)
        headers = self.headers
        try:
            # Auth goes only to api.github.com. GitHub supplies a signed codeload URL.
            for step in range(2):
                with self.client.stream("GET", url, headers=headers, timeout=TIMEOUT) as response:
                    if step == 0 and response.status_code in (301, 302, 303, 307, 308):
                        url = response.headers.get("location", "")
                        target = urlsplit(url)
                        if (
                            target.scheme != "https"
                            or target.netloc != "codeload.github.com"
                            or target.fragment
                            or not target.path.startswith("/" + self.repository.full_name + "/")
                        ):
                            raise IntegrationError("Source archive destination rejected.")
                        headers = {"Accept": "application/octet-stream"}
                        continue
                    if response.status_code != 200:
                        raise IntegrationError("Source archive unavailable.")
                    archive = bytearray()
                    for chunk in response.iter_bytes():
                        archive.extend(chunk)
                        if len(archive) > MAX_ARCHIVE_BYTES:
                            raise IntegrationError("Source archive exceeds supported size.")
                    return bytes(archive)
        except (httpx.HTTPError, ValueError):
            raise IntegrationError("Source archive unavailable.") from None
        raise IntegrationError("Source archive redirect rejected.")


def check_required(github, repository, sha):
    required = repository.required_checks
    if not isinstance(required, list) or any(
        not isinstance(name, str) or not name.strip() for name in required
    ):
        return {"passed": False, "summary": "Required check configuration is invalid."}
    if not required:
        return {
            "passed": False,
            "summary": "No required GitHub checks configured; required test evidence is missing.",
        }
    if not re.fullmatch(r"[a-fA-F0-9]{40,64}", sha):
        raise IntegrationError("Invalid check commit.")
    checks = github.pages(f"/commits/{sha}/check-runs?filter=latest", key="check_runs")
    statuses = github.pages(f"/commits/{sha}/statuses")
    evidence = {}
    # Commit statuses arrive newest first. Only the latest status is meaningful.
    for status in statuses:
        name = status.get("context")
        if name not in evidence:
            evidence[name] = status.get("state") == "success"
    check_evidence = {}
    for check in checks:
        name = check.get("name")
        passed = check.get("status") == "completed" and check.get("conclusion") == "success"
        check_evidence[name] = check_evidence.get(name, True) and passed
    for name, passed in check_evidence.items():
        evidence[name] = evidence.get(name, True) and passed
    missing = [name for name in required if not evidence.get(name, False)]
    return {
        "passed": not missing,
        "summary": "Required checks passed."
        if not missing
        else "Required checks missing, pending or failed: " + ", ".join(missing),
    }

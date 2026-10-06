"""Optional status-only synchronization using admin-confirmed stable task links."""

import json
import re
from urllib.parse import quote

import httpx
from cryptography.fernet import InvalidToken
from django.conf import settings
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from requests.exceptions import RequestException

from .github import TIMEOUT, IntegrationError
from .secrets import decrypt

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
MAX_SHEET_BYTES = 8 * 1024 * 1024


def _connection_configuration(repository):
    config = repository.sheet_config
    if not isinstance(config, dict) or not repository.sheet_credential:
        raise IntegrationError("Sheet connection requires configuration and a credential.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", config.get("spreadsheet_id", "")):
        raise IntegrationError("Invalid Sheet identifier.")
    if not re.fullmatch(
        r"(?:'[^']*(?:''[^']*)*'|[A-Za-z0-9_ -]+)!A1?:[A-Z]+", config.get("range", "")
    ):
        raise IntegrationError("Sheet range must start at column A, row 1 and include the header.")
    header_row = config.get("header_row", 1)
    if type(header_row) is not int or header_row < 1:
        raise IntegrationError("Sheet header_row must be a positive row number.")
    return config


def _vocabulary(config):
    return set(config.get("allowed_statuses", config["statuses"].values()))


def _configuration(repository):
    config = _connection_configuration(repository)
    if (
        not isinstance(config, dict)
        or config.get("confirmed") is not True
        or not repository.sheet_credential
    ):
        raise IntegrationError("Sheet mapping requires administrator confirmation.")
    statuses = config.get("statuses")
    headers = config.get("headers")
    if (
        not isinstance(statuses, dict)
        or set(statuses) != {"review", "feedback", "ready", "done"}
        or any(not isinstance(value, str) or not value.strip() for value in statuses.values())
    ):
        raise IntegrationError("Sheet status mapping requires reconfirmation.")
    allowed = config.get("allowed_statuses", list(set(statuses.values())))
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(not isinstance(value, str) or not value.strip() for value in allowed)
        or len(set(allowed)) != len(allowed)
        or not set(statuses.values()).issubset(allowed)
    ):
        raise IntegrationError("Confirmed Sheet vocabulary must include every mapped status.")
    if statuses["done"] in [statuses[key] for key in ("review", "feedback", "ready")]:
        raise IntegrationError("Completed status must be distinct from review statuses.")
    if (
        not isinstance(headers, list)
        or not headers
        or any(not isinstance(value, str) or not value for value in headers)
        or len(set(headers)) != len(headers)
    ):
        raise IntegrationError("Sheet header signature requires confirmation.")
    if (
        config.get("id_column") not in headers
        or config.get("status_column") not in headers
        or config["id_column"] == config["status_column"]
    ):
        raise IntegrationError("Sheet ID/status columns require confirmation.")
    if not isinstance(config.get("tasks"), dict) or not config["tasks"]:
        raise IntegrationError("Explicit task-to-PR mapping is missing.")
    for task_id, task in config["tasks"].items():
        prs = task.get("prs") if isinstance(task, dict) else None
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(prs, list)
            or not prs
            or any(type(number) is not int or number < 1 for number in prs)
            or len(set(prs)) != len(prs)
            or type(task.get("issue")) is not int
            or task["issue"] < 1
        ):
            raise IntegrationError(
                "Task mapping requires explicit issue and nonempty required PR set."
            )
    return config


def _column(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _row(config, rows, task_id):
    header_row = config.get("header_row", 1)
    if (
        not isinstance(rows, list)
        or len(rows) < header_row
        or rows[header_row - 1] != config["headers"]
    ):
        raise IntegrationError("Sheet headers changed; administrator reconfirmation required.")
    id_index = config["headers"].index(config["id_column"])
    status_index = config["headers"].index(config["status_column"])
    matches = [
        (number, row)
        for number, row in enumerate(rows[header_row:], header_row + 1)
        if isinstance(row, list) and len(row) > id_index and str(row[id_index]) == task_id
    ]
    if len(matches) != 1:
        raise IntegrationError("Sheet task ID is missing or ambiguous; no status written.")
    number, row = matches[0]
    if len(row) <= status_index or row[status_index] not in _vocabulary(config):
        raise IntegrationError(
            "Sheet status vocabulary changed; administrator reconfirmation required."
        )
    return number, row, status_index


class Sheets:
    def __init__(self, repository, *, read_only=False):
        self.read_only = read_only
        self.config = (
            _connection_configuration(repository) if read_only else _configuration(repository)
        )
        try:
            info = json.loads(decrypt(repository.sheet_credential.encrypted_value))
            if info.get("token_uri") != "https://oauth2.googleapis.com/token":
                raise ValueError("Unexpected token endpoint")
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=[SCOPE + ".readonly" if read_only else SCOPE]
            )
            request = Request()
            credentials.refresh(lambda *args, **kwargs: request(*args, **{**kwargs, "timeout": 30}))
            self.headers = {"Authorization": "Bearer " + credentials.token}
        except (
            GoogleAuthError,
            RequestException,
            InvalidToken,
            ValueError,
            TypeError,
            KeyError,
            RuntimeError,
            AttributeError,
        ):
            raise IntegrationError(
                "Google Sheets credential unavailable; reconnect account."
            ) from None
        self.client = httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False)
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + self.config["spreadsheet_id"]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def _request(self, method, path, *, params=None, body=None):
        try:
            with self.client.stream(
                method,
                self.base + path,
                headers=self.headers,
                params=params,
                json=body,
                timeout=TIMEOUT,
            ) as response:
                if response.status_code != 200:
                    raise IntegrationError(
                        f"Google Sheets request failed (HTTP {response.status_code})."
                    )
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_SHEET_BYTES:
                        raise IntegrationError("Sheet exceeds supported size.")
                return json.loads(data)
        except (httpx.HTTPError, ValueError, TypeError):
            raise IntegrationError("Google Sheets request unavailable or malformed.") from None

    def values(self):
        result = self._request(
            "GET",
            "/values/" + quote(self.config["range"], safe=""),
            params={"valueRenderOption": "UNFORMATTED_VALUE"},
        )
        return result.get("values", [])

    def allowed_statuses(self, cell):
        result = self._request(
            "GET",
            "",
            params={
                "ranges": cell,
                "includeGridData": "true",
                "fields": "sheets(data(rowData(values(dataValidation))))",
            },
        )
        try:
            rule = result["sheets"][0]["data"][0]["rowData"][0]["values"][0]["dataValidation"][
                "condition"
            ]
            if rule["type"] != "ONE_OF_LIST":
                raise KeyError("unsupported validation")
            return {value["userEnteredValue"] for value in rule["values"]}
        except (KeyError, IndexError, TypeError):
            raise IntegrationError(
                "Sheet status cell requires an explicit confirmed dropdown vocabulary."
            ) from None

    def update(self, cell, value):
        if getattr(self, "read_only", False):
            raise IntegrationError("Inspection connection is read-only.")
        if not settings.LIVE_WRITES:
            raise IntegrationError("Live external writes are disabled.")
        return self._request(
            "PUT",
            "/values/" + quote(cell, safe=""),
            params={"valueInputOption": "RAW"},
            body={"range": cell, "majorDimension": "ROWS", "values": [[value]]},
        )


def inspect_mapping(repository, *, id_column=None, status_column=None):
    """Read-only onboarding: expose schema/statuses, never infer task/PR links."""
    config = _connection_configuration(repository)
    with Sheets(repository, read_only=True) as sheet:
        rows = sheet.values()
        header_row = config.get("header_row", 1)
        headers = (
            rows[header_row - 1] if isinstance(rows, list) and len(rows) >= header_row else None
        )
        if (
            not isinstance(headers, list)
            or not headers
            or any(not isinstance(value, str) or not value for value in headers)
            or len(set(headers)) != len(headers)
        ):
            raise IntegrationError("Sheet needs unique nonempty column headers.")

        def choose(explicit, known):
            if explicit is not None:
                if explicit not in headers:
                    raise IntegrationError("Requested inspection column does not match a header.")
                return explicit
            matches = [header for header in headers if header.casefold() in known]
            return matches[0] if len(matches) == 1 else None

        id_column = choose(id_column or config.get("id_column"), {"id", "task id", "issue id"})
        status_column = choose(
            status_column or config.get("status_column"), {"status", "issue status", "state"}
        )
        current, dropdown, sample_cell, note = [], [], None, ""
        if status_column:
            index = headers.index(status_column)
            current = sorted(
                {
                    str(row[index])
                    for row in rows[header_row:]
                    if isinstance(row, list) and len(row) > index and str(row[index]).strip()
                }
            )
            sample_row = next(
                (
                    number
                    for number, row in enumerate(rows[header_row:], header_row + 1)
                    if isinstance(row, list) and len(row) > index and str(row[index]).strip()
                ),
                header_row + 1,
            )
            sample_cell = config["range"].split("!")[0] + "!" + _column(index + 1) + str(sample_row)
            try:
                dropdown = sorted(sheet.allowed_statuses(sample_cell))
            except IntegrationError as error:
                note = str(error)
        else:
            note = "Select the status column explicitly; no unambiguous status header found."
        return {
            "rows": max(0, len(rows) - header_row),
            "current_statuses": current,
            "dropdown_sample_cell": sample_cell,
            "note": note,
            "suggested_config": {
                "confirmed": False,
                "spreadsheet_id": config["spreadsheet_id"],
                "range": config["range"],
                "header_row": header_row,
                "headers": headers,
                "id_column": id_column,
                "status_column": status_column,
                "allowed_statuses": dropdown,
                "statuses": config.get(
                    "statuses", dict.fromkeys(("review", "feedback", "ready", "done"))
                ),
                "tasks": config.get("tasks", {}),
            },
        }


def task_context(repository, task_ids):
    config = _configuration(repository)
    result = []
    with Sheets(repository) as sheet:
        rows = sheet.values()
        for task_id in task_ids:
            _, row, _ = _row(config, rows, task_id)
            result.append(
                {
                    "id": task_id,
                    **config["tasks"][task_id],
                    "requirements": dict(zip(config["headers"], row)),
                }
            )
    return result


def _target_status(github, repository, task, number, outcome):
    pulls = [github.pull(pr) for pr in task["prs"]]
    if all(pr.get("merged") is True for pr in pulls):
        return "done"
    if outcome in ("merged", "closed", "done"):
        return None
    status = {
        "approved": "ready",
        "ready": "ready",
        "review": "review",
        "on review": "review",
        "feedback": "feedback",
        "failed": "feedback",
    }.get(outcome)
    if not status:
        raise IntegrationError("Unknown Sheet lifecycle outcome.")
    if status == "ready":
        for pr in pulls:
            if pr.get("merged") is True:
                continue
            if pr.get("state") != "open" or pr.get("draft"):
                return None
            if pr.get("number") != number and repository.approved_label not in [
                label.get("name") for label in pr.get("labels", [])
            ]:
                return None
    return status


def sync_status(github, repository, number, outcome, before_write=None):
    if not repository.sheet_config:
        return
    config = _configuration(repository)
    matches = [
        (task_id, task) for task_id, task in config["tasks"].items() if number in task["prs"]
    ]
    if len(matches) != 1:
        raise IntegrationError("Pull request has no unique confirmed Sheet task mapping.")
    task_id, task = matches[0]
    status = _target_status(github, repository, task, number, outcome)
    if status is None:
        return
    with Sheets(repository) as sheet:
        first_number, first, status_index = _row(config, sheet.values(), task_id)
        target = config["statuses"][status]
        if first[status_index] == target:
            return
        tab = config["range"].split("!")[0]
        first_cell = f"{tab}!{_column(status_index + 1)}{first_number}"
        if sheet.allowed_statuses(first_cell) != _vocabulary(config):
            raise IntegrationError(
                "Sheet allowed statuses changed; administrator reconfirmation required."
            )
        row_number, current, status_index = _row(config, sheet.values(), task_id)
        if first != current:
            raise IntegrationError("Sheet task changed during reconciliation; no status written.")
        cell = f"{tab}!{_column(status_index + 1)}{row_number}"
        if cell != first_cell and sheet.allowed_statuses(cell) != _vocabulary(config):
            raise IntegrationError(
                "Sheet allowed statuses changed; administrator reconfirmation required."
            )
        # Google values.update has no compare-and-set. Re-read narrows, not removes,
        # the concurrent-edit window; no unrelated cells are ever sent for update.
        if _target_status(github, repository, task, number, outcome) != status:
            raise IntegrationError("Required pull request state changed; no status written.")
        if before_write is not None:
            before_write()
        sheet.update(cell, target)

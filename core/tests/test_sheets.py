from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from core.github import IntegrationError
from core.sheets import sync_status


class SheetTests(TestCase):
    def test_title_rows_preserve_absolute_status_cell_and_moved_row(self):
        self.config["header_row"] = 3
        prefix = [["Timeline title"], ["Subtitle"]]
        self.github.pull.side_effect = lambda number: {"number": number, "merged": True}
        with patch("core.sheets.Sheets") as cls:
            sheet = cls.return_value.__enter__.return_value
            sheet.values.side_effect = [
                prefix + deepcopy(self.rows),
                prefix + [self.rows[0], self.rows[2], self.rows[1]],
            ]
            sheet.allowed_statuses.return_value = set(self.config["statuses"].values())
            sync_status(self.github, self.repo, 1, "merged")
            sheet.update.assert_called_once_with("Tasks!B5", "Complete")

    def test_inspection_skips_title_and_section_rows_for_dropdown_sample(self):
        from core.sheets import inspect_mapping

        self.config["header_row"] = 3
        with patch("core.sheets.Sheets") as cls:
            sheet = cls.return_value.__enter__.return_value
            sheet.values.return_value = [["Title"], [], self.rows[0], ["Section"], self.rows[1]]
            sheet.allowed_statuses.return_value = {"Review", "Changes", "Ready", "Complete"}
            result = inspect_mapping(self.repo)
            self.assertEqual(result["dropdown_sample_cell"], "Tasks!B5")
            self.assertEqual(result["suggested_config"]["header_row"], 3)
            sheet.update.assert_not_called()

    def test_invalid_header_row_rejected(self):
        from core.sheets import _connection_configuration

        for value in (0, -1, True, "3", 1.5):
            self.config["header_row"] = value
            with self.subTest(value=value), self.assertRaises(IntegrationError):
                _connection_configuration(self.repo)

    def setUp(self):
        self.config = {
            "confirmed": True,
            "spreadsheet_id": "sheet-id",
            "range": "Tasks!A1:C",
            "headers": ["ID", "Status", "Requirement"],
            "id_column": "ID",
            "status_column": "Status",
            "statuses": {
                "review": "Review",
                "feedback": "Changes",
                "ready": "Ready",
                "done": "Complete",
            },
            "tasks": {"TASK-1": {"issue": 12, "prs": [1, 2]}},
        }
        self.repo = SimpleNamespace(
            approved_label="approved",
            sheet_config=self.config,
            sheet_credential=SimpleNamespace(encrypted_value="encrypted"),
        )
        self.github = Mock()
        self.github.pull.side_effect = lambda number: {
            "number": number,
            "merged": number == 1,
            "state": "closed" if number == 1 else "open",
        }
        self.rows = [
            self.config["headers"],
            ["TASK-1", "Review", "Keep text"],
            ["TASK-2", "Changes", "Other"],
        ]

    def test_unmerged_required_pr_never_completes(self):
        with patch("core.sheets.Sheets") as sheets:
            sync_status(self.github, self.repo, 1, "merged")
            sheets.assert_not_called()

    def test_all_merged_updates_only_status_and_follows_stable_id(self):
        self.github.pull.side_effect = lambda number: {"number": number, "merged": True}
        moved = [self.rows[0], self.rows[2], self.rows[1]]
        with patch("core.sheets.Sheets") as cls:
            sheet = cls.return_value.__enter__.return_value
            sheet.values.side_effect = [deepcopy(self.rows), deepcopy(moved)]
            sheet.allowed_statuses.return_value = set(self.config["statuses"].values())
            sync_status(self.github, self.repo, 1, "merged")
            sheet.update.assert_called_once_with("Tasks!B3", "Complete")

    def test_duplicate_ids_changed_headers_or_changed_status_stop_writes(self):
        bad = [
            self.rows + [self.rows[1]],
            [["ID", "Renamed", "Requirement"], *self.rows[1:]],
            [self.rows[0], ["TASK-1", "Changes", "Keep text"], self.rows[2]],
        ]
        for second in bad:
            with self.subTest(rows=second), patch("core.sheets.Sheets") as cls:
                sheet = cls.return_value.__enter__.return_value
                sheet.values.side_effect = [deepcopy(self.rows), deepcopy(second)]
                sheet.allowed_statuses.return_value = set(self.config["statuses"].values())
                with self.assertRaises(IntegrationError):
                    sync_status(self.github, self.repo, 1, "feedback")
                sheet.update.assert_not_called()

    def test_empty_required_set_or_unconfirmed_mapping_stops(self):
        for config in [
            {**self.config, "confirmed": False},
            {**self.config, "tasks": {"TASK-1": {"issue": 12, "prs": []}}},
        ]:
            self.repo.sheet_config = config
            with patch("core.sheets.Sheets") as cls:
                with self.assertRaises(IntegrationError):
                    sync_status(self.github, self.repo, 1, "feedback")
                cls.assert_not_called()

    def test_changed_dropdown_stops_write(self):
        with patch("core.sheets.Sheets") as cls:
            sheet = cls.return_value.__enter__.return_value
            sheet.values.return_value = deepcopy(self.rows)
            sheet.allowed_statuses.return_value = {"Other"}
            with self.assertRaises(IntegrationError):
                sync_status(self.github, self.repo, 1, "feedback")
            sheet.update.assert_not_called()

    def test_closed_unmerged_blocks_completion(self):
        self.github.pull.side_effect = lambda number: {
            "number": number,
            "merged": number == 1,
            "state": "closed",
        }
        with patch("core.sheets.Sheets") as cls:
            sync_status(self.github, self.repo, 1, "merged")
            cls.assert_not_called()

    def test_real_http_write_only_one_raw_status_cell(self):
        import httpx
        from django.test import override_settings

        from core.sheets import Sheets

        sheet = object.__new__(Sheets)
        sheet.config = self.config
        sheet.base = "https://sheets.googleapis.com/v4/spreadsheets/sheet-id"
        sheet.headers = {"Authorization": "Bearer secret"}
        seen = []

        def handler(request):
            import json

            seen.append(request)
            self.assertEqual(request.method, "PUT")
            self.assertEqual(request.url.params["valueInputOption"], "RAW")
            self.assertEqual(
                json.loads(request.content),
                {
                    "range": "Tasks!B2",
                    "majorDimension": "ROWS",
                    "values": [["Complete"]],
                },
            )
            return httpx.Response(200, json={"updatedCells": 1})

        with (
            httpx.Client(transport=httpx.MockTransport(handler)) as client,
            override_settings(LIVE_WRITES=True),
        ):
            sheet.client = client
            sheet.update("Tasks!B2", "Complete")
        self.assertEqual(len(seen), 1)

    def test_live_write_gate(self):
        from django.test import override_settings

        from core.sheets import Sheets

        sheet = object.__new__(Sheets)
        with override_settings(LIVE_WRITES=False), self.assertRaises(IntegrationError):
            sheet.update("Tasks!B2", "Complete")

    def test_cancelled_before_write_does_not_update(self):
        def cancelled():
            raise IntegrationError("Attempt cancelled.")

        with patch("core.sheets.Sheets") as cls:
            sheet = cls.return_value.__enter__.return_value
            sheet.values.return_value = deepcopy(self.rows)
            sheet.allowed_statuses.return_value = set(self.config["statuses"].values())
            with self.assertRaisesRegex(IntegrationError, "cancelled"):
                sync_status(self.github, self.repo, 1, "feedback", before_write=cancelled)
            sheet.update.assert_not_called()

    def test_partial_pr_approval_does_not_mark_task_ready(self):
        with patch("core.sheets.Sheets") as cls:
            sync_status(self.github, self.repo, 1, "approved")
            cls.assert_not_called()

    def test_ready_requires_other_pr_approved_and_not_draft(self):
        self.repo.approved_label = "approved"
        self.github.pull.side_effect = lambda number: {
            "number": number,
            "merged": False,
            "state": "open",
            "draft": False,
            "labels": [{"name": "approved"}],
        }
        with patch("core.sheets.Sheets") as cls:
            sheet = cls.return_value.__enter__.return_value
            sheet.values.return_value = deepcopy(self.rows)
            sheet.allowed_statuses.return_value = set(self.config["statuses"].values())
            sync_status(self.github, self.repo, 1, "ready")
            sheet.update.assert_called_once_with("Tasks!B2", "Ready")

    def test_backlog_to_review_uses_complete_confirmed_vocabulary(self):
        self.config["statuses"]["review"] = "In review"
        self.config["statuses"]["ready"] = "In review"
        self.config["allowed_statuses"] = ["Backlog", "In review", "Changes", "Complete"]
        self.rows[1][1] = "Backlog"
        with patch("core.sheets.Sheets") as cls:
            sheet = cls.return_value.__enter__.return_value
            sheet.values.return_value = deepcopy(self.rows)
            sheet.allowed_statuses.return_value = set(self.config["allowed_statuses"])
            sync_status(self.github, self.repo, 1, "review")
            sheet.update.assert_called_once_with("Tasks!B2", "In review")

    def test_shared_nonterminal_mapping_without_explicit_vocabulary(self):
        self.config["statuses"]["ready"] = "Review"
        self.rows[1][1] = "Changes"
        with patch("core.sheets.Sheets") as cls:
            sheet = cls.return_value.__enter__.return_value
            sheet.values.return_value = deepcopy(self.rows)
            sheet.allowed_statuses.return_value = {"Review", "Changes", "Complete"}
            sync_status(self.github, self.repo, 1, "review")
            sheet.update.assert_called_once_with("Tasks!B2", "Review")

    def test_vocabulary_must_include_every_mapped_status(self):
        self.config["allowed_statuses"] = ["Backlog"]
        with patch("core.sheets.Sheets") as cls:
            with self.assertRaises(IntegrationError):
                sync_status(self.github, self.repo, 1, "review")
            cls.assert_not_called()

    def test_read_only_inspection_proposes_unconfirmed_actual_mapping(self):
        from core.sheets import inspect_mapping

        self.repo.sheet_config = {"spreadsheet_id": "sheet-id", "range": "Tasks!A1:C"}
        self.rows[1][1] = "Backlog"
        with patch("core.sheets.Sheets") as cls:
            sheet = cls.return_value.__enter__.return_value
            sheet.values.return_value = deepcopy(self.rows)
            sheet.allowed_statuses.return_value = {"Backlog", "In review", "Complete"}
            result = inspect_mapping(self.repo)
            cls.assert_called_once_with(self.repo, read_only=True)
            sheet.update.assert_not_called()
            self.assertEqual(result["current_statuses"], ["Backlog", "Changes"])
            self.assertEqual(result["suggested_config"]["headers"], self.rows[0])
            self.assertEqual(result["suggested_config"]["status_column"], "Status")
            self.assertEqual(
                result["suggested_config"]["allowed_statuses"], ["Backlog", "Complete", "In review"]
            )
            self.assertFalse(result["suggested_config"]["confirmed"])

    def test_read_only_connection_uses_restricted_scope_and_forbids_update(self):
        from django.test import override_settings

        from core.sheets import Sheets

        self.repo.sheet_config = {"spreadsheet_id": "sheet-id", "range": "Tasks!A1:C"}
        with (
            patch(
                "core.sheets.decrypt",
                return_value='{"token_uri":"https://oauth2.googleapis.com/token"}',
            ),
            patch("core.sheets.service_account.Credentials.from_service_account_info") as factory,
        ):
            factory.return_value.token = "test-token"
            with Sheets(self.repo, read_only=True) as sheet, override_settings(LIVE_WRITES=True):
                with self.assertRaisesRegex(IntegrationError, "read-only"):
                    sheet.update("Tasks!B2", "Complete")
            self.assertEqual(
                factory.call_args.kwargs["scopes"],
                ["https://www.googleapis.com/auth/spreadsheets.readonly"],
            )

    def test_inspection_command_outputs_proposal_without_saving(self):
        import json
        from io import StringIO

        from django.core.management import call_command

        output = StringIO()
        proposal = {"suggested_config": {"confirmed": False}, "current_statuses": ["Backlog"]}
        with (
            patch("core.management.commands.inspect_sheet.Repository.objects") as repositories,
            patch("core.management.commands.inspect_sheet.inspect_mapping", return_value=proposal),
        ):
            call_command("inspect_sheet", "17", stdout=output)
            repositories.select_related.return_value.get.assert_called_once_with(pk=17)
            repositories.update.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), proposal)

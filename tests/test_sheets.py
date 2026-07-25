"""
Tests for date handling and row coloring in the Sheets client.

Covers the shared date primitives in src/sheets.py, the two write paths in
check_gmail.py that feed columns F (Application Date) and G/H/I (OA / Phone / Tech),
and the color a freshly appended row lands with.

Usage:
    pytest tests/test_sheets.py -v
    pytest tests/test_sheets.py::TestDateAlreadyRecorded -v
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.email_classifier import EmailClassification
from src.gmail import EmailMessage
from src.config import _parse_status_colors
from src.sheets import (
    COLUMNS,
    SheetsClient,
    date_already_recorded,
    parse_sheet_datetime,
    split_date_cell,
)


# =============================================================================
# Helpers
# =============================================================================

@pytest.fixture
def checker_config():
    """Minimal stand-in exposing only what GmailChecker reads.

    Same approach as FakeConfig in test_gmail_accounts.py. The shared
    test_config_mock_sheets fixture in conftest.py is stale — it constructs Config
    with 26 of the 35 required arguments — and no test currently exercises it.
    """
    return SimpleNamespace(
        discord=SimpleNamespace(enabled=False),
        user=SimpleNamespace(target_companies=[]),
    )


def _email(when: datetime, message_id: str = "msg-1") -> EmailMessage:
    """Build an EmailMessage with a specific arrival timestamp."""
    return EmailMessage(
        message_id=message_id,
        subject="Thanks for applying",
        sender="Recruiting",
        sender_email="noreply@example.com",
        date=when,
        body_text="body",
        body_html="",
        category="Primary",
    )


def _classification(category: str, date_mentioned=None) -> EmailClassification:
    return EmailClassification(
        category=category,
        confidence=0.95,
        company_candidates=["Uber"],
        position="SWE Intern",
        date_mentioned=date_mentioned,
    )


def _checker(config, sheets):
    """Build a GmailChecker wired to a mock sheet, with Gmail/AI stubbed out."""
    with patch("check_gmail.get_gmail_clients", return_value=[MagicMock()]), \
         patch("check_gmail.get_classifier"), \
         patch("check_gmail.get_sheets_client", return_value=sheets):
        from check_gmail import GmailChecker

        return GmailChecker(config)


def _seed_job(sheets, company: str = "Uber") -> int:
    """Add a job row and return its row number."""
    return sheets.add_job({"company": company, "position": "SWE Intern"})


def _cell(sheets, row_number: int, column: str) -> str:
    return sheets.rows[row_number - 1][COLUMNS[column]]


# =============================================================================
# parse_sheet_datetime
# =============================================================================

class TestParseSheetDatetime:
    """Every shape a date cell can arrive in."""

    @pytest.mark.parametrize("text", ["7/23/2026 20:28:52", "07/23/2026 20:28:52"])
    def test_timestamp_either_padding(self, text):
        assert parse_sheet_datetime(text) == datetime(2026, 7, 23, 20, 28, 52)

    @pytest.mark.parametrize("text", ["7/23/2026", "07/23/2026"])
    def test_date_only_either_padding(self, text):
        assert parse_sheet_datetime(text) == datetime(2026, 7, 23)

    def test_minutes_without_seconds(self):
        assert parse_sheet_datetime("7/23/2026 20:28") == datetime(2026, 7, 23, 20, 28)

    def test_iso(self):
        assert parse_sheet_datetime("2026-07-23") == datetime(2026, 7, 23)
        assert parse_sheet_datetime("2026-07-23T20:28:52") == datetime(2026, 7, 23, 20, 28, 52)

    def test_sheets_serial(self):
        # Serial 25569 is the Unix epoch; the fraction carries the time of day.
        assert parse_sheet_datetime("25569") == datetime(1970, 1, 1)
        assert parse_sheet_datetime("25569.5") == datetime(1970, 1, 1, 12, 0)

    def test_datetime_passes_through(self):
        moment = datetime(2026, 7, 23, 20, 28, 52)
        assert parse_sheet_datetime(moment) is moment

    def test_surrounding_whitespace(self):
        assert parse_sheet_datetime("  07/23/2026  ") == datetime(2026, 7, 23)

    @pytest.mark.parametrize("value", [None, "", "   ", "TBD", "next Tuesday", "inf", "nan"])
    def test_unparseable(self, value):
        assert parse_sheet_datetime(value) is None


# =============================================================================
# split_date_cell
# =============================================================================

class TestSplitDateCell:

    def test_multi(self):
        assert split_date_cell("7/23/2026; 08/01/2026") == ["7/23/2026", "08/01/2026"]

    def test_single(self):
        assert split_date_cell("7/23/2026") == ["7/23/2026"]

    def test_serial_float(self):
        assert split_date_cell(46226.85) == ["46226.85"]

    def test_trailing_separator_and_blanks(self):
        assert split_date_cell("7/23/2026; ") == ["7/23/2026"]

    @pytest.mark.parametrize("value", [None, "", "   ", ";"])
    def test_empty(self, value):
        assert split_date_cell(value) == []


# =============================================================================
# date_already_recorded
# =============================================================================

class TestDateAlreadyRecorded:
    """The regression core: the cell is read as FORMATTED_VALUE (unpadded, with a
    time) while the incoming value is zero-padded and may be date-only."""

    def test_row_306_application_date(self):
        # Uber row 306 verbatim: a timestamp already in the cell, a date-only value
        # incoming. A raw string compare misses this and appends.
        assert date_already_recorded("7/23/2026 20:28:52", "07/23/2026") is True

    def test_row_317_oa_date(self):
        # Akuna row 317: the DATE-column variant, times swapped around.
        assert date_already_recorded("7/23/2026", "07/23/2026 20:28:52") is True

    def test_already_present_in_multi_date_cell(self):
        assert date_already_recorded("7/23/2026; 8/1/2026", "08/01/2026") is True

    def test_genuinely_new_date_still_appends(self):
        # The guard against over-fixing: a real second round must not be suppressed.
        assert date_already_recorded("7/23/2026", "08/01/2026") is False

    def test_identical_strings(self):
        assert date_already_recorded("07/23/2026", "07/23/2026") is True

    def test_serial_against_string(self):
        assert date_already_recorded("25569", "01/01/1970") is True

    def test_unparseable_but_identical_text(self):
        assert date_already_recorded("TBD", "TBD") is True

    def test_unparseable_existing_does_not_block_a_real_date(self):
        assert date_already_recorded("TBD", "07/23/2026") is False

    def test_empty_cell(self):
        assert date_already_recorded("", "07/23/2026") is False


# =============================================================================
# add_date_to_column (through the mock sheet)
# =============================================================================

class TestAddDateToColumn:

    def test_same_date_twice_stays_single(self, mock_sheets_client):
        row = _seed_job(mock_sheets_client)
        mock_sheets_client.add_date_to_column(row, "oa_date", "07/23/2026")
        mock_sheets_client.add_date_to_column(row, "oa_date", "07/23/2026")
        assert _cell(mock_sheets_client, row, "oa_date") == "07/23/2026"

    def test_same_day_different_format_stays_single(self, mock_sheets_client):
        row = _seed_job(mock_sheets_client)
        mock_sheets_client.add_date_to_column(row, "oa_date", "7/23/2026 20:28:52")
        mock_sheets_client.add_date_to_column(row, "oa_date", "07/23/2026")
        assert ";" not in _cell(mock_sheets_client, row, "oa_date")

    def test_different_date_appends(self, mock_sheets_client):
        row = _seed_job(mock_sheets_client)
        mock_sheets_client.add_date_to_column(row, "oa_date", "07/23/2026")
        mock_sheets_client.add_date_to_column(row, "oa_date", "08/01/2026")
        assert _cell(mock_sheets_client, row, "oa_date") == "07/23/2026; 08/01/2026"


# =============================================================================
# Application Date (column F)
# =============================================================================

class TestApplicationDate:

    def test_ignores_date_mentioned(self, checker_config, mock_sheets_client):
        """A confirmation body mentioning an OA deadline must not set the app date."""
        row = _seed_job(mock_sheets_client)
        checker = _checker(checker_config, mock_sheets_client)
        job = mock_sheets_client.get_all_jobs()[0]

        checker._update_job_status(
            job,
            _classification("confirmation", date_mentioned="2026-08-15"),
            _email(datetime(2026, 7, 23, 20, 28, 52)),
        )

        assert _cell(mock_sheets_client, row, "application_date") == "07/23/2026 20:28:52"

    def test_write_once(self, checker_config, mock_sheets_client):
        row = _seed_job(mock_sheets_client)
        checker = _checker(checker_config, mock_sheets_client)

        checker._update_job_status(
            mock_sheets_client.get_all_jobs()[0],
            _classification("confirmation"),
            _email(datetime(2026, 7, 23, 20, 28, 52)),
        )
        first = _cell(mock_sheets_client, row, "application_date")

        # A second confirmation for the same job, days later.
        checker._update_job_status(
            mock_sheets_client.get_all_jobs()[0],
            _classification("confirmation"),
            _email(datetime(2026, 7, 25, 9, 0, 0), message_id="msg-2"),
        )

        assert _cell(mock_sheets_client, row, "application_date") == first

    def test_never_accumulates(self, checker_config, mock_sheets_client):
        """Reproduces the exact row 306 sequence: a first confirmation with no date in
        the body, then a second one that does mention a date. Pre-fix this produced
        "7/23/2026 20:28:52; 08/15/2026"."""
        row = _seed_job(mock_sheets_client)
        checker = _checker(checker_config, mock_sheets_client)

        for i, (when, mentioned) in enumerate([
            (datetime(2026, 7, 23, 20, 28, 52), None),
            (datetime(2026, 7, 25, 9, 0, 0), "2026-08-15"),
        ]):
            checker._update_job_status(
                mock_sheets_client.get_all_jobs()[0],
                _classification("confirmation", date_mentioned=mentioned),
                _email(when, message_id=f"msg-{i}"),
            )

        cell = _cell(mock_sheets_client, row, "application_date")
        assert ";" not in cell
        assert cell == "07/23/2026 20:28:52"

    def test_status_still_applied(self, checker_config, mock_sheets_client):
        """application_date now rides the status batch — check status still lands."""
        row = _seed_job(mock_sheets_client)
        checker = _checker(checker_config, mock_sheets_client)

        checker._update_job_status(
            mock_sheets_client.get_all_jobs()[0],
            _classification("confirmation"),
            _email(datetime(2026, 7, 23, 20, 28, 52)),
        )

        assert _cell(mock_sheets_client, row, "status") == "Applied"


# =============================================================================
# Event date columns (G / H / I)
# =============================================================================

class TestEventDateColumns:

    def test_prefers_date_mentioned(self, checker_config, mock_sheets_client):
        row = _seed_job(mock_sheets_client)
        checker = _checker(checker_config, mock_sheets_client)

        checker._update_job_status(
            mock_sheets_client.get_all_jobs()[0],
            _classification("oa", date_mentioned="2026-08-15"),
            _email(datetime(2026, 7, 23, 20, 28, 52)),
        )

        assert _cell(mock_sheets_client, row, "oa_date") == "08/15/2026"

    def test_fallback_is_date_only(self, checker_config, mock_sheets_client):
        """Columns G/H/I carry a DATE format, so no time component belongs here."""
        row = _seed_job(mock_sheets_client)
        checker = _checker(checker_config, mock_sheets_client)

        checker._update_job_status(
            mock_sheets_client.get_all_jobs()[0],
            _classification("oa"),
            _email(datetime(2026, 7, 23, 20, 28, 52)),
        )

        assert _cell(mock_sheets_client, row, "oa_date") == "07/23/2026"

    def test_reprocessing_same_email_is_idempotent(
        self, checker_config, mock_sheets_client
    ):
        """The value derives from the email, not the clock, so a re-run re-derives it."""
        row = _seed_job(mock_sheets_client)
        checker = _checker(checker_config, mock_sheets_client)
        email = _email(datetime(2026, 7, 23, 20, 28, 52))

        for _ in range(2):
            checker._update_job_status(
                mock_sheets_client.get_all_jobs()[0], _classification("oa"), email
            )

        assert _cell(mock_sheets_client, row, "oa_date") == "07/23/2026"

    def test_second_round_accumulates(self, checker_config, mock_sheets_client):
        row = _seed_job(mock_sheets_client)
        checker = _checker(checker_config, mock_sheets_client)

        for i, mentioned in enumerate(["2026-08-15", "2026-08-16"]):
            checker._update_job_status(
                mock_sheets_client.get_all_jobs()[0],
                _classification("oa", date_mentioned=mentioned),
                _email(datetime(2026, 7, 23, 20, 28, 52), message_id=f"msg-{i}"),
            )

        assert _cell(mock_sheets_client, row, "oa_date") == "08/15/2026; 08/16/2026"

    @pytest.mark.parametrize("category,column", [
        ("oa", "oa_date"),
        ("phone", "phone_date"),
        ("technical", "tech_date"),
    ])
    def test_category_routing(
        self, checker_config, mock_sheets_client, category, column
    ):
        row = _seed_job(mock_sheets_client)
        checker = _checker(checker_config, mock_sheets_client)

        checker._update_job_status(
            mock_sheets_client.get_all_jobs()[0],
            _classification(category, date_mentioned="2026-08-15"),
            _email(datetime(2026, 7, 23, 20, 28, 52)),
        )

        assert _cell(mock_sheets_client, row, column) == "08/15/2026"

    def test_unparseable_date_mentioned_falls_back(
        self, checker_config, mock_sheets_client
    ):
        row = _seed_job(mock_sheets_client)
        checker = _checker(checker_config, mock_sheets_client)

        checker._update_job_status(
            mock_sheets_client.get_all_jobs()[0],
            _classification("oa", date_mentioned="whenever you're ready"),
            _email(datetime(2026, 7, 23, 20, 28, 52)),
        )

        assert _cell(mock_sheets_client, row, "oa_date") == "07/23/2026"


# =============================================================================
# Email timestamp localization
# =============================================================================

class TestEmailTimestampLocalization:
    """parsedate_to_datetime() returns the *sender's* UTC offset; the sheet and the
    daily-summary windows are local naive."""

    def test_aware_timestamp_is_localized(self, checker_config, mock_sheets_client):
        row = _seed_job(mock_sheets_client)
        checker = _checker(checker_config, mock_sheets_client)
        sent = datetime(2026, 7, 23, 20, 28, 52, tzinfo=timezone(timedelta(hours=-7)))

        checker._update_job_status(
            mock_sheets_client.get_all_jobs()[0],
            _classification("confirmation"),
            _email(sent),
        )

        expected = sent.astimezone().replace(tzinfo=None).strftime("%m/%d/%Y %H:%M:%S")
        assert _cell(mock_sheets_client, row, "application_date") == expected

    def test_naive_timestamp_passes_through(self):
        from check_gmail import GmailChecker

        moment = datetime(2026, 7, 23, 20, 28, 52)
        assert GmailChecker._local_naive(moment) == moment

    def test_missing_timestamp_falls_back_to_now(self):
        from check_gmail import GmailChecker

        result = GmailChecker._local_naive(None)
        assert result.tzinfo is None
        assert abs((datetime.now() - result).total_seconds()) < 5


# =============================================================================
# Row color on append
# =============================================================================

STATUS_COLOR_KEYS = [
    "STATUS_COLOR_NEW", "STATUS_COLOR_APPLIED", "STATUS_COLOR_OA",
    "STATUS_COLOR_PHONE", "STATUS_COLOR_TECHNICAL", "STATUS_COLOR_OFFER",
    "STATUS_COLOR_REJECTED",
]


@pytest.fixture
def no_status_color_env(monkeypatch):
    """A clean slate — the developer's own .env must not decide these assertions."""
    for key in STATUS_COLOR_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestStatusColorConfig:
    """"New" has to carry a color like any other status, or nothing ever paints a
    freshly appended row."""

    def test_new_defaults_to_white(self, no_status_color_env):
        assert _parse_status_colors()["New"] == "#FFFFFF"

    def test_new_is_overridable(self, no_status_color_env, monkeypatch):
        monkeypatch.setenv("STATUS_COLOR_NEW", "#EEEEEE")
        assert _parse_status_colors()["New"] == "#EEEEEE"

    def test_other_statuses_still_opt_in(self, no_status_color_env):
        """Only "New" gets a fallback; an unset status stays absent so the row is
        left alone rather than repainted."""
        assert "OA" not in _parse_status_colors()


class TestAppendedRowColor:
    """The regression: values().append(insertDataOption="INSERT_ROWS") makes the new
    row inherit the formatting of the row above it, so a job landing under a colored
    OA row came out OA-colored."""

    @pytest.fixture
    def client(self):
        """A real SheetsClient with the Google service mocked out."""
        config = SimpleNamespace(
            google_sheet_id="sheet-123",
            jobs_sheet_tab="Jobs",
            status_colors={"New": "#FFFFFF", "OA": "#B3E5FC"},
        )
        client = SheetsClient(config=config)

        service = MagicMock()
        service.spreadsheets().values().append().execute.return_value = {
            "updates": {"updatedRange": "Jobs!A306:U306"}
        }
        client._service = service
        client._get_jobs_sheet_id = MagicMock(return_value=0)

        return client

    def _color_requests(self, client):
        """Every backgroundColor repeatCell the client sent, as (row, hex)."""
        found = []
        batch_update = client._service.spreadsheets().batchUpdate
        for call in batch_update.call_args_list:
            for request in call.kwargs.get("body", {}).get("requests", []):
                cell = request.get("repeatCell")
                if not cell or "backgroundColor" not in cell["cell"]["userEnteredFormat"]:
                    continue
                rgb = cell["cell"]["userEnteredFormat"]["backgroundColor"]
                hex_color = "#" + "".join(
                    f"{round(rgb[c] * 255):02X}" for c in ("red", "green", "blue")
                )
                found.append((cell["range"]["startRowIndex"] + 1, hex_color))
        return found

    def test_new_row_is_painted_white(self, client):
        row = client.add_job({"company": "Uber", "position": "SWE Intern"})

        assert row == 306
        assert self._color_requests(client) == [(306, "#FFFFFF")]

    def test_covers_the_full_row(self, client):
        client.add_job({"company": "Uber", "position": "SWE Intern"})

        request = client._service.spreadsheets().batchUpdate.call_args.kwargs["body"]
        cell_range = request["requests"][0]["repeatCell"]["range"]
        assert cell_range["startColumnIndex"] == 0
        assert cell_range["endColumnIndex"] == len(COLUMNS)

    def test_explicit_status_wins_over_white(self, client):
        """A row added already in a non-New status gets that status's color."""
        client.add_job({"company": "Uber", "position": "SWE Intern", "status": "OA"})

        assert self._color_requests(client) == [(306, "#B3E5FC")]

    def test_color_failure_does_not_lose_the_row(self, client):
        """The append already succeeded — a formatting error must not surface as a
        failed add, or the caller marks a written job as skipped."""
        client._service.spreadsheets().batchUpdate.side_effect = RuntimeError("boom")

        assert client.add_job({"company": "Uber", "position": "SWE Intern"}) == 306

    def test_unparseable_range_skips_coloring(self, client):
        """row_num == -1 means we don't know which row to paint; painting row 0 would
        recolor the header."""
        client._service.spreadsheets().values().append().execute.return_value = {}

        assert client.add_job({"company": "Uber", "position": "SWE Intern"}) == -1
        assert self._color_requests(client) == []


# =============================================================================
# Jobs tab name + gid resolution
# =============================================================================

def _client(tab: str = "Jobs") -> SheetsClient:
    """A SheetsClient whose service returns one spreadsheet with `tab` at gid 42."""
    config = SimpleNamespace(
        google_sheet_id="sheet-123", jobs_sheet_tab=tab, status_colors={},
    )
    client = SheetsClient(config=config)

    service = MagicMock()
    service.spreadsheets().get().execute.return_value = {
        "sheets": [
            {"properties": {"title": "Other", "sheetId": 7}},
            {"properties": {"title": tab, "sheetId": 42}},
        ]
    }
    client._service = service
    return client


class TestJobsTabName:
    """The tab was hardcoded as "Jobs" in 13 places; it now comes from
    JOBS_SHEET_TAB."""

    def test_range_uses_configured_tab(self):
        assert _client("Applications")._range("A:U") == "'Applications'!A:U"

    def test_tab_is_quoted(self):
        """A name with a space is invalid A1 notation unquoted."""
        assert _client("Job Board")._range("A2:U") == "'Job Board'!A2:U"

    def test_apostrophe_in_tab_name(self):
        """Sheets escapes a literal ' by doubling it."""
        assert _client("Sam's Jobs")._range("A1") == "'Sam''s Jobs'!A1"

    def test_gid_lookup_matches_on_title(self):
        assert _client("Applications")._get_jobs_sheet_id() == 42

    def test_gid_falls_back_when_tab_missing(self):
        client = _client("Jobs")
        client._service.spreadsheets().get().execute.return_value = {"sheets": []}
        assert client._get_jobs_sheet_id() == 0

    def test_appended_row_number_survives_a_quoted_tab(self):
        """The row number is parsed out of updatedRange, which comes back quoted for
        a tab name like this — and split from the right, since the name has a "!"."""
        client = _client("Jobs!Live")
        client._service.spreadsheets().values().append().execute.return_value = {
            "updates": {"updatedRange": "'Jobs!Live'!A306:U306"}
        }
        assert client.add_job({"company": "Uber"}) == 306


class TestJobsSheetIdCache:
    """The gid costs an API call and cannot change under a live client."""

    def _lookup_count(self, client) -> int:
        return client._service.spreadsheets().get().execute.call_count

    def test_resolved_once_across_calls(self):
        client = _client()
        before = self._lookup_count(client)

        for _ in range(5):
            client._get_jobs_sheet_id()

        assert self._lookup_count(client) - before == 1

    def test_creating_the_tab_invalidates_it(self):
        """A tab created this run has a gid we never saw; a stale 0 would format the
        wrong tab."""
        client = _client()
        client._jobs_sheet_id = 42
        client._service.spreadsheets().get().execute.return_value = {"sheets": []}

        client._ensure_jobs_sheet_exists()

        assert client._jobs_sheet_id is None

"""
Tests for the Last Email Time column (V) and the stale-email guard.

Column V records the arrival time of the most recent email that updated a row.
check_gmail.py refuses to apply any email that is not strictly newer, so an older
message processed after a newer one can no longer walk a row backwards.

Also covers the schema de-hardcoding that landed alongside it: col_letter() past Z,
the name-declared number-format tuples, and ensure_headers() idempotence.

Usage:
    pytest tests/test_last_email_time.py -v
    pytest tests/test_last_email_time.py::TestStaleGuard -v
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.email_classifier import EmailClassification
from src.gmail import EmailMessage
from src.sheets import (
    COLUMNS,
    DATE_COLUMNS,
    DATETIME_COLUMNS,
    HEADERS,
    LAST_COL,
    SheetsClient,
    col_letter,
)
from tests.mocks.mock_sheets import MockSheetsClient


# =============================================================================
# Helpers
# =============================================================================

@pytest.fixture
def checker_config():
    """Minimal stand-in exposing only what GmailChecker reads."""
    return SimpleNamespace(
        discord=SimpleNamespace(enabled=False),
        user=SimpleNamespace(target_companies=[]),
    )


def _email(when: datetime, message_id: str = "msg-1") -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        subject="Update on your application",
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


def _checker(config, sheets, reprocess: bool = False):
    with patch("check_gmail.get_gmail_clients", return_value=[MagicMock()]), \
         patch("check_gmail.get_classifier"), \
         patch("check_gmail.get_sheets_client", return_value=sheets):
        from check_gmail import GmailChecker

        return GmailChecker(config, reprocess=reprocess)


def _seed_job(sheets, company: str = "Uber") -> int:
    return sheets.add_job({"company": company, "position": "SWE Intern"})


def _cell(sheets, row_number: int, column: str) -> str:
    return sheets.rows[row_number - 1][COLUMNS[column]]


# =============================================================================
# Schema shape
# =============================================================================

class TestSchema:
    def test_last_email_time_is_column_v(self):
        assert COLUMNS["last_email_time"] == 21
        assert col_letter(COLUMNS["last_email_time"]) == "V"

    def test_stage_tracking_columns_are_last(self):
        """
        Completed Stages (W) and Last Email Category (X) were appended together. This
        pins the width so a future column has to update LAST_COL deliberately — every A1
        range in the client derives from it.
        """
        assert col_letter(COLUMNS["completed_stages"]) == "W"
        assert col_letter(COLUMNS["last_event"]) == "X"
        assert LAST_COL == "X"

    def test_header_present_and_aligned(self):
        assert len(HEADERS) == len(COLUMNS)
        assert HEADERS[COLUMNS["last_email_time"]] == "Last Email Time"

    def test_job_row_defaults_to_blank(self):
        """Pre-existing rows are short; from_row pads rather than raising."""
        from src.sheets import JobRow

        job = JobRow.from_row(5, ["Uber", "SWE Intern"])
        assert job.last_email_time == ""

    def test_job_row_reads_the_cell(self):
        from src.sheets import JobRow

        values = [""] * len(COLUMNS)
        values[COLUMNS["last_email_time"]] = "07/23/2026 20:28:52"
        assert JobRow.from_row(5, values).last_email_time == "07/23/2026 20:28:52"


class TestColLetter:
    @pytest.mark.parametrize("index,expected", [
        (0, "A"), (5, "F"), (20, "U"), (21, "V"), (25, "Z"),
        (26, "AA"), (27, "AB"), (51, "AZ"), (52, "BA"), (701, "ZZ"), (702, "AAA"),
    ])
    def test_letters(self, index, expected):
        assert col_letter(index) == expected

    def test_past_z_is_not_a_bracket(self):
        """The bug in chr(ord("A") + idx), which the schema is one column from hitting."""
        assert col_letter(26) != "["


class TestFormatColumnTuples:
    def test_every_name_is_a_real_column(self):
        for name in DATE_COLUMNS + DATETIME_COLUMNS:
            assert name in COLUMNS, name

    def test_date_and_datetime_are_disjoint(self):
        assert not set(DATE_COLUMNS) & set(DATETIME_COLUMNS)

    def test_last_email_time_carries_a_time(self):
        """Column V stores a timestamp, so it must format like F and R, not like G."""
        assert "last_email_time" in DATETIME_COLUMNS


# =============================================================================
# The guard
# =============================================================================

class TestStaleGuard:
    """_update_job_status must not touch the row for an out-of-date email."""

    def test_blank_cell_lets_the_email_through(self, checker_config):
        sheets = MockSheetsClient()
        row = _seed_job(sheets)
        checker = _checker(checker_config, sheets)

        assert _cell(sheets, row, "last_email_time") == ""

        updated = checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("confirmation"),
            _email(datetime(2026, 7, 23, 20, 28, 52)),
        )

        assert updated is True
        assert _cell(sheets, row, "last_email_time") == "07/23/2026 20:28:52"
        assert _cell(sheets, row, "status") == "Applied"

    def test_newer_email_applies_and_advances_the_stamp(self, checker_config):
        sheets = MockSheetsClient()
        row = _seed_job(sheets)
        checker = _checker(checker_config, sheets)

        checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("confirmation"),
            _email(datetime(2026, 7, 23, 20, 28, 52), message_id="first"),
        )
        updated = checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("rejection"),
            _email(datetime(2026, 7, 30, 9, 4, 11), message_id="second"),
        )

        assert updated is True
        assert _cell(sheets, row, "status") == "Rejected"
        assert _cell(sheets, row, "last_email_time") == "07/30/2026 09:04:11"

    def test_older_email_cannot_walk_the_status_backwards(self, checker_config):
        """The bug: a stale confirmation arriving after a rejection."""
        sheets = MockSheetsClient()
        row = _seed_job(sheets)
        checker = _checker(checker_config, sheets)

        checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("rejection"),
            _email(datetime(2026, 7, 30, 9, 4, 11), message_id="new"),
        )
        updated = checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("confirmation"),
            _email(datetime(2026, 7, 23, 20, 28, 52), message_id="old"),
        )

        assert updated is False
        assert checker.stats["stale_skipped"] == 1
        assert _cell(sheets, row, "status") == "Rejected"
        assert _cell(sheets, row, "last_email_time") == "07/30/2026 09:04:11"

    def test_equal_timestamp_is_not_newer(self, checker_config):
        sheets = MockSheetsClient()
        _seed_job(sheets)
        checker = _checker(checker_config, sheets)

        when = datetime(2026, 7, 23, 20, 28, 52)
        checker._update_job_status(
            sheets.get_all_jobs()[0], _classification("oa"), _email(when, "a")
        )
        updated = checker._update_job_status(
            sheets.get_all_jobs()[0], _classification("offer"), _email(when, "b")
        )

        assert updated is False
        assert checker.stats["stale_skipped"] == 1

    def test_stale_email_writes_nothing_at_all(self, checker_config):
        """Not just status — the date, notes and color paths are all downstream."""
        sheets = MagicMock()
        sheets.get_struck_rows.return_value = set()
        job = SimpleNamespace(
            row_number=7,
            company="Uber",
            position="SWE Intern",
            status="Rejected",
            application_date="",
            notes="",
            last_email_time="07/30/2026 09:04:11",
        )
        checker = _checker(checker_config, sheets)

        updated = checker._update_job_status(
            job,
            _classification("offer", date_mentioned="2026-08-15"),
            _email(datetime(2026, 7, 23, 20, 28, 52)),
        )

        assert updated is False
        sheets.update_job.assert_not_called()
        sheets.add_date_to_column.assert_not_called()
        sheets.append_to_notes.assert_not_called()
        sheets.apply_status_color.assert_not_called()

    def test_stale_email_sends_no_discord_notification(self, checker_config):
        checker_config.discord = SimpleNamespace(enabled=True)
        checker_config.user = SimpleNamespace(target_companies=["Uber"])

        sheets = MagicMock()
        job = SimpleNamespace(
            row_number=7,
            company="Uber",
            position="SWE Intern",
            status="Rejected",
            application_date="",
            notes="",
            last_email_time="07/30/2026 09:04:11",
            position_url="",
        )
        checker = _checker(checker_config, sheets)

        with patch("check_gmail.notify_status_change") as notify:
            checker._update_job_status(
                job,
                _classification("confirmation"),
                _email(datetime(2026, 7, 23, 20, 28, 52)),
            )

        notify.assert_not_called()

    def test_newer_confirmation_cannot_undo_an_oa(self, checker_config):
        """
        The row 404 regression. Castleton's assessment vendor and its ATS mailed three
        seconds apart, the "Thank You for Applying" second, so the confirmation was
        genuinely the newer message and sailed past the stale guard — resetting a row
        that had just reached OA back to Applied.

        The email is still recorded (its stamp advances); only the backwards status
        move is refused.
        """
        sheets = MockSheetsClient()
        row = _seed_job(sheets)
        checker = _checker(checker_config, sheets)

        checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("oa"),
            _email(datetime(2026, 8, 2, 19, 54, 3), message_id="assessment"),
        )
        assert _cell(sheets, row, "status") == "OA"

        updated = checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("confirmation"),
            _email(datetime(2026, 8, 2, 19, 54, 6), message_id="ats-confirmation"),
        )

        assert updated is True
        assert _cell(sheets, row, "status") == "OA"
        assert _cell(sheets, row, "last_email_time") == "08/02/2026 19:54:06"
        assert checker.stats["status_regression_blocked"] == 1

    def test_held_status_keeps_its_own_colour(self, checker_config):
        """Recolouring to a stage the row is not at is its own kind of wrong answer."""
        sheets = MagicMock()
        sheets.get_struck_rows.return_value = set()
        job = SimpleNamespace(
            row_number=404, company="Castleton", position="Full-Stack SWE Intern",
            status="OA", application_date="", notes="", last_email_time="",
            position_url="",
        )
        checker = _checker(checker_config, sheets)

        checker._update_job_status(
            job, _classification("confirmation"), _email(datetime(2026, 8, 2, 19, 54, 6))
        )

        sheets.apply_status_color.assert_called_once_with(404, "OA")

    def test_held_status_sends_no_discord_notification(self, checker_config):
        """No stage change happened, so announcing one would be a lie."""
        checker_config.discord = SimpleNamespace(enabled=True)
        checker_config.user = SimpleNamespace(target_companies=["Castleton"])

        sheets = MagicMock()
        sheets.get_struck_rows.return_value = set()
        job = SimpleNamespace(
            row_number=404, company="Castleton", position="Full-Stack SWE Intern",
            status="OA", application_date="", notes="", last_email_time="",
            position_url="",
        )
        checker = _checker(checker_config, sheets)

        with patch("check_gmail.notify_status_change") as notify:
            checker._update_job_status(
                job, _classification("confirmation"),
                _email(datetime(2026, 8, 2, 19, 54, 6)),
            )

        notify.assert_not_called()

    @pytest.mark.parametrize("category,expected", [
        ("oa", "OA"),            # same stage — no progress to make
        ("confirmation", "OA"),  # earlier stage
        ("phone", "Phone"),      # later stage still advances
        ("rejection", "Rejected"),  # terminal, allowed from any stage
    ])
    def test_only_forward_moves_and_terminals_apply(self, checker_config, category, expected):
        sheets = MockSheetsClient()
        row = _seed_job(sheets)
        checker = _checker(checker_config, sheets)

        checker._update_job_status(
            sheets.get_all_jobs()[0], _classification("oa"),
            _email(datetime(2026, 8, 2, 10, 0, 0), message_id="first"),
        )
        checker._update_job_status(
            sheets.get_all_jobs()[0], _classification(category),
            _email(datetime(2026, 8, 2, 11, 0, 0), message_id="second"),
        )

        assert _cell(sheets, row, "status") == expected

    @pytest.mark.parametrize("garbage", ["TBD", "next Tuesday", "   "])
    def test_unparseable_cell_does_not_freeze_the_row(self, checker_config, garbage):
        sheets = MockSheetsClient()
        row = _seed_job(sheets)
        sheets.update_job(row, {"last_email_time": garbage})
        checker = _checker(checker_config, sheets)

        updated = checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("confirmation"),
            _email(datetime(2026, 7, 23, 20, 28, 52)),
        )

        assert updated is True
        assert _cell(sheets, row, "last_email_time") == "07/23/2026 20:28:52"

    def test_sheets_serial_cell_compares_correctly(self, checker_config):
        """valueRenderOption=FORMULA returns a DATE_TIME cell as a serial number."""
        sheets = MockSheetsClient()
        row = _seed_job(sheets)
        # 46236.37787... is 07/30/2026 09:04:11 in Sheets serial form.
        serial = 46236 + (9 * 3600 + 4 * 60 + 11) / 86400
        sheets.update_job(row, {"last_email_time": str(serial)})
        checker = _checker(checker_config, sheets)

        updated = checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("confirmation"),
            _email(datetime(2026, 7, 23, 20, 28, 52)),
        )

        assert updated is False
        assert checker.stats["stale_skipped"] == 1

    def test_tz_aware_email_is_localized_before_comparing(self, checker_config):
        """
        parsedate_to_datetime carries the *sender's* offset; the cell is local naive.
        Comparing them raw would either raise or shift the hour.
        """
        sheets = MockSheetsClient()
        row = _seed_job(sheets)
        checker = _checker(checker_config, sheets)

        aware = datetime(2026, 7, 23, 20, 28, 52, tzinfo=timezone(timedelta(hours=-4)))
        updated = checker._update_job_status(
            sheets.get_all_jobs()[0], _classification("confirmation"), _email(aware)
        )

        assert updated is True
        stored = _cell(sheets, row, "last_email_time")
        expected = aware.astimezone().replace(tzinfo=None).strftime("%m/%d/%Y %H:%M:%S")
        assert stored == expected

        # And the localized value round-trips: replaying the same email is now stale.
        assert checker._is_newer_than_last_email(sheets.get_all_jobs()[0], _email(aware)) is False


class TestReprocessBypass:
    """--reprocess exists to re-walk old mail; the guard must not neuter it."""

    def test_equal_timestamp_still_applies(self, checker_config):
        sheets = MockSheetsClient()
        row = _seed_job(sheets)
        checker = _checker(checker_config, sheets, reprocess=True)

        when = datetime(2026, 7, 23, 20, 28, 52)
        checker._update_job_status(
            sheets.get_all_jobs()[0], _classification("confirmation"), _email(when, "a")
        )
        updated = checker._update_job_status(
            sheets.get_all_jobs()[0], _classification("rejection"), _email(when, "b")
        )

        assert updated is True
        assert checker.stats["stale_skipped"] == 0
        assert _cell(sheets, row, "status") == "Rejected"

    def test_older_email_still_applies(self, checker_config):
        sheets = MockSheetsClient()
        row = _seed_job(sheets)
        checker = _checker(checker_config, sheets, reprocess=True)

        checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("oa"),
            _email(datetime(2026, 7, 30, 9, 4, 11), "new"),
        )
        updated = checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("technical"),
            _email(datetime(2026, 7, 23, 20, 28, 52), "old"),
        )

        assert updated is True
        assert checker.stats["stale_skipped"] == 0
        assert _cell(sheets, row, "status") == "Technical"

    def test_the_older_email_still_writes_over_a_terminal_row(self, checker_config):
        """
        The staleness guard is bypassed; the regression guard is not, and never was.

        Written with a rejection first because that pairing used to assert the opposite:
        Rejected was absent from STATUS_RANK, so a replayed confirmation reset the row to
        Applied. Everything the email carries is still recorded — only the status holds.
        """
        sheets = MockSheetsClient()
        row = _seed_job(sheets)
        checker = _checker(checker_config, sheets, reprocess=True)

        checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("rejection"),
            _email(datetime(2026, 7, 30, 9, 4, 11), "new"),
        )
        updated = checker._update_job_status(
            sheets.get_all_jobs()[0],
            _classification("confirmation"),
            _email(datetime(2026, 7, 23, 20, 28, 52), "old"),
        )

        assert updated is True
        assert checker.stats["stale_skipped"] == 0
        assert checker.stats["status_regression_blocked"] == 1
        assert _cell(sheets, row, "status") == "Rejected"
        # The older email was still processed, which is what --reprocess is for.
        assert _cell(sheets, row, "last_email_time") == "07/23/2026 20:28:52"


# =============================================================================
# Batch ordering
# =============================================================================

class TestBatchOrdering:
    """
    Gmail returns newest-first. Processed in that order the guard would apply only
    the first email per row and drop the rest, so run() re-sorts oldest-first.
    """

    def test_newest_first_batch_still_applies_every_email(self, checker_config):
        sheets = MockSheetsClient()
        row = _seed_job(sheets)

        # Newest first, as the Gmail API hands them back.
        batch = [
            _email(datetime(2026, 7, 30, 9, 4, 11), "rejection"),
            _email(datetime(2026, 7, 26, 14, 0, 0), "oa"),
            _email(datetime(2026, 7, 23, 20, 28, 52), "confirmation"),
        ]
        by_id = {
            "rejection": _classification("rejection"),
            "oa": _classification("oa", date_mentioned="2026-08-15"),
            "confirmation": _classification("confirmation"),
        }

        client = MagicMock(label="me@example.com")
        client.fetch_recent_emails.return_value = batch

        classifier = MagicMock()
        classifier.classify.side_effect = lambda email: by_id[email.message_id]

        config = SimpleNamespace(
            discord=SimpleNamespace(enabled=False),
            user=SimpleNamespace(target_companies=[]),
            gmail_lookback_days=3,
        )

        with patch("check_gmail.get_gmail_clients", return_value=[client]), \
             patch("check_gmail.get_classifier", return_value=classifier), \
             patch("check_gmail.get_sheets_client", return_value=sheets), \
             patch("check_gmail.apply_privacy_filters", return_value=(True, "")):
            from check_gmail import GmailChecker

            stats = GmailChecker(config).run()

        assert stats["stale_skipped"] == 0
        assert stats["updated"] == 3
        # Final state is the newest email's, and the middle one was not lost.
        assert _cell(sheets, row, "status") == "Rejected"
        assert _cell(sheets, row, "application_date") == "07/23/2026 20:28:52"
        assert _cell(sheets, row, "oa_date") == "08/15/2026"
        assert _cell(sheets, row, "last_email_time") == "07/30/2026 09:04:11"


# =============================================================================
# ensure_headers idempotence
# =============================================================================

class TestEnsureHeadersIdempotence:
    """3 API calls each time, one of them a write — so do it once per process."""

    def _client(self):
        config = SimpleNamespace(
            google_sheet_id="sheet-123",
            jobs_sheet_tab="Jobs",
            auth_dir=None,
            google_credentials_path=None,
        )
        client = SheetsClient.__new__(SheetsClient)
        client.config = config
        client._service = MagicMock()
        client._creds = None
        client._jobs_sheet_id = 0
        client._schema_ensured = False
        return client

    def test_second_call_issues_no_api_calls(self):
        client = self._client()
        values = client._service.spreadsheets.return_value.values.return_value
        values.get.return_value.execute.return_value = {"values": [HEADERS]}
        client._service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Jobs", "sheetId": 0}}]
        }

        client.ensure_headers()
        first = client._service.spreadsheets.call_count
        assert first > 0

        client.ensure_headers()
        assert client._service.spreadsheets.call_count == first

    def test_failure_does_not_latch_the_flag(self):
        client = self._client()
        client._service.spreadsheets.return_value.get.return_value.execute.side_effect = (
            RuntimeError("network down")
        )

        with pytest.raises(RuntimeError):
            client.ensure_headers()

        assert client._schema_ensured is False

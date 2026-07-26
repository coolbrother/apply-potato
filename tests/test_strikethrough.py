"""
Tests for strikethrough as the ambiguous-match tie-breaker.

Covers SheetsClient.get_struck_rows() / set_row_strikethrough(), the matcher
skipping retired rows, and the row-selection logic in scripts/mark_canonical.py.

The point of all of it: a company with five rows cannot be matched from a generic
email. Striking four of them leaves one, and the match succeeds.

Usage:
    pytest tests/test_strikethrough.py -v
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.email_classifier import EmailClassification
from src.needs_review import REASON_ALL_RETIRED, REASON_AMBIGUOUS, REASON_UNTRACKED
from src.sheets import COLUMNS, SheetsClient
from scripts.mark_canonical import _matching_position, _same_company


# =============================================================================
# Helpers
# =============================================================================

@pytest.fixture
def checker_config(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return SimpleNamespace(
        data_dir=data_dir,
        gmail_lookback_days=1,  # read by run()'s opening log line
        discord=SimpleNamespace(enabled=False),
        user=SimpleNamespace(target_companies=[]),
    )


def _row(row_number: int, company: str, position: str, status: str = "New"):
    return SimpleNamespace(
        row_number=row_number,
        company=company,
        position=position,
        status=status,
        application_date="",
        notes="",
    )


def _classification(companies, position=None):
    return EmailClassification(
        category="confirmation",
        confidence=0.95,
        company_candidates=companies,
        position=position,
        date_mentioned=None,
    )


def _checker(config, sheets, struck=None):
    with patch("check_gmail.get_gmail_clients", return_value=[MagicMock()]), \
         patch("check_gmail.get_classifier"), \
         patch("check_gmail.get_sheets_client", return_value=sheets):
        from check_gmail import GmailChecker

        checker = GmailChecker(config)
        checker._struck_rows = struck or set()
        return checker


def _grid(*rows):
    """Build a spreadsheets().get() response from (strike_a, strike_b) pairs."""
    return {
        "sheets": [{
            "data": [{
                "rowData": [
                    {"values": [
                        {"effectiveFormat": {"textFormat": {"strikethrough": a}}},
                        {"effectiveFormat": {"textFormat": {"strikethrough": b}}},
                    ]}
                    for a, b in rows
                ]
            }]
        }]
    }


def _client_returning(response):
    """A SheetsClient whose spreadsheets().get() returns a canned grid response."""
    client = SheetsClient.__new__(SheetsClient)
    client.config = SimpleNamespace(google_sheet_id="sheet-1", jobs_sheet_tab="Jobs")
    client._jobs_sheet_id = 0
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.return_value = response
    client._get_service = lambda: service
    client._retry_with_backoff = lambda func: func()
    return client, service


# =============================================================================
# get_struck_rows
# =============================================================================

class TestGetStruckRows:

    def test_reports_struck_rows_as_sheet_row_numbers(self):
        """Row 1 is the header, so grid index 0 is sheet row 2."""
        client, _ = _client_returning(_grid((False, False), (True, True), (False, False)))

        assert client.get_struck_rows() == {3}

    def test_strikethrough_on_position_alone_counts(self):
        client, _ = _client_returning(_grid((False, True)))

        assert client.get_struck_rows() == {2}

    def test_strikethrough_on_company_alone_counts(self):
        client, _ = _client_returning(_grid((True, False)))

        assert client.get_struck_rows() == {2}

    def test_no_strikethrough_anywhere_is_empty(self):
        client, _ = _client_returning(_grid((False, False), (False, False)))

        assert client.get_struck_rows() == set()

    def test_missing_format_keys_are_treated_as_not_struck(self):
        """A never-formatted cell comes back as a bare {}."""
        response = {"sheets": [{"data": [{"rowData": [{"values": [{}, {}]}]}]}]}
        client, _ = _client_returning(response)

        assert client.get_struck_rows() == set()

    def test_empty_sheet_is_empty(self):
        client, _ = _client_returning({"sheets": []})

        assert client.get_struck_rows() == set()

    def test_reads_only_columns_a_and_b(self):
        """Formatting payloads are large; A:B is all the answer needs."""
        client, service = _client_returning(_grid((False, False)))
        client.get_struck_rows()

        kwargs = service.spreadsheets.return_value.get.call_args.kwargs
        assert kwargs["ranges"] == ["'Jobs'!A2:B"]
        assert kwargs["includeGridData"] is True


class TestSetRowStrikethrough:

    def _client(self):
        client = SheetsClient.__new__(SheetsClient)
        client.config = SimpleNamespace(google_sheet_id="sheet-1", jobs_sheet_tab="Jobs")
        client._jobs_sheet_id = 7
        service = MagicMock()
        client._get_service = lambda: service
        client._retry_with_backoff = lambda func: func()
        return client, service

    def _request(self, service):
        body = service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
        return body["requests"][0]["repeatCell"]

    def test_strikes_the_whole_row(self):
        client, service = self._client()
        client.set_row_strikethrough(315, True)

        req = self._request(service)
        assert req["range"] == {
            "sheetId": 7,
            "startRowIndex": 314,  # 0-indexed
            "endRowIndex": 315,
            "startColumnIndex": 0,
            "endColumnIndex": len(COLUMNS),
        }
        assert req["cell"]["userEnteredFormat"]["textFormat"]["strikethrough"] is True

    def test_clearing_writes_false_not_a_missing_key(self):
        """An absent key would leave the existing strikethrough in place."""
        client, service = self._client()
        client.set_row_strikethrough(315, False)

        req = self._request(service)
        assert req["cell"]["userEnteredFormat"]["textFormat"]["strikethrough"] is False

    def test_field_mask_touches_only_strikethrough(self):
        """A wider mask would wipe the status background colors."""
        client, service = self._client()
        client.set_row_strikethrough(315, True)

        assert self._request(service)["fields"] == "userEnteredFormat.textFormat.strikethrough"


# =============================================================================
# Matcher skips struck rows
# =============================================================================

class TestMatcherSkipsStruckRows:

    def test_striking_all_but_one_resolves_the_tie(self, checker_config):
        """The DRW case: five rows, four struck, the email now matches."""
        drw = [
            _row(315, "DRW", "Trade Support Intern"),
            _row(291, "DRW", "FPGA Intern"),
            _row(290, "DRW", "Quantitative Trading Analyst Intern"),
            _row(287, "DRW", "Software Developer Intern"),
            _row(282, "DRW", "Software Developer Intern"),
        ]
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = drw
        checker = _checker(checker_config, sheets, struck={291, 290, 287, 282})

        outcome = checker._find_matching_job(_classification(["DRW"]))

        assert outcome.matched is True
        assert outcome.job.row_number == 315

    def test_duplicate_rows_resolve_on_company_plus_position(self, checker_config):
        """The Appian/Akuna case: one role, two rows, different URLs."""
        sheets = MagicMock()
        sheets.find_jobs_by_company_and_position.return_value = [
            _row(312, "Appian", "Software Engineering Intern"),
            _row(347, "Appian", "Software Engineering Intern"),
        ]
        checker = _checker(checker_config, sheets, struck={347})

        outcome = checker._find_matching_job(
            _classification(["Appian"], "Software Engineering Intern")
        )

        assert outcome.matched is True
        assert outcome.job.row_number == 312

    def test_still_ambiguous_when_two_live_rows_remain(self, checker_config):
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = [
            _row(315, "DRW", "A"), _row(291, "DRW", "B"), _row(290, "DRW", "C"),
        ]
        checker = _checker(checker_config, sheets, struck={290})

        outcome = checker._find_matching_job(_classification(["DRW"]))

        assert outcome.reason == REASON_AMBIGUOUS
        assert [r.row_number for r in outcome.candidates] == [315, 291]

    def test_no_strikethrough_leaves_behaviour_unchanged(self, checker_config):
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = [
            _row(315, "DRW", "A"), _row(291, "DRW", "B"),
        ]
        checker = _checker(checker_config, sheets, struck=set())

        assert checker._find_matching_job(_classification(["DRW"])).reason == REASON_AMBIGUOUS

    def test_all_rows_struck_is_reported_separately_from_untracked(self, checker_config):
        """Striking every row is a user mistake worth naming, not an untracked company."""
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = [
            _row(315, "DRW", "A"), _row(291, "DRW", "B"),
        ]
        checker = _checker(checker_config, sheets, struck={315, 291})

        outcome = checker._find_matching_job(_classification(["DRW"]))

        assert outcome.reason == REASON_ALL_RETIRED
        assert outcome.company == "DRW"

    def test_genuinely_absent_company_is_still_untracked(self, checker_config):
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = []
        sheets.find_jobs_by_company_and_position.return_value = []
        checker = _checker(checker_config, sheets, struck={315})

        outcome = checker._find_matching_job(_classification(["Belvedere Trading"]))

        assert outcome.reason == REASON_UNTRACKED

    def test_position_lookup_falls_back_to_company_when_struck_empties_it(self, checker_config):
        """Striking the only position match must not abandon the company lookup."""
        sheets = MagicMock()
        sheets.find_jobs_by_company_and_position.return_value = [
            _row(347, "Appian", "Software Engineering Intern"),
        ]
        sheets.find_jobs_by_company.return_value = [
            _row(347, "Appian", "Software Engineering Intern"),
            _row(312, "Appian", "Data Intern"),
        ]
        checker = _checker(checker_config, sheets, struck={347})

        outcome = checker._find_matching_job(
            _classification(["Appian"], "Software Engineering Intern")
        )

        assert outcome.matched is True
        assert outcome.job.row_number == 312


class TestRunLoadsStruckRows:

    def test_run_populates_struck_rows_once(self, checker_config):
        sheets = MagicMock()
        sheets.get_struck_rows.return_value = {291, 290}
        checker = _checker(checker_config, sheets)
        checker.gmail_clients = []

        checker.run()

        assert checker._struck_rows == {291, 290}
        sheets.get_struck_rows.assert_called_once()

    def test_run_survives_a_failed_strikethrough_read(self, checker_config):
        """Losing the tie-breaker must not cost the run the emails it can match."""
        sheets = MagicMock()
        sheets.get_struck_rows.side_effect = RuntimeError("API down")
        checker = _checker(checker_config, sheets)
        checker.gmail_clients = []

        stats = checker.run()

        assert checker._struck_rows == set()
        assert stats["emails_fetched"] == 0


# =============================================================================
# mark_canonical row selection
# =============================================================================

class TestMarkCanonicalSelection:

    def test_same_company_is_exact_not_substring(self):
        """'DRW' must not drag in 'DRW Holdings' — striking is a write."""
        jobs = [
            _row(315, "DRW", "A"),
            _row(291, "DRW", "B"),
            _row(400, "DRW Holdings", "C"),
        ]

        assert [j.row_number for j in _same_company(jobs, "DRW")] == [315, 291]

    def test_same_company_ignores_case_and_padding(self):
        jobs = [_row(315, "  drw  ", "A"), _row(291, "DRW", "B")]

        assert len(_same_company(jobs, "DRW")) == 2

    def test_matching_position_is_substring(self):
        """The email's position text rarely matches the sheet's title exactly."""
        jobs = [
            _row(312, "Appian", "Software Engineering Intern"),
            _row(347, "Appian", "Software Engineering Intern - Summer 2027"),
            _row(350, "Appian", "Data Intern"),
        ]

        kept = _matching_position(jobs, "software engineering intern")
        assert [j.row_number for j in kept] == [312, 347]

    def test_matching_position_returns_nothing_on_a_typo(self):
        jobs = [_row(312, "Appian", "Software Engineering Intern")]

        assert _matching_position(jobs, "Softwre Engineering") == []

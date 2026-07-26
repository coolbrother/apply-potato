"""
Tests for the unmatched-status-email review queue.

Covers src/needs_review.py, the MatchOutcome reasons _find_matching_job() returns,
and the no-match path in _process_email() that writes the log entry.

The scenario driving all of this: a DRW confirmation naming no position arrived
while five DRW rows sat in the Sheet. The matcher correctly refused to guess, then
dropped the email with no trace.

Usage:
    pytest tests/test_needs_review.py -v
"""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.email_classifier import EmailClassification
from src.gmail import EmailMessage
from src.needs_review import (
    REASON_AMBIGUOUS,
    REASON_NO_COMPANY,
    REASON_UNTRACKED,
    load_needs_review,
    needs_review_path,
    record_needs_review,
)


# =============================================================================
# Helpers
# =============================================================================

@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def checker_config(data_dir):
    """Minimal stand-in exposing only what GmailChecker reads."""
    return SimpleNamespace(
        data_dir=data_dir,
        discord=SimpleNamespace(enabled=False),
        user=SimpleNamespace(target_companies=[]),
    )


def _row(row_number: int, company: str, position: str, status: str = "New"):
    """A stand-in for JobRow.

    Carries the fields the review log reads, plus the ones _update_job_status
    touches on the matched path so a successful match can run to completion.
    """
    return SimpleNamespace(
        row_number=row_number,
        company=company,
        position=position,
        status=status,
        application_date="",
        notes="",
    )


def _email(message_id: str = "msg-1", subject: str = "Thank you for applying to DRW"):
    return EmailMessage(
        message_id=message_id,
        subject=subject,
        sender="DRW Recruiting",
        sender_email="no-reply@drwholdings.com",
        date=datetime(2026, 7, 24, 2, 18),
        body_text="Thank you for your interest in DRW.",
        body_html="",
        category="Primary",
    )


def _classification(companies, position=None, category="confirmation"):
    return EmailClassification(
        category=category,
        confidence=0.95,
        company_candidates=companies,
        position=position,
        date_mentioned=None,
    )


def _checker(config, sheets):
    """Build a GmailChecker wired to a mock sheet, with Gmail/AI stubbed out."""
    with patch("check_gmail.get_gmail_clients", return_value=[MagicMock()]), \
         patch("check_gmail.get_classifier"), \
         patch("check_gmail.get_sheets_client", return_value=sheets):
        from check_gmail import GmailChecker

        return GmailChecker(config)


# =============================================================================
# record_needs_review / load_needs_review
# =============================================================================

class TestRecordNeedsReview:

    def test_writes_entry_with_all_fields(self, data_dir):
        written = record_needs_review(
            data_dir,
            message_id="msg-1",
            reason=REASON_AMBIGUOUS,
            account="sz684@cornell.edu",
            sender="no-reply@drwholdings.com",
            subject="Thank you for applying to DRW",
            category="confirmation",
            company="DRW",
            candidates=[{"row": 315, "position": "Trade Support Intern", "status": "New"}],
        )

        assert written is True
        entries = load_needs_review(data_dir)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["message_id"] == "msg-1"
        assert entry["reason"] == REASON_AMBIGUOUS
        assert entry["account"] == "sz684@cornell.edu"
        assert entry["company"] == "DRW"
        assert entry["candidates"][0]["row"] == 315
        assert entry["timestamp"]  # stamped, format checked by _parse_dt tests

    def test_second_call_same_message_id_is_a_noop(self, data_dir):
        """--reprocess re-walks old mail; entries must not multiply."""
        record_needs_review(data_dir, message_id="msg-1", reason=REASON_AMBIGUOUS)
        written = record_needs_review(data_dir, message_id="msg-1", reason=REASON_UNTRACKED)

        assert written is False
        entries = load_needs_review(data_dir)
        assert len(entries) == 1
        assert entries[0]["reason"] == REASON_AMBIGUOUS  # original kept

    def test_distinct_messages_accumulate(self, data_dir):
        record_needs_review(data_dir, message_id="msg-1", reason=REASON_AMBIGUOUS)
        record_needs_review(data_dir, message_id="msg-2", reason=REASON_UNTRACKED)

        assert [e["message_id"] for e in load_needs_review(data_dir)] == ["msg-1", "msg-2"]

    def test_creates_data_dir_when_missing(self, tmp_path):
        target = tmp_path / "not-yet"
        record_needs_review(target, message_id="msg-1", reason=REASON_UNTRACKED)

        assert needs_review_path(target).exists()

    def test_candidates_default_to_empty_list(self, data_dir):
        record_needs_review(data_dir, message_id="msg-1", reason=REASON_UNTRACKED)

        assert load_needs_review(data_dir)[0]["candidates"] == []


class TestLoadNeedsReview:

    def test_missing_file_is_empty(self, data_dir):
        assert load_needs_review(data_dir) == []

    def test_corrupt_file_is_empty_not_fatal(self, data_dir):
        needs_review_path(data_dir).write_text("{not json", encoding="utf-8")

        assert load_needs_review(data_dir) == []

    def test_non_list_json_is_empty(self, data_dir):
        needs_review_path(data_dir).write_text('{"a": 1}', encoding="utf-8")

        assert load_needs_review(data_dir) == []

    def test_corrupt_file_is_replaced_not_appended_to(self, data_dir):
        needs_review_path(data_dir).write_text("garbage", encoding="utf-8")
        record_needs_review(data_dir, message_id="msg-1", reason=REASON_UNTRACKED)

        entries = json.loads(needs_review_path(data_dir).read_text(encoding="utf-8"))
        assert len(entries) == 1


# =============================================================================
# _find_matching_job -> MatchOutcome
# =============================================================================

class TestMatchOutcome:

    def test_single_match_returns_the_row(self, checker_config):
        sheets = MagicMock()
        sheets.find_jobs_by_company_and_position.return_value = [_row(315, "DRW", "Trade Support Intern")]
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(_classification(["DRW"], "Trade Support Intern"))

        assert outcome.matched is True
        assert outcome.job.row_number == 315
        assert outcome.reason == ""

    def test_tied_rows_report_ambiguous_with_candidates(self, checker_config):
        """The DRW case: no position in the email, five DRW rows in the Sheet."""
        drw_rows = [
            _row(315, "DRW", "Trade Support Intern"),
            _row(291, "DRW", "FPGA Intern"),
            _row(290, "DRW", "Quantitative Trading Analyst Intern"),
        ]
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = drw_rows
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(_classification(["DRW"]))

        assert outcome.matched is False
        assert outcome.reason == REASON_AMBIGUOUS
        assert outcome.company == "DRW"
        assert [r.row_number for r in outcome.candidates] == [315, 291, 290]

    def test_company_plus_position_tie_is_ambiguous(self, checker_config):
        """The Akuna case: the email names a position two duplicate rows share."""
        sheets = MagicMock()
        sheets.find_jobs_by_company_and_position.return_value = [
            _row(317, "Akuna Capital", "Python Software Engineer Intern"),
            _row(323, "Akuna Capital", "Python Software Engineer Intern"),
        ]
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(
            _classification(["Akuna Capital"], "Python Software Engineer Intern")
        )

        assert outcome.reason == REASON_AMBIGUOUS
        assert [r.row_number for r in outcome.candidates] == [317, 323]

    def test_no_rows_anywhere_is_untracked(self, checker_config):
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = []
        sheets.find_jobs_by_company_and_position.return_value = []
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(_classification(["Belvedere Trading"]))

        assert outcome.reason == REASON_UNTRACKED
        assert outcome.company == "Belvedere Trading"
        assert outcome.candidates == []

    def test_empty_candidate_list_is_no_company(self, checker_config):
        checker = _checker(checker_config, MagicMock())

        outcome = checker._find_matching_job(_classification([]))

        assert outcome.reason == REASON_NO_COMPANY

    def test_later_candidate_resolving_wins_over_an_earlier_tie(self, checker_config):
        """A tie on candidate 1 must not stop candidate 2 from matching cleanly."""
        sheets = MagicMock()
        sheets.find_jobs_by_company.side_effect = lambda name: (
            [_row(1, "DRW", "A"), _row(2, "DRW", "B")] if name == "DRW"
            else [_row(9, "DRW Holdings", "C")]
        )
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(_classification(["DRW", "DRW Holdings"]))

        assert outcome.matched is True
        assert outcome.job.row_number == 9

    def test_first_tie_is_the_one_reported(self, checker_config):
        sheets = MagicMock()
        sheets.find_jobs_by_company.side_effect = lambda name: (
            [_row(1, "DRW", "A"), _row(2, "DRW", "B")] if name == "DRW"
            else [_row(8, "DRW Holdings", "C"), _row(9, "DRW Holdings", "D")]
        )
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(_classification(["DRW", "DRW Holdings"]))

        assert outcome.company == "DRW"
        assert [r.row_number for r in outcome.candidates] == [1, 2]


# =============================================================================
# _process_email no-match path
# =============================================================================

class TestProcessEmailFlagging:

    def _run(self, checker_config, sheets, classification, email=None):
        checker = _checker(checker_config, sheets)
        checker.classifier = MagicMock()
        checker.classifier.classify.return_value = classification
        client = MagicMock(label="sz684@cornell.edu")

        with patch("check_gmail.apply_privacy_filters", return_value=(True, "ok")):
            updated = checker._process_email(client, email or _email())

        return checker, client, updated

    def test_ambiguous_email_is_flagged_and_still_marked_processed(self, checker_config, data_dir):
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = [
            _row(315, "DRW", "Trade Support Intern"),
            _row(291, "DRW", "FPGA Intern"),
        ]
        checker, client, updated = self._run(checker_config, sheets, _classification(["DRW"]))

        assert updated is False
        # Flagged once, not retried: the email stays in the processed cache.
        client.mark_as_processed.assert_called_once_with("msg-1")

        entries = load_needs_review(data_dir)
        assert len(entries) == 1
        assert entries[0]["reason"] == REASON_AMBIGUOUS
        assert entries[0]["account"] == "sz684@cornell.edu"
        assert entries[0]["sender"] == "no-reply@drwholdings.com"
        assert entries[0]["category"] == "confirmation"
        assert entries[0]["candidates"] == [
            {"row": 315, "position": "Trade Support Intern", "status": "New"},
            {"row": 291, "position": "FPGA Intern", "status": "New"},
        ]

    def test_stats_split_ambiguous_out_of_no_match(self, checker_config):
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = [
            _row(315, "DRW", "A"), _row(291, "DRW", "B"),
        ]
        checker, _, _ = self._run(checker_config, sheets, _classification(["DRW"]))

        assert checker.stats["no_match"] == 1
        assert checker.stats["ambiguous"] == 1

    def test_untracked_counts_as_no_match_but_not_ambiguous(self, checker_config, data_dir):
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = []
        sheets.find_jobs_by_company_and_position.return_value = []
        checker, _, _ = self._run(
            checker_config, sheets, _classification(["Belvedere Trading"])
        )

        assert checker.stats["no_match"] == 1
        assert checker.stats["ambiguous"] == 0
        assert load_needs_review(data_dir)[0]["reason"] == REASON_UNTRACKED

    def test_matched_email_writes_nothing(self, checker_config, data_dir):
        sheets = MagicMock()
        sheets.find_jobs_by_company_and_position.return_value = [
            _row(315, "DRW", "Trade Support Intern")
        ]
        self._run(
            checker_config, sheets, _classification(["DRW"], "Trade Support Intern")
        )

        assert load_needs_review(data_dir) == []

    def test_unknown_category_is_not_flagged(self, checker_config, data_dir):
        """Non-status mail is noise, not something to review."""
        sheets = MagicMock()
        checker, client, _ = self._run(
            checker_config, sheets, _classification(["DRW"], category="unknown")
        )

        assert load_needs_review(data_dir) == []
        assert checker.stats["unknown_category"] == 1

    def test_unwritable_log_does_not_break_the_run(self, checker_config, data_dir):
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = [
            _row(315, "DRW", "A"), _row(291, "DRW", "B"),
        ]
        with patch("check_gmail.record_needs_review", side_effect=OSError("disk full")):
            checker, client, updated = self._run(
                checker_config, sheets, _classification(["DRW"])
            )

        assert updated is False
        client.mark_as_processed.assert_called_once_with("msg-1")

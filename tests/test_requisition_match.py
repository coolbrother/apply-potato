"""
Tests for matching an email to a row by requisition id.

A Workday receipt is signed by whichever legal entity runs the ATS, not by the business
that owns the posting: several subsidiaries of one parent all send confirmations
signed by the parent. Company lookup then reaches none of the subsidiary's rows and
several of the parent's, and the email is dropped as ambiguous.
The same email prints the requisition number beside the job title, and every row already
carries that number inside the HYPERLINK URL behind its Position cell.

Usage:
    pytest tests/test_requisition_match.py -v
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.email_classifier import EmailClassification
from src.gmail import EmailMessage, body_as_text, message_text
from src.sheets import requisition_ids, url_contains_requisition


# Shaped like a Workday receipt: the requisition number sits unlabelled inside the
# parentheses before the title, and only the parent company is named anywhere.
WORKDAY_BODY = (
    "Hi Applicant ,\n"
    "Thank you for your interest in our open position ( 01999999\n"
    "Methods Intern - Hot Section Engineering (Summer 2027) (Onsite) ). We look "
    "forward to reviewing your skills, qualifications and experience.\n"
    "Regards,\nParentCo Global Talent Acquisition\n"
    "ParentCo Corporation - 1 Example Ave."
)

SUBSIDIARY_URL = (
    "https://example.wd5.myworkdayjobs.com/en-US/Private_Posting/job/"
    "US-CT-EXAMPLE-SITE/"
    "Methods-Intern---Hot-Section-Engineering--Summer-2027---Onsite-_01999999"
)


def _row(row_number, company, position, url=None, status="New"):
    return SimpleNamespace(
        row_number=row_number,
        company=company,
        position=position,
        position_url=url,
        status=status,
        application_date="",
        added_date="",
        notes="",
        last_email_time="",
    )


def _email(subject="Application Received", body=WORKDAY_BODY, html=""):
    return EmailMessage(
        message_id="msg-1",
        subject=subject,
        sender="Example Workday Notifications",
        sender_email="notifications@example-ats.com",
        date=datetime(2026, 8, 13, 22, 36),
        body_text=body,
        body_html=html,
        category="Updates",
    )


def _classification(companies, position=None, category="confirmation"):
    return EmailClassification(
        category=category,
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


@pytest.fixture
def checker_config(tmp_path):
    """Minimal stand-in exposing only what GmailChecker reads."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return SimpleNamespace(
        data_dir=data_dir,
        discord=SimpleNamespace(enabled=False),
        user=SimpleNamespace(target_companies=[]),
    )


# =============================================================================
# Pulling ids out of text
# =============================================================================

class TestRequisitionIds:

    def test_finds_the_id_beside_the_job_title(self):
        assert "01999999" in requisition_ids(WORKDAY_BODY)

    def test_leading_zeros_are_kept(self):
        """Some ATSs pad ids with zeros; trimming them would stop matching the URL."""
        assert requisition_ids("req 0000183827") == {"0000183827"}

    def test_short_numbers_are_not_ids(self):
        """Years, street numbers, ZIPs and dollar figures are not identifiers."""
        assert requisition_ids("2027 intern, 1000 Wilson Blvd, VA 22209, $85,000") == set()

    def test_no_digits_at_all(self):
        assert requisition_ids("Thank you for applying to BankCo") == set()

    def test_none_and_empty(self):
        assert requisition_ids(None) == set()
        assert requisition_ids("") == set()


class TestUrlContainsRequisition:

    def test_workday_slug_tail(self):
        assert url_contains_requisition(SUBSIDIARY_URL, "01999999")

    def test_a_shorter_id_cannot_claim_a_longer_one(self):
        assert not url_contains_requisition(SUBSIDIARY_URL, "1999999")
        assert not url_contains_requisition(SUBSIDIARY_URL, "0199999")

    def test_a_longer_id_is_not_matched_by_its_prefix(self):
        assert not url_contains_requisition("https://x.com/jobs/019999991", "01999999")

    def test_non_digit_neighbours_are_boundaries(self):
        assert url_contains_requisition("https://x.com/jobs/362479", "362479")
        assert url_contains_requisition("https://x.com/JR260339/apply", "260339")

    def test_missing_values(self):
        assert not url_contains_requisition("", "01999999")
        assert not url_contains_requisition(SUBSIDIARY_URL, "")
        assert not url_contains_requisition(None, "01999999")


# =============================================================================
# The matcher
# =============================================================================

class TestMatchByRequisitionId:

    def test_the_parent_company_case(self, checker_config):
        """The parent signs the mail; the id still finds the subsidiary's row."""
        sheets = MagicMock()
        sheets.find_jobs_by_requisition_id.return_value = [
            _row(53, "SubsidiaryCo", "Methods Intern", SUBSIDIARY_URL)
        ]
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(
            _classification(["ParentCo Corporation", "ParentCo"]), _email()
        )

        assert outcome.matched is True
        assert outcome.job.row_number == 53
        # The company path is never consulted once the id resolves.
        sheets.find_jobs_by_company.assert_not_called()

    def test_id_wins_over_a_company_that_would_also_match(self, checker_config):
        sheets = MagicMock()
        sheets.find_jobs_by_requisition_id.return_value = [
            _row(53, "SubsidiaryCo", "Methods Intern", SUBSIDIARY_URL)
        ]
        sheets.find_jobs_by_company.return_value = [_row(1, "ParentCo", "Something else")]
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(_classification(["ParentCo"]), _email())

        assert outcome.job.row_number == 53

    def test_two_rows_share_the_id_so_it_falls_through(self, checker_config):
        sheets = MagicMock()
        sheets.find_jobs_by_requisition_id.return_value = [
            _row(62, "TradingCo", "Software Engineer Intern", "https://x.com/4823924101"),
            _row(63, "TradingCo", "Hardware Engineer Intern", "https://x.com/4823924101"),
        ]
        sheets.find_jobs_by_company.return_value = [_row(9, "TradingCo", "Trader")]
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(_classification(["TradingCo"]), _email())

        assert outcome.job.row_number == 9  # resolved by company, not by id

    def test_a_struck_row_still_proves_the_id_is_not_unique(self, checker_config):
        """
        The job-alert case. A digest listing several of one firm's postings quotes ids
        spanning six rows, five struck. Filtering the struck ones first would leave
        exactly one row and read a marketing email as a receipt for it.
        """
        sheets = MagicMock()
        sheets.find_jobs_by_requisition_id.return_value = [
            _row(62, "TradingCo", "Software Engineer Intern", "https://x.com/4823924101"),
            _row(63, "TradingCo", "Hardware Engineer Intern", "https://x.com/4823924101"),
        ]
        sheets.find_jobs_by_company.return_value = []
        checker = _checker(checker_config, sheets, struck={63})

        outcome = checker._find_matching_job(_classification(["TradingCo"]), _email())

        assert outcome.matched is False

    def test_a_uniquely_matched_row_that_is_struck_does_not_match(self, checker_config):
        sheets = MagicMock()
        sheets.find_jobs_by_requisition_id.return_value = [
            _row(53, "SubsidiaryCo", "Methods Intern", SUBSIDIARY_URL)
        ]
        sheets.find_jobs_by_company.return_value = []
        checker = _checker(checker_config, sheets, struck={53})

        outcome = checker._find_matching_job(_classification(["ParentCo"]), _email())

        assert outcome.matched is False

    def test_an_id_matches_even_with_no_company_candidates(self, checker_config):
        """No company name in the mail is fatal to the old path and irrelevant here."""
        sheets = MagicMock()
        sheets.find_jobs_by_requisition_id.return_value = [
            _row(53, "SubsidiaryCo", "Methods Intern", SUBSIDIARY_URL)
        ]
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(_classification([]), _email())

        assert outcome.job.row_number == 53

    def test_no_email_means_no_id_lookup(self, checker_config):
        """Callers without the message keep the old behaviour exactly."""
        sheets = MagicMock()
        sheets.find_jobs_by_company.return_value = [_row(9, "ParentCo", "Intern")]
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(_classification(["ParentCo"]), None)

        assert outcome.job.row_number == 9
        sheets.find_jobs_by_requisition_id.assert_not_called()

    def test_no_ids_in_the_email_falls_through(self, checker_config):
        sheets = MagicMock()
        sheets.find_jobs_by_requisition_id.return_value = []
        sheets.find_jobs_by_company.return_value = [_row(49, "BankCo", "Analyst")]
        checker = _checker(checker_config, sheets)

        outcome = checker._find_matching_job(
            _classification(["BankCo"]),
            _email(subject="Application Update",
                   body="Thank you for applying to the 2027 Technology Summer "
                        "Analyst Program (Somewhere) at BankCo."),
        )

        assert outcome.job.row_number == 49


# =============================================================================
# The text the scan runs over
# =============================================================================

class TestMessageText:

    def test_html_is_converted_when_there_is_no_text_part(self):
        """Some ATSs send HTML only, so reading body_text alone sees nothing."""
        email = _email(body="", html="<html><body><p>position ( 01999999 )</p></body></html>")
        assert "01999999" in body_as_text(email)
        assert "01999999" in requisition_ids(message_text(email))

    def test_script_and_style_are_dropped(self):
        email = _email(body="", html="<style>a{b:1}</style><p>hello</p><script>x()</script>")
        text = body_as_text(email)
        assert "hello" in text
        assert "b:1" not in text

    def test_the_text_part_wins_when_present(self):
        email = _email(body="plain wins", html="<p>html loses</p>")
        assert body_as_text(email).strip() == "plain wins"

    def test_subject_is_included(self):
        """Some senders put the requisition id in the subject and nowhere else."""
        email = _email(subject="Your application for 300697", body="See the portal.")
        assert "300697" in requisition_ids(message_text(email))

    def test_empty_email(self):
        assert body_as_text(_email(subject="", body="", html="")) == ""


# =============================================================================
# A struck exact-position match ends the search
#
# A receipt naming one role was written onto a different role's row: the row naming
# that role was struck, so the matcher widened to the employer's other roles and let
# the tie-break choose. Striking is deliberate, so it is an answer, not a gap.
# =============================================================================

class TestStruckPositionMatch:

    def _sheets(self, named, by_company):
        sheets = MagicMock()
        sheets.find_jobs_by_requisition_id.return_value = []
        sheets.find_jobs_by_company_and_position.return_value = named
        sheets.find_jobs_by_company.return_value = by_company
        return sheets

    def test_does_not_guess_between_other_roles(self, checker_config):
        """
        The case that misrouted a receipt: the named row was struck, the search widened
        to the employer's other roles, and the tie-break picked one of them. Widening is
        allowed — it is how a canonical row is reached — but the guess is not.
        """
        named = [_row(4, "TradingCo", "Software Engineer Intern (Commodities)")]
        siblings = [_row(1, "TradingCo", "Quantitative Risk Intern"),
                    _row(2, "TradingCo", "Trading Intern (Commodities)")]
        checker = _checker(checker_config, self._sheets(named, siblings), struck={4})

        outcome = checker._find_matching_job(
            _classification(["TradingCo"], position="Software Engineer Intern (Commodities)"),
            _email(),
        )

        assert outcome.matched is False
        # The AI is never asked to choose between roles the email did not name.
        checker.classifier.choose_job_row.assert_not_called()

    def test_a_live_named_row_still_wins(self, checker_config):
        named = [_row(4, "TradingCo", "Software Engineer Intern (Commodities)")]
        checker = _checker(checker_config, self._sheets(named, []), struck=set())

        outcome = checker._find_matching_job(
            _classification(["TradingCo"], position="Software Engineer Intern (Commodities)"),
            _email(),
        )

        assert outcome.job.row_number == 4

    def test_one_struck_one_live_still_resolves(self, checker_config):
        """Striking the duplicate is exactly how a canonical row is singled out."""
        named = [_row(4, "TradingCo", "Software Engineer Intern (Commodities)"),
                 _row(5, "TradingCo", "Software Engineer Intern (Commodities)")]
        checker = _checker(checker_config, self._sheets(named, []), struck={5})

        outcome = checker._find_matching_job(
            _classification(["TradingCo"], position="Software Engineer Intern (Commodities)"),
            _email(),
        )

        assert outcome.job.row_number == 4

    def test_no_named_row_at_all_still_falls_back_to_company(self, checker_config):
        """Nothing carries that position, so the company fallback is untouched."""
        checker = _checker(
            checker_config,
            self._sheets([], [_row(9, "TradingCo", "Some Other Intern")]),
            struck=set(),
        )

        outcome = checker._find_matching_job(
            _classification(["TradingCo"], position="A Role Nobody Tracks"), _email()
        )

        assert outcome.job.row_number == 9

    def test_a_single_live_row_is_still_taken(self, checker_config):
        """
        The mark-canonical workflow: the duplicate carrying the email's title is struck
        and the surviving row has a different title. One live row is not a guess.
        """
        named = [_row(4, "TradingCo", "Software Engineer Intern")]
        checker = _checker(
            checker_config,
            self._sheets(named, [_row(4, "TradingCo", "Software Engineer Intern"),
                                 _row(9, "TradingCo", "Data Intern")]),
            struck={4},
        )

        outcome = checker._find_matching_job(
            _classification(["TradingCo"], position="Software Engineer Intern"), _email()
        )

        assert outcome.job.row_number == 9

    def test_a_later_candidate_still_gets_its_turn(self, checker_config):
        """Only the same-company widening is given up, not the remaining candidates."""
        sheets = MagicMock()
        sheets.find_jobs_by_requisition_id.return_value = []
        sheets.find_jobs_by_company_and_position.side_effect = lambda company, position: (
            [_row(4, "TradingCo", "Software Engineer Intern")] if company == "TradingCo"
            else [_row(7, "CommoditiesCo", "Software Engineer Intern")]
        )
        sheets.find_jobs_by_company.return_value = []
        checker = _checker(checker_config, sheets, struck={4})

        outcome = checker._find_matching_job(
            _classification(["TradingCo", "CommoditiesCo"], position="Software Engineer Intern"),
            _email(),
        )

        assert outcome.job.row_number == 7


# =============================================================================
# Breaking a tie on evidence of having applied
#
# An assessment platform's mail names the employer and no role at all — "confirms your
# submission to <company>" is the whole body — so position matching has nothing to work
# with and every row of that employer ties. Six rows tied, five never touched and one
# sitting at Applied, and the email was abandoned.
# =============================================================================

class TestTieBrokenByApplicationEvidence:

    def _tied(self, checker_config, rows, struck=None):
        sheets = MagicMock()
        sheets.find_jobs_by_requisition_id.return_value = []
        sheets.find_jobs_by_company_and_position.return_value = []
        sheets.find_jobs_by_company.return_value = rows
        checker = _checker(checker_config, sheets, struck=struck)
        checker.classifier.choose_job_row.return_value = None   # the AI declines
        return checker

    def test_one_applied_row_among_untouched_ones_wins(self, checker_config):
        rows = [
            _row(24, "AssetCo", "Summer Analyst Data Engineer"),
            _row(25, "AssetCo", "Data Engineer Summer Analyst"),
            _row(19, "AssetCo", "Software Engineer Summer Analyst", status="Applied"),
            _row(26, "AssetCo", "Data Science Summer Analyst"),
        ]
        checker = self._tied(checker_config, rows)

        outcome = checker._find_matching_job(_classification(["AssetCo"]), _email())

        assert outcome.job.row_number == 19

    def test_an_application_date_alone_is_evidence(self, checker_config):
        """A row can sit at New with its confirmation recorded only as a date."""
        applied = _row(19, "AssetCo", "Software Engineer Summer Analyst")
        applied.application_date = "08/19/2026 21:00:00"
        rows = [_row(24, "AssetCo", "Data Engineer"), applied]
        checker = self._tied(checker_config, rows)

        outcome = checker._find_matching_job(_classification(["AssetCo"]), _email())

        assert outcome.job.row_number == 19

    def test_two_applied_rows_stay_ambiguous(self, checker_config):
        rows = [
            _row(1, "AssetCo", "A", status="Applied"),
            _row(2, "AssetCo", "B", status="OA"),
            _row(3, "AssetCo", "C"),
        ]
        checker = self._tied(checker_config, rows)

        outcome = checker._find_matching_job(_classification(["AssetCo"]), _email())

        assert outcome.matched is False
        assert [j.row_number for j in outcome.candidates] == [1, 2, 3]

    def test_no_applied_row_stays_ambiguous(self, checker_config):
        rows = [_row(1, "AssetCo", "A"), _row(2, "AssetCo", "B")]
        checker = self._tied(checker_config, rows)

        outcome = checker._find_matching_job(_classification(["AssetCo"]), _email())

        assert outcome.matched is False

    def test_the_ai_still_wins_when_it_answers(self, checker_config):
        """Evidence is the fallback, not a replacement for reading the email."""
        rows = [
            _row(1, "AssetCo", "A", status="Applied"),
            _row(2, "AssetCo", "B"),
        ]
        checker = self._tied(checker_config, rows)
        checker.classifier.choose_job_row.return_value = 2

        outcome = checker._find_matching_job(_classification(["AssetCo"]), _email())

        assert outcome.job.row_number == 2

    def test_not_applied_when_the_named_row_was_struck(self, checker_config):
        """
        Every remaining candidate is then a role the email never named, so picking the
        applied one is the same wrong guess as picking any other.
        """
        sheets = MagicMock()
        sheets.find_jobs_by_requisition_id.return_value = []
        sheets.find_jobs_by_company_and_position.return_value = [
            _row(9, "TradingCo", "Software Engineer Intern")
        ]
        sheets.find_jobs_by_company.return_value = [
            _row(1, "TradingCo", "Trading Intern", status="Applied"),
            _row(2, "TradingCo", "Quant Intern"),
        ]
        checker = _checker(checker_config, sheets, struck={9})
        checker.classifier.choose_job_row.return_value = None

        outcome = checker._find_matching_job(
            _classification(["TradingCo"], position="Software Engineer Intern"), _email()
        )

        assert outcome.matched is False

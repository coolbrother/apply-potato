"""
What _record_stage_done writes to the sheet.

This path has had two bugs, both of the same shape — deciding there was nothing to do by
consulting one column when three are in play:

  * Capital One's receipt wrote nothing, because Completed Stages already said "OA" and
    the early return looked no further. Last Event never got set, so the row stayed on
    the to-do list.
  * Last Email Time was omitted entirely, so a row read as last touched days before the
    receipt that had just arrived.

    pytest tests/test_stage_done_writes.py -v
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from check_gmail import GmailChecker
from src.sheets import JobRow


RECEIPT_AT = datetime(2026, 8, 9, 19, 56, 6)


def _job(row=518, status="OA", completed="", event="", last_email=""):
    """A JobRow with only the fields this path reads."""
    return JobRow(
        row_number=row, company="Millennium", position="Intern", position_url=None,
        status=status, job_posting_date="", application_date="", oa_date="8/12/2026",
        phone_date="", tech_date="", dream="No", fit_score=0, salary="", job_type="",
        work_model="", location="", season_year="Summer 2027", deadline="", source="",
        added_date="", resume_needed="", cover_letter_needed="", notes="",
        last_email_time=last_email, completed_stages=completed, last_event=event,
    )


@pytest.fixture
def checker():
    """
    Built without __init__: the constructor opens Gmail, the classifier and Sheets, none
    of which this path needs. Only the collaborators it actually touches are supplied.
    """
    c = GmailChecker.__new__(GmailChecker)
    c.sheets_client = MagicMock()
    c.reprocess = False
    c._struck_rows = set()
    c.stats = {"no_match": 0, "ambiguous": 0, "updated": 0, "stages_completed": 0}
    return c


def _run(checker, job, checker_client=None):
    checker.sheets_client.find_jobs_by_company.return_value = [job]
    email = SimpleNamespace(
        message_id="m1", sender_email="support@hackerrankforwork.com",
        subject="Thanks for taking", date=RECEIPT_AT,
    )
    classification = SimpleNamespace(
        category="stage_done", stage_completed="OA",
        company_candidates=["Millennium"],
    )
    client = checker_client or MagicMock()
    result = checker._record_stage_done(client, email, classification)
    writes = checker.sheets_client.update_job.call_args
    return result, (writes.args[1] if writes else None)


class TestWrites:
    def test_writes_all_three_columns(self, checker):
        _, written = _run(checker, _job())
        assert written["completed_stages"] == "OA"
        assert written["last_event"] == "Assessment Submitted"
        assert written["last_email_time"] == "08/09/2026 19:56:06"

    def test_writes_even_when_the_stage_is_already_recorded(self, checker):
        """
        Capital One's exact case: W already says "OA", so add_stage returns it unchanged.
        The other two columns still have to be written.
        """
        _, written = _run(checker, _job(completed="OA"))
        assert written is not None
        assert written["completed_stages"] == "OA"
        assert written["last_event"] == "Assessment Submitted"

    def test_last_email_time_moves_forward_on_a_later_receipt(self, checker):
        """Last Email Time is the most recent email to touch the row, whatever it was."""
        _, written = _run(checker, _job(completed="OA", event="Assessment Submitted",
                                        last_email="08/04/2026 21:54:38"))
        assert written["last_email_time"] == "08/09/2026 19:56:06"

    def test_skips_only_when_all_three_would_be_unchanged(self, checker):
        """A genuine duplicate writes nothing, so a reprocess is not a storm of updates."""
        result, written = _run(checker, _job(completed="OA", event="Assessment Submitted",
                                             last_email="08/09/2026 19:56:06"))
        assert result is False
        assert written is None

    def test_never_writes_status(self, checker):
        """A row sits at OA before and after the assessment."""
        _, written = _run(checker, _job())
        assert "status" not in written


class TestMatching:
    def test_refuses_when_two_rows_reached_the_stage(self, checker):
        """
        Millennium runs one assessment across two roles, but SIG issues a separate one
        per position — so marking both would record work that was never done.
        """
        checker._log_needs_review = MagicMock()
        checker.sheets_client.find_jobs_by_company.return_value = [_job(518), _job(519)]
        email = SimpleNamespace(message_id="m1", sender_email="x@y.com",
                                subject="Thanks for taking", date=RECEIPT_AT)
        classification = SimpleNamespace(category="stage_done", stage_completed="OA",
                                         company_candidates=["Millennium"])

        result = checker._record_stage_done(MagicMock(), email, classification)

        assert result is False
        checker.sheets_client.update_job.assert_not_called()
        assert checker.stats["ambiguous"] == 1

    def test_refuses_when_no_row_reached_the_stage(self, checker):
        """Castleton's receipt landed here while it was still classified Technical."""
        checker._log_needs_review = MagicMock()
        job = _job(status="Applied")
        job.oa_date = ""
        checker.sheets_client.find_jobs_by_company.return_value = [job]
        email = SimpleNamespace(message_id="m1", sender_email="x@y.com",
                                subject="Thanks for taking", date=RECEIPT_AT)
        classification = SimpleNamespace(category="stage_done", stage_completed="OA",
                                         company_candidates=["Millennium"])

        result = checker._record_stage_done(MagicMock(), email, classification)

        assert result is False
        checker.sheets_client.update_job.assert_not_called()


class TestRetryWhenNoRowHasReachedTheStage:
    """
    A completion that found no row is usually a race, not an error: the invite that puts
    the row at the stage may land minutes later. Consuming it retires the email for good,
    and the assessment ends up recorded only by hand — which is what happened to a real
    receipt whose invite had not yet been applied when it arrived.
    """

    def _refuse(self, checker, job, client):
        checker._log_needs_review = MagicMock()
        checker.sheets_client.find_jobs_by_company.return_value = job
        email = SimpleNamespace(message_id="m1", sender_email="noreply@platform.example",
                                subject="Thank you for submitting", date=RECEIPT_AT)
        classification = SimpleNamespace(category="stage_done", stage_completed="OA",
                                         company_candidates=["Millennium"])
        return checker._record_stage_done(client, email, classification)

    def test_an_orphaned_completion_is_left_for_the_next_run(self, checker):
        job = _job(status="Applied")
        job.oa_date = ""
        client = MagicMock()

        result = self._refuse(checker, [job], client)

        assert result is False
        client.mark_as_processed.assert_not_called()

    def test_it_is_recorded_once_the_row_reaches_the_stage(self, checker):
        """The retry that the previous test makes possible."""
        client = MagicMock()
        waiting = _job(status="Applied")
        waiting.oa_date = ""
        assert self._refuse(checker, [waiting], client) is False

        # The invite lands, the row reaches OA, and the same email is seen again.
        _, written = _run(checker, _job(status="OA"), checker_client=client)

        assert written["completed_stages"] == "OA"
        assert written["last_event"] == "Assessment Submitted"
        client.mark_as_processed.assert_called_once()

    def test_an_ambiguous_completion_is_still_consumed(self, checker):
        """Two rows at the stage needs a person; more runs will not resolve it."""
        client = MagicMock()

        result = self._refuse(checker, [_job(518), _job(519)], client)

        assert result is False
        client.mark_as_processed.assert_called_once()

    def test_a_successful_mark_still_consumes_the_email(self, checker):
        client = MagicMock()
        _run(checker, _job(), checker_client=client)
        client.mark_as_processed.assert_called_once()

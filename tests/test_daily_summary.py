"""Tests for the season totals and new-rejection list in scripts/daily_summary.py."""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.daily_summary import (
    MAX_REJECTION_DETAIL,
    _new_rejections,
    _rejection_block,
    _season_matches,
    _season_totals,
)
from src.sheets import JobRow


def make_job(row_number=2, status="New", season_year="Summer 2027",
             application_date="", oa_date="", phone_date="", tech_date="",
             company="TestCo", position="SWE Intern",
             last_email_time="", last_event=""):
    """Build a JobRow with only the fields the summary looks at."""
    return JobRow(
        row_number=row_number,
        company=company,
        position=position,
        position_url=None,
        status=status,
        job_posting_date="",
        application_date=application_date,
        oa_date=oa_date,
        phone_date=phone_date,
        tech_date=tech_date,
        dream="No",
        fit_score=0,
        salary="",
        job_type="Internship",
        work_model="",
        location="",
        season_year=season_year,
        deadline="",
        source="",
        added_date="",
        resume_needed="",
        cover_letter_needed="",
        notes="",
        last_email_time=last_email_time,
        last_event=last_event,
    )


# =============================================================================
# Season matching
# =============================================================================

@pytest.mark.parametrize("target,job_season,expected", [
    ("Summer 2027", "Summer 2027", True),
    ("Summer 2027", "Fall 2027", True),        # same year, different season
    ("Summer 2027", "Summer 2026", False),     # wrong year
    ("Summer 2027", "", True),                 # unspecified -> counted
    ("Summer 2027", None, True),
    ("Summer 2027", "Summer", True),           # no year -> can't rule out
    (None, "Summer 2026", True),               # no target -> everything counts
    ("", "Summer 2026", True),
])
def test_season_matches(target, job_season, expected):
    assert _season_matches(target, job_season) is expected


def test_out_of_season_jobs_excluded_from_totals():
    jobs = [
        make_job(status="Applied", season_year="Summer 2027", application_date="7/1/2026"),
        make_job(status="Applied", season_year="Summer 2026", application_date="7/1/2025"),
    ]
    totals = _season_totals(jobs, "Summer 2027")
    assert totals["in_season"] == 1
    assert totals["applied"] == 1


# =============================================================================
# Stage counting
# =============================================================================

def test_applied_counts_from_date_or_status():
    jobs = [
        make_job(status="New", application_date="7/1/2026"),   # date only
        make_job(status="Rejected"),                            # status only
        make_job(status="New"),                                 # neither
    ]
    totals = _season_totals(jobs, "Summer 2027")
    assert totals["applied"] == 2


def test_stages_are_cumulative_not_current_status():
    """A job that got rejected after a phone screen still counts at each stage."""
    jobs = [
        make_job(
            status="Rejected",
            application_date="6/1/2026",
            oa_date="6/10/2026",
            phone_date="6/20/2026",
        )
    ]
    totals = _season_totals(jobs, "Summer 2027")
    assert totals["applied"] == 1
    assert totals["oa"] == 1
    assert totals["phone"] == 1
    assert totals["technical"] == 0
    assert totals["rejected"] == 1


def test_stage_counted_from_status_when_date_missing():
    jobs = [
        make_job(status="OA"),
        make_job(status="Phone"),
        make_job(status="Technical"),
    ]
    totals = _season_totals(jobs, "Summer 2027")
    assert totals["oa"] == 1
    assert totals["phone"] == 1
    assert totals["technical"] == 1
    assert totals["applied"] == 3  # all three imply an application went out


def test_terminal_status_counts():
    jobs = [
        make_job(status="Offer", application_date="6/1/2026"),
        make_job(status="Rejected", application_date="6/1/2026"),
        make_job(status="Rejected", application_date="6/2/2026"),
        make_job(status="Ghosted", application_date="6/3/2026"),
    ]
    totals = _season_totals(jobs, "Summer 2027")
    assert totals["offer"] == 1
    assert totals["rejected"] == 2
    assert totals["ghosted"] == 1


def test_awaiting_excludes_closed_and_unapplied():
    jobs = [
        make_job(status="Applied", application_date="6/1/2026"),   # awaiting
        make_job(status="OA", application_date="6/1/2026"),        # awaiting
        make_job(status="Rejected", application_date="6/1/2026"),  # closed
        make_job(status="Offer", application_date="6/1/2026"),     # closed
        make_job(status="Ghosted", application_date="6/1/2026"),   # closed
        make_job(status="New"),                                    # never applied
    ]
    totals = _season_totals(jobs, "Summer 2027")
    assert totals["awaiting"] == 2
    assert totals["applied"] == 5


def test_empty_sheet():
    totals = _season_totals([], "Summer 2027")
    assert all(v == 0 for v in totals.values())


# =============================================================================
# New rejections
# =============================================================================

# The evening window: 09:00 to 17:00 on the same day.
WINDOW_START = datetime(2026, 9, 19, 9, 0, 0)
WINDOW_END = datetime(2026, 9, 19, 17, 0, 0)


def rejected(row_number, when, **kwargs):
    """A row as the Gmail checker leaves it after applying a rejection email."""
    kwargs.setdefault("last_event", "Rejected")
    return make_job(row_number=row_number, status="Rejected",
                    last_email_time=when, **kwargs)


def rejected_rows(jobs, struck=frozenset()):
    found = _new_rejections(jobs, set(struck), WINDOW_START, WINDOW_END)
    return [job.row_number for job in found]


def test_rejection_inside_window_is_listed():
    jobs = [rejected(10, "09/19/2026 11:30:00")]
    assert rejected_rows(jobs) == [10]


def test_rejection_outside_window_is_not_listed():
    jobs = [
        rejected(10, "09/18/2026 16:00:00"),   # the day before
        rejected(11, "09/19/2026 17:30:00"),   # after the window closed
        rejected(12, ""),                       # typed by hand, never dated
    ]
    assert rejected_rows(jobs) == []


def test_window_opens_early_for_mail_processed_after_the_previous_summary():
    jobs = [
        rejected(10, "09/19/2026 08:45:00"),   # inside the 30-minute margin
        rejected(11, "09/19/2026 08:15:00"),   # before it
    ]
    assert rejected_rows(jobs) == [10]


def test_only_rows_the_checker_rejected_count():
    jobs = [
        # Touched in the window, but the mail was not a rejection.
        rejected(10, "09/19/2026 11:00:00", last_event="Application Received"),
        # A rejection-looking event on a row that is not Rejected.
        make_job(row_number=11, status="Applied", last_event="Rejected",
                 last_email_time="09/19/2026 11:00:00"),
        # The spelling this column used before the rename still resolves.
        rejected(12, "09/19/2026 11:00:00", last_event="rejection"),
    ]
    assert rejected_rows(jobs) == [12]


def test_struck_rows_are_skipped():
    jobs = [rejected(10, "09/19/2026 11:00:00"), rejected(11, "09/19/2026 12:00:00")]
    assert rejected_rows(jobs, struck={10}) == [11]


def test_rejections_are_listed_oldest_first():
    jobs = [rejected(10, "09/19/2026 15:00:00"), rejected(11, "09/19/2026 10:00:00")]
    assert rejected_rows(jobs) == [11, 10]


def test_rejection_line_names_row_company_and_position():
    jobs = [rejected(1873, "09/19/2026 11:00:00", company="Acme", position="SWE Intern")]
    block = _rejection_block(jobs, budget=1000)
    assert block == "\n     • row 1873 · Acme — SWE Intern"


def test_rejection_line_without_a_position():
    jobs = [rejected(1873, "09/19/2026 11:00:00", company="Acme", position="")]
    assert _rejection_block(jobs, budget=1000) == "\n     • row 1873 · Acme"


def test_no_rejections_renders_nothing():
    assert _rejection_block([], budget=1000) == ""


def test_rejection_list_is_capped():
    jobs = [rejected(n, "09/19/2026 11:00:00") for n in range(MAX_REJECTION_DETAIL + 3)]
    lines = _rejection_block(jobs, budget=5000).strip("\n").split("\n")
    assert len(lines) == MAX_REJECTION_DETAIL + 1
    assert lines[-1].strip() == "• +3 more"


def test_rejection_list_shrinks_to_fit_the_budget():
    jobs = [rejected(n, "09/19/2026 11:00:00") for n in range(6)]
    full = _rejection_block(jobs, budget=5000)
    tight = _rejection_block(jobs, budget=len(full) - 1)
    assert len(tight) < len(full)
    assert tight.endswith("• +2 more")    # fell back to four rows
    assert _rejection_block(jobs, budget=20) == "\n     • +6 more"

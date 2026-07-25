"""Tests for the cumulative season totals in scripts/daily_summary.py."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.daily_summary import _season_matches, _season_totals
from src.sheets import JobRow


def make_job(row_number=2, status="New", season_year="Summer 2027",
             application_date="", oa_date="", phone_date="", tech_date=""):
    """Build a JobRow with only the fields the season totals look at."""
    return JobRow(
        row_number=row_number,
        company="TestCo",
        position="SWE Intern",
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

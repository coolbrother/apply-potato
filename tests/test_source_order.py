"""
Source ordering in the scraping pipeline.

run() concatenates every source's listings into one list and processes it in
order, so the position of a source decides how long its jobs wait. The Job List
sheet holds URLs pasted in by hand; the repo sources are a bulk feed. Hand-picked
work goes first.

The config here is a stub rather than the `test_config` fixture: _build_sources
reads four plain attributes and never touches Sheets, so depending on the fixture
would skip these cases whenever TEST_GOOGLE_SHEET_ID is unset.
"""

from types import SimpleNamespace

import pytest

from scrape_jobs import JobScraper


def _config(job_list_sheet_id="", newsletters=()):
    return SimpleNamespace(
        github_repos=[SimpleNamespace(owner_repo="owner/repo", branch="dev")],
        job_list_sheet_id=job_list_sheet_id,
        newsletter_enabled=bool(newsletters),
        newsletter_sources=[SimpleNamespace(name=n) for n in newsletters],
    )


def _scraper(config, job_list_parser=None):
    """
    A JobScraper with only the attributes _build_sources touches.

    __init__ builds a network-heavy world — GitHub parser, AI extractor, Sheets
    client, ensure_headers — none of which _build_sources reads.
    """
    scraper = JobScraper.__new__(JobScraper)
    scraper.config = config
    scraper.github_parser = object()
    scraper.job_list_parser = job_list_parser
    return scraper


def _names(sources):
    return [name for name, _fn, _detail in sources]


def test_job_list_comes_before_github():
    """The hand-pasted queue is processed ahead of the bulk repo feed."""
    scraper = _scraper(_config(job_list_sheet_id="sheet123"), job_list_parser=object())

    assert _names(scraper._build_sources()) == ["sheets", "github"]


def test_github_still_present_without_a_job_list():
    """Job List is optional; without it the repo feed is the only source."""
    scraper = _scraper(_config(), job_list_parser=None)

    assert _names(scraper._build_sources()) == ["github"]


def test_newsletter_stays_last():
    """Newsletters are a bulk feed too, and keep their place behind github."""
    scraper = _scraper(
        _config(job_list_sheet_id="sheet123", newsletters=("weekly",)),
        job_list_parser=object(),
    )

    assert _names(scraper._build_sources()) == ["sheets", "github", "newsletter"]


def test_only_filter_preserves_order():
    """
    Restricting to a subset keeps the built order.

    The filter rebuilds the list by comprehension over `candidates`, so it inherits
    that order rather than the order of the `only` argument — passing
    only=["github", "sheets"] must not put github first.
    """
    scraper = _scraper(_config(job_list_sheet_id="sheet123"), job_list_parser=object())

    assert _names(scraper._build_sources(only=["github", "sheets"])) == ["sheets", "github"]


def test_only_can_select_the_job_list_alone():
    """`--only sheets` is how a pending Job List is cleared without a full run."""
    scraper = _scraper(_config(job_list_sheet_id="sheet123"), job_list_parser=object())

    assert _names(scraper._build_sources(only=["sheets"])) == ["sheets"]


def test_unknown_source_name_selects_nothing():
    """A typo in --only yields no sources rather than silently running everything."""
    scraper = _scraper(_config(job_list_sheet_id="sheet123"), job_list_parser=object())

    assert scraper._build_sources(only=["githbu"]) == []


def test_detail_strings_describe_their_source():
    """The detail column is what the run log prints beside each source name."""
    scraper = _scraper(_config(job_list_sheet_id="sheet123"), job_list_parser=object())
    details = {name: detail for name, _fn, detail in scraper._build_sources()}

    assert "sheet123" in details["sheets"]
    assert "owner/repo@dev" in details["github"]

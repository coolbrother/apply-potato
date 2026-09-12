"""
What the Job List sheet is told after a URL is processed.

Three Job List rows read "Filtered out" with no note although the eligibility
filter never rejected them. They had passed extraction and were then skipped in the
per-position loop — two as the same posting already on the sheet, one for a company
with an application in flight. Those checks `continue` so a multi-position page can
still add its other positions, and when every position was skipped the function fell
through to `return False`, which the writer labelled "Filtered out". "Already
Processed" was reserved for `None`, and every `None` path runs before extraction and
keys on the URL — these URLs were new, so they missed.

Now a page whose positions were all skipped after extraction returns
"Skipped: <why>", which the writer records as "Already Processed" with the reason in
Notes, and which does not count toward --limit.

The scraper is built with __new__ and hand-set attributes, as in test_source_order:
__init__ opens GitHub, Sheets and an AI client, none of which these paths need.
"""

from types import SimpleNamespace

import pytest

import scrape_jobs
from scrape_jobs import JobScraper
from src.ai_extractor import ExtractedJob
from src.github_parser import JobListing


URL = "https://example.com/jobs/1"
PAGE = "Software Engineer Intern. Summer 2027. Apply now. " * 60  # long enough not to look closed


class _Parser:
    """Records mark_row calls in place of SheetsJobListParser."""

    def __init__(self):
        self.calls = []

    def mark_row(self, url, status, notes=None):
        self.calls.append((url, status, notes))


class _Dedup:
    def __init__(self, identity_dup=False, live_at=None):
        self.identity_dup = identity_dup
        self.live_at = live_at
        self.seen = []
        self.cached = []

    def is_seen_source(self, url):
        return False

    def job_exists(self, url):
        return False

    def is_filtered(self, url):
        return False

    def company_has_live_application(self, company):
        return self.live_at

    def job_exists_by_identity(self, company, position, location, term):
        return self.identity_dup

    def mark_source_seen(self, url):
        self.seen.append(url)

    def mark_as_filtered(self, url):
        pass

    def add_to_cache(self, url):
        self.cached.append(url)

    def add_identity_to_cache(self, *key):
        pass


class _Scraper:
    async def fetch_page(self, url, render_delay=0):
        return PAGE, url, False


class _Sheets:
    def __init__(self, fail=False):
        self.fail = fail
        self.added = []

    def add_job(self, job_data):
        if self.fail:
            raise RuntimeError("quota exceeded")
        self.added.append(job_data)
        return 500 + len(self.added)

    def update_job(self, row, fields):
        pass


def _listing(source="sheets-list"):
    return JobListing(
        company="", title="", location="", url=URL,
        date_posted="", source_repo=source, age_days=0,
    )


def _job(title="Software Engineer Intern", company="Acme"):
    return ExtractedJob(
        company=company, title=title, job_type="Internship",
        locations=["Chicago, IL"], season_year="Summer 2027",
    )


def _scraper(dedup, sheets=None, jobs=None, parser=None):
    scraper = JobScraper.__new__(JobScraper)
    scraper.config = SimpleNamespace(
        max_retries=1,
        render_delay_seconds=0,
        skip_applied_companies=True,
        user=SimpleNamespace(target_companies=[]),
        discord=SimpleNamespace(enabled=False),
        auto_apply=SimpleNamespace(detect_requirements=False),
    )
    scraper.eligibility_mode = "code"
    scraper.dedup_checker = dedup
    scraper.sheets_client = sheets or _Sheets()
    scraper.ai_extractor = SimpleNamespace(extract=lambda content, source_url: list(jobs or [_job()]))
    scraper.job_list_parser = parser
    scraper.stats = {
        "duplicates_skipped": 0, "duplicate_postings": 0, "applied_company_skipped": 0,
        "filtered_skipped": 0, "scrape_failures": 0, "extraction_failures": 0,
        "filtered_out": 0, "jobs_added": 0, "eligibility_unavailable": 0,
    }
    scraper._log_filtered = lambda *a, **k: None  # never touch data/filter_log.json
    return scraper


@pytest.fixture(autouse=True)
def _quiet_pipeline(monkeypatch):
    """The steps after add_job that reach the network or the Resume repo."""
    monkeypatch.setattr(scrape_jobs, "passes_hard_filters", lambda user, job, judgment=None, mode=None: (True, "ok", ""))
    monkeypatch.setattr(scrape_jobs, "calculate_fit_score", lambda user, job: (50, []))
    monkeypatch.setattr(scrape_jobs, "is_dream_company", lambda *a, **k: False)
    monkeypatch.setattr("src.job_desc.save_job_description", lambda **k: None)


# --- _process_listing: what a fully-skipped page returns ---------------------------


async def test_identity_duplicate_returns_skipped_not_false():
    """Same company/position/location/term on the sheet already: a skip, not a rejection."""
    dedup = _Dedup(identity_dup=True)
    result = await _scraper(dedup)._process_listing(_listing(), _Scraper())

    assert result == (
        "Skipped: same posting already on the sheet — "
        "Acme - Software Engineer Intern (Chicago, IL, Summer 2027)"
    )
    assert dedup.seen == [URL]


async def test_live_application_returns_skipped_not_false():
    dedup = _Dedup(live_at="Acme Corp")
    result = await _scraper(dedup)._process_listing(_listing(), _Scraper())

    assert result == "Skipped: application already in flight at Acme Corp — Acme - Software Engineer Intern"


async def test_one_skipped_one_added_is_done():
    """A multi-position page still counts as Done when any position lands."""
    calls = []

    class _Half(_Dedup):
        def job_exists_by_identity(self, company, position, *rest):
            calls.append(position)
            return position == "Old Role"

    result = await _scraper(
        _Half(), jobs=[_job("Old Role"), _job("New Role")]
    )._process_listing(_listing(), _Scraper())

    assert result is True
    assert calls == ["Old Role", "New Role"]


async def test_filter_rejection_outranks_a_skip(monkeypatch):
    """One position rejected, one already on the sheet: the rejection is the news."""
    monkeypatch.setattr(
        scrape_jobs, "passes_hard_filters",
        lambda user, job, judgment=None, mode=None: (
            (False, "Requires PhD", "class_standing") if job.title == "Research Role" else (True, "ok", "")
        ),
    )
    result = await _scraper(
        _Dedup(identity_dup=True), jobs=[_job("Research Role"), _job("SWE Role")]
    )._process_listing(_listing(), _Scraper())

    assert result == "Filtered: Requires PhD"


async def test_sheet_write_failure_is_a_failure_not_filtered():
    """Nothing skipped, nothing filtered, nothing written: that is a failure."""
    result = await _scraper(_Dedup(), sheets=_Sheets(fail=True))._process_listing(_listing(), _Scraper())

    assert result == "Failed (could not add to Sheets: quota exceeded)"


# --- _record_job_list_result: the word the sheet gets ------------------------------


@pytest.mark.parametrize("result, expected", [
    (None, ("Already Processed", None)),
    (True, ("Done", None)),
    ("Skipped: same posting already on the sheet — Acme - SWE", ("Already Processed", "same posting already on the sheet — Acme - SWE")),
    ("Filtered: Requires PhD", ("Filtered out", "Requires PhD")),
    ("Closed: Job posting closed or removed (404)", ("Closed", "Job posting closed or removed (404)")),
    ("Blocked (scrape failed)", ("Failed", "Blocked (scrape failed)")),
    ("Failed (could not add to Sheets: quota exceeded)", ("Failed", "Failed (could not add to Sheets: quota exceeded)")),
])
def test_result_maps_to_job_list_word(result, expected):
    parser = _Parser()
    _scraper(_Dedup(), parser=parser)._record_job_list_result(_listing(), result)

    assert parser.calls == [(URL, *expected)]


def test_false_no_longer_reaches_the_sheet_as_filtered_out():
    """
    The old bare False landed as "Filtered out" with no note. _process_listing no
    longer returns it; if some path ever does again, it must surface as a failure
    rather than masquerade as a filter decision.
    """
    parser = _Parser()
    _scraper(_Dedup(), parser=parser)._record_job_list_result(_listing(), False)

    assert parser.calls[0][1] != "Filtered out"


def test_rows_from_other_sources_are_not_written():
    parser = _Parser()
    _scraper(_Dedup(), parser=parser)._record_job_list_result(_listing(source="owner/repo"), True)

    assert parser.calls == []


def test_no_parser_is_a_no_op():
    _scraper(_Dedup(), parser=None)._record_job_list_result(_listing(), True)


# --- --limit counts new jobs only ---------------------------------------------------


@pytest.mark.parametrize("result, skipped", [
    (None, True),
    ("Skipped: same posting already on the sheet — Acme - SWE", True),
    (True, False),
    ("Filtered: Requires PhD", False),
    ("Closed: gone", False),
    ("Blocked (scrape failed)", False),
])
def test_skips_do_not_count_toward_limit(result, skipped):
    assert JobScraper._was_skipped(result) is skipped

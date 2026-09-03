"""
Skipping postings from a company with an application already in flight.

American Express relists one Campus Undergraduate programme per location and Booz
Allen separates its 2027 Summer Games roles by a comma, so company + position +
location + term never line up and identity dedup cannot collapse them. The company
itself is the only thing they share.

The rule deliberately reopens a company whose applications have all ended: a rejection
from one team does not bar another role, and this is the season to try again.
"""

from types import SimpleNamespace

import pytest

from src.deduplication import DeduplicationChecker
from src.sheets import has_live_application


def row(row_number=1, company="Acme", position="Intern", status="", application_date="",
        location="", season_year=""):
    return SimpleNamespace(
        row_number=row_number, company=company, position=position, status=status,
        application_date=application_date, location=location, season_year=season_year,
        position_url=f"https://example.com/{row_number}",
    )


class TestHasLiveApplication:
    """The predicate: applied, and not yet finished."""

    @pytest.mark.parametrize("status", ["Applied", "OA", "Phone", "Technical", "Offer"])
    def test_live_statuses(self, status):
        assert has_live_application(row(status=status))

    @pytest.mark.parametrize("status", ["Rejected", "Ghosted"])
    def test_terminal_statuses_are_not_live(self, status):
        """The company opens back up once every application there has ended."""
        assert not has_live_application(row(status=status))

    def test_new_with_no_application_date_is_not_live(self):
        assert not has_live_application(row(status="New"))

    def test_application_date_alone_counts(self):
        """Confirmations get misfiled, so a row can be applied to and still read New.
        An Application Date is enough on its own."""
        assert has_live_application(row(status="New", application_date="08/29/2026"))

    def test_rejected_beats_an_application_date(self):
        """A rejection ends the application whatever else the row carries."""
        assert not has_live_application(
            row(status="Rejected", application_date="08/29/2026")
        )


class TestCompanyHasLiveApplication:
    """The cache built from those rows, and the name matching over it."""

    def _checker(self, rows, struck=()):
        checker = DeduplicationChecker.__new__(DeduplicationChecker)
        checker._cached_urls = set()
        checker._cached_identities = set()
        checker._applied_companies = set()
        checker._sheets_client = SimpleNamespace(
            get_all_jobs=lambda: rows,
            get_struck_rows=lambda: set(struck),
        )
        checker.refresh_cache()
        return checker

    def test_company_with_live_application_is_matched(self):
        c = self._checker([row(1, "American Express", status="Applied")])
        assert c.company_has_live_application("American Express") == "American Express"

    def test_company_without_application_is_not_matched(self):
        c = self._checker([row(1, "American Express", status="New")])
        assert c.company_has_live_application("American Express") is None

    def test_rejected_company_reopens(self):
        c = self._checker([row(1, "Acme", status="Rejected")])
        assert c.company_has_live_application("Acme") is None

    def test_one_live_application_among_several_rejections_still_blocks(self):
        c = self._checker([
            row(1, "Acme", status="Rejected"),
            row(2, "Acme", status="OA"),
        ])
        assert c.company_has_live_application("Acme") == "Acme"

    def test_longer_scraped_name_still_matches(self):
        """The extractor and the sheet spell one employer several ways."""
        c = self._checker([row(1, "American Express", status="Applied")])
        assert c.company_has_live_application("American Express Company")

    def test_shorter_scraped_name_still_matches(self):
        c = self._checker([row(1, "Booz Allen Hamilton", status="Applied")])
        assert c.company_has_live_application("Booz Allen Hamilton Inc")

    def test_unrelated_company_is_not_matched(self):
        c = self._checker([row(1, "American Express", status="Applied")])
        assert c.company_has_live_application("Express Scripts") is None

    def test_struck_rows_do_not_speak_for_their_company(self):
        """A struck row is retired by hand; it should not suppress new postings."""
        c = self._checker([row(7, "Acme", status="Applied")], struck={7})
        assert c.company_has_live_application("Acme") is None

    def test_blank_company_never_matches(self):
        c = self._checker([row(1, "Acme", status="Applied")])
        assert c.company_has_live_application("") is None
        assert c.company_has_live_application("   ") is None

    def test_struck_row_failure_does_not_break_the_cache(self):
        """Losing strikethrough only costs tie-breaking; the run continues."""
        def boom():
            raise RuntimeError("sheets down")

        checker = DeduplicationChecker.__new__(DeduplicationChecker)
        checker._cached_urls = set()
        checker._cached_identities = set()
        checker._applied_companies = set()
        checker._sheets_client = SimpleNamespace(
            get_all_jobs=lambda: [row(1, "Acme", status="Applied")],
            get_struck_rows=boom,
        )
        checker.refresh_cache()
        assert checker.company_has_live_application("Acme") == "Acme"

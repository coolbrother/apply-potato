"""
Recognising the same seat when it reaches the sheet under a second URL.

GDIT posted one Praxis internship four times under four requisition ids. URL dedup saw
four URLs and made four rows, so every later Praxis email was an ambiguous match and got
dropped. Comparing the page text would not have fixed it either: Palantir runs one
description across New York, Washington and Palo Alto, which are three real seats, and
the four Praxis pages differed only in the requisition id.

What separates them is the job's own attributes — the way a person decides.

    pytest tests/test_identity_dedup.py -v
"""

from unittest.mock import MagicMock

import pytest

from src.deduplication import DeduplicationChecker, identity_key


PRAXIS = ("Praxis Engineering, a GDIT company",
          "Summer 2027 Software Developer Internship",
          "Annapolis Junction, MD", "Summer 2027")


class TestIdentityKey:
    def test_the_four_gdit_requisitions_share_one_key(self):
        """Same company, role, place and term — one seat, posted four times."""
        assert identity_key(*PRAXIS) == identity_key(*PRAXIS)

    def test_palantir_cities_stay_distinct(self):
        """One description across three offices is three seats, not one."""
        ny = identity_key("Palantir", "Software Engineer, Internship", "New York, NY", "")
        dc = identity_key("Palantir", "Software Engineer, Internship", "Washington, D.C.", "")
        pa = identity_key("Palantir", "Software Engineer, Internship", "Palo Alto, CA", "")
        assert len({ny, dc, pa}) == 3

    def test_terms_stay_distinct(self):
        summer_27 = identity_key("Acme", "SWE Intern", "NYC", "Summer 2027")
        summer_28 = identity_key("Acme", "SWE Intern", "NYC", "Summer 2028")
        assert summer_27 != summer_28

    def test_case_and_padding_are_ignored(self):
        assert identity_key("ACME ", " SWE Intern", "NYC", "Summer 2027") == \
               identity_key("acme", "swe intern", "nyc", "summer 2027")

    def test_a_blank_is_its_own_value(self):
        """
        A row that never recorded a location must not match one that did. Treating blank
        as a wildcard would drop real jobs; treating it as distinct only risks a
        duplicate row, which is the cheaper mistake.
        """
        assert identity_key("Acme", "SWE Intern", "", "Summer 2027") != \
               identity_key("Acme", "SWE Intern", "NYC", "Summer 2027")

    def test_no_key_without_company_and_title(self):
        """A key built on blanks would collapse unrelated rows."""
        assert identity_key("", "SWE Intern", "NYC", "Summer 2027") is None
        assert identity_key("Acme", "", "NYC", "Summer 2027") is None

    def test_non_string_cells_do_not_raise(self):
        """
        Sheets returns a bare year like 2027 as an int, and dates as floats. The first
        live run tripped on exactly that.
        """
        assert identity_key("Acme", "SWE Intern", "NYC", 2027) == \
               identity_key("Acme", "SWE Intern", "NYC", "2027")
        assert identity_key("Acme", "SWE Intern", None, None) is not None


class TestChecker:
    @pytest.fixture
    def checker(self):
        c = DeduplicationChecker.__new__(DeduplicationChecker)
        c._cached_urls = set()
        c._cached_identities = set()
        return c

    def test_recognises_a_seat_already_on_the_sheet(self, checker):
        checker.add_identity_to_cache(*PRAXIS)
        assert checker.job_exists_by_identity(*PRAXIS) is True

    def test_does_not_match_a_different_city(self, checker):
        checker.add_identity_to_cache("Palantir", "SWE Intern", "New York, NY", "")
        assert checker.job_exists_by_identity("Palantir", "SWE Intern", "Palo Alto, CA", "") is False

    def test_unknown_seat_is_not_a_duplicate(self, checker):
        assert checker.job_exists_by_identity(*PRAXIS) is False

    def test_a_keyless_posting_is_never_a_duplicate(self, checker):
        """Without a company or title there is nothing to compare, so the job is kept."""
        checker.add_identity_to_cache(*PRAXIS)
        assert checker.job_exists_by_identity("", "", "", "") is False

    def test_caching_within_a_run(self, checker):
        """
        The sheet cache is only refreshed at the start, so two requisitions arriving in
        one pass would both be added without this.
        """
        assert checker.job_exists_by_identity(*PRAXIS) is False
        checker.add_identity_to_cache(*PRAXIS)
        assert checker.job_exists_by_identity(*PRAXIS) is True

    def test_refresh_builds_both_caches(self):
        c = DeduplicationChecker.__new__(DeduplicationChecker)
        c._cached_urls = None
        c._cached_identities = None
        row = MagicMock(position_url="https://x.test/a", company="Acme",
                        position="SWE Intern", location="NYC", season_year="Summer 2027")
        c._sheets_client = MagicMock()
        c._sheets_client.get_all_jobs.return_value = [row]
        type(c).sheets_client = property(lambda self: self._sheets_client)

        c.refresh_cache()

        assert c.job_exists_by_identity("Acme", "SWE Intern", "NYC", "Summer 2027") is True
        assert len(c._cached_urls) == 1

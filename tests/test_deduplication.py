"""
Tests for deduplication module.

Usage:
    pytest tests/test_deduplication.py -v
    pytest tests/test_deduplication.py -v -m integration  # Include live Sheets test
"""

import pytest

from src.deduplication import (
    normalize_url,
    DeduplicationChecker,
)


class TestNormalizeUrl:
    """Test URL normalization."""

    @pytest.mark.parametrize("input_url,expected", [
        # Basic normalization
        (
            "https://boards.greenhouse.io/company/jobs/123",
            "https://boards.greenhouse.io/company/jobs/123",
        ),
        # Trailing slash removal
        (
            "https://boards.greenhouse.io/company/jobs/123/",
            "https://boards.greenhouse.io/company/jobs/123",
        ),
        # Lowercase domain
        (
            "HTTPS://Boards.Greenhouse.IO/company/jobs/123",
            "https://boards.greenhouse.io/company/jobs/123",
        ),
        # UTM params removal
        (
            "https://boards.greenhouse.io/company/jobs/123?utm_source=simplify&utm_medium=email",
            "https://boards.greenhouse.io/company/jobs/123",
        ),
        # Ref param removal
        (
            "https://boards.greenhouse.io/company/jobs/123?ref=github",
            "https://boards.greenhouse.io/company/jobs/123",
        ),
        # Mixed tracking params
        (
            "https://lever.co/company/abc123?utm_source=linkedin&ref=jobboard&gh_src=test",
            "https://lever.co/company/abc123",
        ),
        # Preserve job ID params
        (
            "https://careers.company.com/apply?job_id=12345",
            "https://careers.company.com/apply?job_id=12345",
        ),
        # Remove fragment
        (
            "https://boards.greenhouse.io/company/jobs/123#apply-section",
            "https://boards.greenhouse.io/company/jobs/123",
        ),
        # Complex URL with mixed params
        (
            "https://jobs.lever.co/company/abc-123?utm_source=github&posting_id=xyz&ref=simplify",
            "https://jobs.lever.co/company/abc-123?posting_id=xyz",
        ),
        # Workday URL
        (
            "https://company.wd5.myworkdayjobs.com/en-US/careers/job/NYC/Engineer_JR-001234?utm_source=linkedin",
            "https://company.wd5.myworkdayjobs.com/en-US/careers/job/NYC/Engineer_JR-001234",
        ),
        # Facebook/Google click tracking params
        (
            "https://careers.company.com/job/123?fbclid=abc123&gclid=xyz789",
            "https://careers.company.com/job/123",
        ),
        # Params starting with underscore
        (
            "https://careers.company.com/job/123?_ga=abc&_gl=xyz&id=456",
            "https://careers.company.com/job/123?id=456",
        ),
        # HTTP vs HTTPS normalization
        (
            "http://boards.greenhouse.io/company/jobs/123",
            "https://boards.greenhouse.io/company/jobs/123",
        ),
        (
            "http://stanfordhealthcare.wd5.myworkdayjobs.com/jobs/intern",
            "https://stanfordhealthcare.wd5.myworkdayjobs.com/jobs/intern",
        ),
        # Strip /apply from Lever URLs
        (
            "https://jobs.lever.co/company/abc123/apply",
            "https://jobs.lever.co/company/abc123",
        ),
        (
            "https://jobs.lever.co/company/abc123/apply/",
            "https://jobs.lever.co/company/abc123",
        ),
        # Strip /apply from Workable URLs
        (
            "https://apply.workable.com/altom-transport/j/DEADBB3616/apply",
            "https://apply.workable.com/altom-transport/j/DEADBB3616",
        ),
        (
            "https://apply.workable.com/altom-transport/j/DEADBB3616/apply/",
            "https://apply.workable.com/altom-transport/j/DEADBB3616",
        ),
        # Edge cases
        ("", ""),
        ("   ", ""),
        (
            "  https://example.com/job  ",
            "https://example.com/job",
        ),
    ])
    def test_normalize_url(self, input_url, expected):
        result = normalize_url(input_url)
        assert result == expected


class TestUrlDedupMatching:
    """Test URL-based deduplication matching."""

    @pytest.fixture
    def checker_with_cache(self):
        """Create a checker with pre-populated cache."""
        checker = DeduplicationChecker()
        checker._cached_urls = {
            "https://boards.greenhouse.io/google/jobs/123",
            "https://jobs.lever.co/meta/abc456",
            "https://amazon.wd5.myworkdayjobs.com/jobs/sde-intern",
            "https://careers.stripe.com/apply?job_id=789",
        }
        return checker

    @pytest.mark.parametrize("url,should_exist", [
        # Exact match
        ("https://boards.greenhouse.io/google/jobs/123", True),
        # With trailing slash
        ("https://boards.greenhouse.io/google/jobs/123/", True),
        # With tracking params
        ("https://boards.greenhouse.io/google/jobs/123?utm_source=simplify", True),
        # Different case
        ("HTTPS://Boards.Greenhouse.IO/google/jobs/123", True),
        # Different job at same company
        ("https://boards.greenhouse.io/google/jobs/456", False),
        # Lever job exists
        ("https://jobs.lever.co/meta/abc456", True),
        # Lever job with params
        ("https://jobs.lever.co/meta/abc456?ref=github&utm_source=linkedin", True),
        # Different Lever job
        ("https://jobs.lever.co/meta/def789", False),
        # Workday job exists
        ("https://amazon.wd5.myworkdayjobs.com/jobs/sde-intern", True),
        # Stripe job with job_id param
        ("https://careers.stripe.com/apply?job_id=789", True),
        # Stripe different job_id
        ("https://careers.stripe.com/apply?job_id=999", False),
        # Completely new URL
        ("https://careers.newcompany.com/job/123", False),
        # HTTP vs HTTPS should match
        ("http://boards.greenhouse.io/google/jobs/123", True),
        ("http://jobs.lever.co/meta/abc456", True),
    ])
    def test_url_dedup_matching(self, checker_with_cache, url, should_exist):
        result = checker_with_cache.job_exists(url)
        assert result == should_exist


class TestAddToCache:
    """Test adding URLs to cache."""

    def test_add_to_cache(self):
        checker = DeduplicationChecker()
        checker._cached_urls = set()

        # Add a URL with tracking params
        url = "https://boards.greenhouse.io/company/jobs/999?utm_source=test"
        checker.add_to_cache(url)

        # Should now exist (with normalization)
        assert checker.job_exists(url) is True
        assert checker.job_exists("https://boards.greenhouse.io/company/jobs/999") is True
        assert checker.job_exists("https://boards.greenhouse.io/company/jobs/999?ref=other") is True


@pytest.mark.integration
class TestLiveSheets:
    """Test with live Google Sheets (requires TEST_GOOGLE_SHEET_ID)."""

    def test_refresh_cache_from_sheets(self):
        """Test loading cache from live Google Sheets."""
        from src.config import get_config
        from src.logging_config import setup_logging
        import os

        if not os.getenv("TEST_GOOGLE_SHEET_ID"):
            pytest.skip("TEST_GOOGLE_SHEET_ID not set")

        config = get_config()
        setup_logging("dedup_test", config, console=True)

        checker = DeduplicationChecker(config)
        checker.refresh_cache()

        # Just verify it loaded without error
        assert isinstance(checker._cached_urls, set)

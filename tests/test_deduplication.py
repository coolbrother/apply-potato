#!/usr/bin/env python3
"""
Test script for deduplication module.

Usage:
    python test_deduplication.py                # Run all tests
    python test_deduplication.py --live         # Test with live Google Sheets
"""

import argparse

from src.config import get_config
from src.deduplication import (
    normalize_url,
    DeduplicationChecker,
)
from src.logging_config import setup_logging


def test_normalize_url():
    """Test URL normalization."""
    print("\n" + "=" * 60)
    print("Testing normalize_url()")
    print("=" * 60)

    test_cases = [
        # (input, expected)
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
        # HTTP vs HTTPS normalization (should both normalize to https)
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
    ]

    passed = 0
    failed = 0

    for input_url, expected in test_cases:
        result = normalize_url(input_url)
        status = "PASS" if result == expected else "FAIL"

        if result == expected:
            passed += 1
            print(f"  {status}: '{input_url[:60]}...' " if len(input_url) > 60 else f"  {status}: '{input_url}'")
        else:
            failed += 1
            print(f"  {status}: '{input_url}'")
            print(f"         Expected: '{expected}'")
            print(f"         Got:      '{result}'")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} URL normalization tests failed"


def test_url_dedup_matching():
    """Test URL-based deduplication matching."""
    print("\n" + "=" * 60)
    print("Testing DeduplicationChecker URL matching")
    print("=" * 60)

    checker = DeduplicationChecker()

    # Manually set cache with normalized URLs
    checker._cached_urls = {
        "https://boards.greenhouse.io/google/jobs/123",
        "https://jobs.lever.co/meta/abc456",
        "https://amazon.wd5.myworkdayjobs.com/jobs/sde-intern",
        "https://careers.stripe.com/apply?job_id=789",
    }

    test_cases = [
        # (url, should_exist)
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
        # HTTP vs HTTPS should match (both normalize to https)
        ("http://boards.greenhouse.io/google/jobs/123", True),
        ("http://jobs.lever.co/meta/abc456", True),
    ]

    passed = 0
    failed = 0

    for url, should_exist in test_cases:
        result = checker.job_exists(url)
        status = "PASS" if result == should_exist else "FAIL"

        if result == should_exist:
            passed += 1
            expected_str = "exists" if should_exist else "new"
            print(f"  {status}: {url[:50]}... -> {expected_str}" if len(url) > 50 else f"  {status}: {url} -> {expected_str}")
        else:
            failed += 1
            expected_str = "should exist" if should_exist else "should be new"
            result_str = "found" if result else "not found"
            print(f"  {status}: {url}")
            print(f"         {result_str} ({expected_str})")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} URL dedup matching tests failed"


def test_add_to_cache():
    """Test adding URLs to cache."""
    print("\n" + "=" * 60)
    print("Testing add_to_cache()")
    print("=" * 60)

    checker = DeduplicationChecker()
    checker._cached_urls = set()

    # Add a URL
    url = "https://boards.greenhouse.io/company/jobs/999?utm_source=test"
    checker.add_to_cache(url)

    # Should now exist (with normalization)
    exists = checker.job_exists(url)
    exists_normalized = checker.job_exists("https://boards.greenhouse.io/company/jobs/999")
    exists_with_other_params = checker.job_exists("https://boards.greenhouse.io/company/jobs/999?ref=other")

    results = [
        ("Original URL exists after add", exists, True),
        ("Normalized URL exists", exists_normalized, True),
        ("URL with different tracking params exists", exists_with_other_params, True),
    ]

    passed = 0
    failed = 0

    for desc, result, expected in results:
        status = "PASS" if result == expected else "FAIL"
        if result == expected:
            passed += 1
        else:
            failed += 1
        print(f"  {status}: {desc}")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} cache tests failed"


def run_live_sheets_test(args):
    """Test with live Google Sheets."""
    print("\n" + "=" * 60)
    print("Testing with LIVE Google Sheets")
    print("=" * 60)

    config = get_config()
    setup_logging("dedup_test", config, console=True)

    checker = DeduplicationChecker(config)

    print("\nRefreshing cache from Google Sheets...")
    checker.refresh_cache()

    print(f"\nLoaded {len(checker._cached_urls)} existing job URLs")

    if checker._cached_urls:
        print("\nSample of cached URLs (first 5):")
        for i, url in enumerate(list(checker._cached_urls)[:5]):
            print(f"  {i+1}. {url[:80]}..." if len(url) > 80 else f"  {i+1}. {url}")

    # Test with some sample queries
    test_urls = [
        "https://boards.greenhouse.io/test/jobs/12345",
        "https://jobs.lever.co/testcompany/abc123",
    ]

    print("\nTesting sample queries:")
    for url in test_urls:
        exists = checker.job_exists(url)
        status = "EXISTS" if exists else "NEW"
        print(f"  {url[:60]}... : {status}" if len(url) > 60 else f"  {url}: {status}")

    return True


def main():
    parser = argparse.ArgumentParser(description="Test deduplication module")
    parser.add_argument("--live", action="store_true", help="Test with live Google Sheets")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("DEDUPLICATION MODULE TESTS (URL-based)")
    print("=" * 60)

    failed_tests = []

    # Run unit tests
    try:
        test_normalize_url()
    except AssertionError as e:
        failed_tests.append(f"test_normalize_url: {e}")

    try:
        test_url_dedup_matching()
    except AssertionError as e:
        failed_tests.append(f"test_url_dedup_matching: {e}")

    try:
        test_add_to_cache()
    except AssertionError as e:
        failed_tests.append(f"test_add_to_cache: {e}")

    # Run live test if requested
    if args.live:
        run_live_sheets_test(args)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    if not failed_tests:
        print("All tests PASSED")
        return 0
    else:
        print("Some tests FAILED:")
        for test in failed_tests:
            print(f"  - {test}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

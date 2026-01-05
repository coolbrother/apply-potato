#!/usr/bin/env python3
"""
Test script for hard eligibility filters.

Usage:
    python test_filters.py
"""

from src.filters import (
    check_class_standing,
    check_graduation_timeline,
    check_season_year,
    check_work_authorization,
    check_job_type,
    _parse_class_standing,
    _parse_graduation_date,
)


def test_parse_class_standing():
    """Test class standing parsing."""
    print("\n" + "=" * 60)
    print("Testing _parse_class_standing()")
    print("=" * 60)

    test_cases = [
        # (input, expected_level)
        ("Freshman", 1),
        ("freshman", 1),
        ("First Year", 1),
        ("Sophomore", 2),
        ("Junior", 3),
        ("Senior", 4),
        ("Graduate", 5),
        ("Masters", 5),
        ("PhD", 6),
        # Rising patterns (returns level BEFORE the target)
        ("Rising Senior", 3),  # Currently Junior
        ("rising junior", 2),  # Currently Sophomore
        ("Rising Sophomore", 1),  # Currently Freshman
        # Entering patterns
        ("Entering junior year", 2),  # Currently Sophomore
        ("entering senior year", 3),  # Currently Junior
        # Special patterns
        ("Penultimate year", 3),  # Junior (2nd to last in 4-year)
        ("Final year", 4),  # Senior
        # "Matriculated in undergraduate" patterns (any undergrad qualifies)
        ("Matriculated in an undergraduate program", 1),
        ("Enrolled in undergraduate program", 1),
        ("Pursuing undergraduate degree", 1),
        ("matriculated in an undergraduate program in good standing", 1),
        # Edge cases
        ("", None),
        (None, None),
        ("Unknown", None),
    ]

    passed = 0
    failed = 0

    for input_text, expected in test_cases:
        result = _parse_class_standing(input_text) if input_text else None
        status = "PASS" if result == expected else "FAIL"

        if result == expected:
            passed += 1
            print(f"  {status}: '{input_text}' -> {result}")
        else:
            failed += 1
            print(f"  {status}: '{input_text}' -> {result} (expected: {expected})")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def test_check_class_standing():
    """Test class standing filter."""
    print("\n" + "=" * 60)
    print("Testing check_class_standing()")
    print("=" * 60)

    test_cases = [
        # (user_standing, job_requirement, should_pass)
        # Basic matches
        ("Junior", "Junior", True),
        ("Senior", "Junior", True),  # Senior >= Junior
        ("Sophomore", "Junior", False),  # Sophomore < Junior
        # Rising patterns
        ("Junior", "Rising Senior", True),  # Junior meets "Rising Senior" (currently Junior)
        ("Sophomore", "Rising Senior", False),  # Sophomore doesn't meet "Rising Senior"
        ("Junior", "Rising Junior", True),  # Junior meets "Rising Junior" (currently Sophomore)
        # Entering patterns
        ("Sophomore", "Entering junior year", True),  # Sophomore -> Junior
        ("Freshman", "Entering junior year", False),  # Freshman < Sophomore
        # Matriculated in undergraduate (any undergrad qualifies)
        ("Sophomore", "Matriculated in an undergraduate program in good standing", True),
        ("Freshman", "Enrolled in undergraduate program", True),
        ("Junior", "Pursuing undergraduate degree", True),
        ("Senior", "Matriculated in undergraduate", True),
        # No requirement = pass
        ("Freshman", None, True),
        ("Freshman", "", True),
        # Graduated user (no standing) = pass
        (None, "Junior", True),
        ("", "Senior", True),
    ]

    passed = 0
    failed = 0

    for user, job, should_pass in test_cases:
        result, reason = check_class_standing(user, job)
        status = "PASS" if result == should_pass else "FAIL"

        if result == should_pass:
            passed += 1
            expected = "pass" if should_pass else "fail"
            print(f"  {status}: user='{user}', job='{job}' -> {expected}")
        else:
            failed += 1
            expected = "pass" if should_pass else "fail"
            actual = "passed" if result else "failed"
            print(f"  {status}: user='{user}', job='{job}' -> {actual} (expected: {expected})")
            print(f"         Reason: {reason}")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def test_parse_graduation_date():
    """Test graduation date parsing."""
    print("\n" + "=" * 60)
    print("Testing _parse_graduation_date()")
    print("=" * 60)

    test_cases = [
        # (input, expected_year, expected_month)
        ("May 2026", 2026, 5),
        ("December 2025", 2025, 12),
        ("Spring 2026", 2026, 5),
        ("Fall 2025", 2025, 12),
        ("Summer 2025", 2025, 8),
        ("2026", 2026, 5),  # Year only defaults to May
        ("", None, None),
        (None, None, None),
    ]

    passed = 0
    failed = 0

    for input_text, exp_year, exp_month in test_cases:
        result = _parse_graduation_date(input_text) if input_text else None

        if exp_year is None:
            is_correct = result is None
        else:
            is_correct = result is not None and result.year == exp_year and result.month == exp_month

        status = "PASS" if is_correct else "FAIL"

        if is_correct:
            passed += 1
            if result:
                print(f"  {status}: '{input_text}' -> {result.year}-{result.month:02d}")
            else:
                print(f"  {status}: '{input_text}' -> None")
        else:
            failed += 1
            if result:
                print(f"  {status}: '{input_text}' -> {result.year}-{result.month:02d} (expected: {exp_year}-{exp_month:02d})")
            else:
                print(f"  {status}: '{input_text}' -> None (expected: {exp_year}-{exp_month:02d})")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def test_check_graduation_timeline():
    """Test graduation timeline filter."""
    print("\n" + "=" * 60)
    print("Testing check_graduation_timeline()")
    print("=" * 60)

    test_cases = [
        # (user_grad, job_timeline, should_pass)
        ("May 2026", "Must graduate by June 2026", True),
        ("May 2026", "Graduate by December 2025", False),
        ("December 2025", "December 2025", True),
        ("May 2025", "2026", True),  # Before deadline
        ("May 2027", "2026", False),  # After deadline
        # "between X and Y" range requirements
        ("May 2026", "Expected graduation between December 2025 and June 2027", True),  # In range
        ("May 2025", "Expected graduation between December 2025 and June 2027", False),  # Before range
        ("December 2027", "Expected graduation between December 2025 and June 2027", False),  # After range
        ("December 2025", "graduation between December 2025 and June 2027", True),  # At start of range
        ("June 2027", "graduation between December 2025 and June 2027", True),  # At end of range
        # No requirement = pass
        ("May 2026", None, True),
        ("May 2026", "", True),
        # No user date = pass
        (None, "June 2026", True),
        ("", "June 2026", True),
    ]

    passed = 0
    failed = 0

    for user, job, should_pass in test_cases:
        result, reason = check_graduation_timeline(user, job)
        status = "PASS" if result == should_pass else "FAIL"

        if result == should_pass:
            passed += 1
            expected = "pass" if should_pass else "fail"
            print(f"  {status}: user='{user}', job='{job}' -> {expected}")
        else:
            failed += 1
            expected = "pass" if should_pass else "fail"
            actual = "passed" if result else "failed"
            print(f"  {status}: user='{user}', job='{job}' -> {actual} (expected: {expected})")
            print(f"         Reason: {reason}")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def test_check_season_year():
    """Test season/year filter."""
    print("\n" + "=" * 60)
    print("Testing check_season_year()")
    print("=" * 60)

    test_cases = [
        # (user_target, job_season_year, should_pass)
        ("Summer 2025", "Summer 2025", True),
        ("Summer 2025", "summer 2025", True),  # Case insensitive
        ("Summer 2025", "Fall 2025", True),  # Same year = pass
        ("Summer 2025", "Summer 2026", False),  # Different year
        # No preference = pass
        (None, "Summer 2025", True),
        ("", "Fall 2026", True),
        # No job season = pass
        ("Summer 2025", None, True),
        ("Summer 2025", "", True),
        # Job has season but no year = pass (can't determine mismatch)
        ("Summer 2026", "Summer", True),
        ("Fall 2025", "Fall", True),
        ("Summer 2026", "Summer Internship", True),
    ]

    passed = 0
    failed = 0

    for user, job, should_pass in test_cases:
        result, reason = check_season_year(user, job)
        status = "PASS" if result == should_pass else "FAIL"

        if result == should_pass:
            passed += 1
            expected = "pass" if should_pass else "fail"
            print(f"  {status}: user='{user}', job='{job}' -> {expected}")
        else:
            failed += 1
            expected = "pass" if should_pass else "fail"
            actual = "passed" if result else "failed"
            print(f"  {status}: user='{user}', job='{job}' -> {actual} (expected: {expected})")
            print(f"         Reason: {reason}")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def test_check_work_authorization():
    """Test work authorization filter."""
    print("\n" + "=" * 60)
    print("Testing check_work_authorization()")
    print("=" * 60)

    test_cases = [
        # (user_auth, job_requirement, sponsorship_available, should_pass)
        # US Citizen passes everything
        ("US Citizen", "Must be authorized to work", None, True),
        ("US Citizen", "No sponsorship available", None, True),
        ("US Citizen", "Cannot sponsor", None, True),
        # Green Card passes everything
        ("Green Card", "No sponsorship", None, True),
        ("Permanent Resident", "Will not sponsor", None, True),
        # Need sponsorship
        ("Need Sponsorship", "No sponsorship available", None, False),
        ("Need Sponsorship", "Cannot sponsor", None, False),
        ("Need Sponsorship", "Will not sponsor", None, False),
        ("Need Sponsorship", "Must be authorized", None, True),  # Might sponsor
        ("Need Sponsorship", None, False, False),  # Explicit no sponsorship
        # OPT/CPT
        ("OPT", "No sponsorship", None, True),  # Can work temporarily
        ("CPT", "Must be authorized", None, True),
        # No requirement = pass
        ("Need Sponsorship", None, None, True),
        ("Need Sponsorship", "", None, True),
        # No user auth = pass
        (None, "No sponsorship", None, True),
    ]

    passed = 0
    failed = 0

    for user, job, sponsor, should_pass in test_cases:
        result, reason = check_work_authorization(user, job, sponsor)
        status = "PASS" if result == should_pass else "FAIL"

        if result == should_pass:
            passed += 1
            expected = "pass" if should_pass else "fail"
            print(f"  {status}: user='{user}', job='{job}', sponsor={sponsor} -> {expected}")
        else:
            failed += 1
            expected = "pass" if should_pass else "fail"
            actual = "passed" if result else "failed"
            print(f"  {status}: user='{user}', job='{job}', sponsor={sponsor}")
            print(f"         -> {actual} (expected: {expected})")
            print(f"         Reason: {reason}")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def test_check_job_type():
    """Test job type filter."""
    print("\n" + "=" * 60)
    print("Testing check_job_type()")
    print("=" * 60)

    test_cases = [
        # (user_target, job_type, should_pass)
        ("Internship", "Internship", True),
        ("Internship", "Summer Internship", True),  # Contains match
        ("Internship", "Full-Time", False),
        ("Full-Time", "Full-Time", True),
        ("Full-Time", "Internship", False),
        ("Both", "Internship", True),
        ("Both", "Full-Time", True),
        ("Both", "Contract", True),
        # No job type = pass
        ("Internship", None, True),
        ("Internship", "", True),
    ]

    passed = 0
    failed = 0

    for user, job, should_pass in test_cases:
        result, reason = check_job_type(user, job)
        status = "PASS" if result == should_pass else "FAIL"

        if result == should_pass:
            passed += 1
            expected = "pass" if should_pass else "fail"
            print(f"  {status}: user='{user}', job='{job}' -> {expected}")
        else:
            failed += 1
            expected = "pass" if should_pass else "fail"
            actual = "passed" if result else "failed"
            print(f"  {status}: user='{user}', job='{job}' -> {actual} (expected: {expected})")
            print(f"         Reason: {reason}")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def main():
    print("\n" + "=" * 60)
    print("HARD ELIGIBILITY FILTER TESTS")
    print("=" * 60)

    tests = [
        test_parse_class_standing,
        test_check_class_standing,
        test_parse_graduation_date,
        test_check_graduation_timeline,
        test_check_season_year,
        test_check_work_authorization,
        test_check_job_type,
    ]

    failed_tests = []
    for test in tests:
        try:
            test()
        except AssertionError as e:
            failed_tests.append(f"{test.__name__}: {e}")

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

#!/usr/bin/env python3
"""
Test script for soft fit scoring.

Usage:
    python test_scoring.py
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.scoring import (
    score_major_match,
    score_gpa_match,
    score_location_match,
    score_skills_match,
    score_company_match,
    score_salary_match,
)


def test_score_major_match():
    """Test major scoring."""
    print("\n" + "=" * 60)
    print("Testing score_major_match() [0-20 pts]")
    print("=" * 60)

    test_cases = [
        # (user_majors, user_minors, job_majors, job_category, expected)
        # Direct match
        (["Computer Science"], [], ["Computer Science"], None, 20),
        (["Computer Science"], [], ["CS", "Computer Science"], None, 20),
        # Partial match
        (["Computer Science"], [], ["Computer Science or equivalent"], None, 20),
        # Minor match
        (["Business"], ["Computer Science"], ["Computer Science"], None, 10),
        # Category relevance
        (["Computer Science"], [], [], "Software Engineering", 15),
        (["Data Science"], [], [], "Data Science/AI/ML", 15),
        # No requirement
        (["Art History"], [], [], None, 20),
        # "Or related" flexibility
        (["Physics"], [], ["Computer Science or related field"], None, 10),
    ]

    passed = 0
    failed = 0

    for majors, minors, job_majors, category, expected in test_cases:
        result = score_major_match(majors, minors, job_majors, category)
        status = "PASS" if result == expected else "FAIL"

        if result == expected:
            passed += 1
            print(f"  {status}: majors={majors}, job_majors={job_majors}, cat='{category}' -> {result} pts")
        else:
            failed += 1
            print(f"  {status}: majors={majors}, job_majors={job_majors}, cat='{category}'")
            print(f"         -> {result} pts (expected: {expected})")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def test_score_gpa_match():
    """Test GPA scoring."""
    print("\n" + "=" * 60)
    print("Testing score_gpa_match() [0-10 pts]")
    print("=" * 60)

    test_cases = [
        # (user_gpa, job_requirement, expected)
        # Meets/exceeds
        (3.8, 3.5, 10),
        (3.5, 3.5, 10),
        (4.0, 3.0, 10),
        # Within 0.3
        (3.3, 3.5, 5),
        (3.2, 3.5, 5),
        # Below by more than 0.3
        (3.0, 3.5, 0),
        (2.5, 3.5, 0),
        # No requirement
        (3.0, None, 10),
        (3.0, 0, 10),
        # No user GPA
        (0, 3.5, 5),
    ]

    passed = 0
    failed = 0

    for user_gpa, job_req, expected in test_cases:
        result = score_gpa_match(user_gpa, job_req)
        status = "PASS" if result == expected else "FAIL"

        if result == expected:
            passed += 1
            print(f"  {status}: user_gpa={user_gpa}, job_req={job_req} -> {result} pts")
        else:
            failed += 1
            print(f"  {status}: user_gpa={user_gpa}, job_req={job_req} -> {result} pts (expected: {expected})")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def test_score_location_match():
    """Test location scoring."""
    print("\n" + "=" * 60)
    print("Testing score_location_match() [0-10 pts]")
    print("=" * 60)

    test_cases = [
        # (user_locs, user_work_model, job_locs, job_remote, job_work_model, expected)
        # User accepts any
        ([], "Any", ["NYC"], False, "On-site", 10),
        (["NYC"], "Any", ["SF"], False, "On-site", 10),
        # Remote job, user accepts
        (["NYC"], "Remote", [], True, "Remote", 10),
        (["NYC"], "Any", [], True, None, 10),
        # Remote job, user prefers on-site
        (["NYC"], "On-site", [], True, "Remote", 5),
        # Location match
        (["NYC", "SF"], "On-site", ["New York City"], False, "On-site", 10),
        (["San Francisco"], "Hybrid", ["SF, CA"], False, "Hybrid", 10),
        # State abbreviation doesn't match full state name (edge case - 0 is acceptable)
        (["California"], "On-site", ["San Francisco, CA"], False, None, 0),
        # No match
        (["NYC"], "On-site", ["Seattle"], False, "On-site", 0),
    ]

    passed = 0
    failed = 0

    for user_locs, user_model, job_locs, job_remote, job_model, expected in test_cases:
        result = score_location_match(user_locs, user_model, job_locs, job_remote, job_model)
        status = "PASS" if result == expected else "FAIL"

        if result == expected:
            passed += 1
            print(f"  {status}: user={user_locs}, job={job_locs}, remote={job_remote} -> {result} pts")
        else:
            failed += 1
            print(f"  {status}: user={user_locs}, model='{user_model}', job={job_locs}, remote={job_remote}")
            print(f"         -> {result} pts (expected: {expected})")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def test_score_skills_match():
    """Test skills scoring."""
    print("\n" + "=" * 60)
    print("Testing score_skills_match() [0-20 pts]")
    print("=" * 60)

    test_cases = [
        # (user_skills, job_required, job_preferred, expected)
        # All skills match
        (["Python", "Java", "SQL"], ["Python", "Java"], ["SQL"], 20),
        # Partial required match (1/3 * 15 = 5, + 5 for no preferred = 10)
        (["Python"], ["Python", "Java", "Go"], [], 10),
        # No requirements (full credit)
        (["Python", "Java"], [], [], 20),
        # No user skills (partial credit)
        ([], ["Python", "Java"], ["SQL"], 10),
        # Mixed match (1/2 * 15 = 7 for required, 1/2 * 5 = 2 for preferred = 9)
        (["Python", "React"], ["Python", "Java"], ["React", "Node"], 9),
    ]

    passed = 0
    failed = 0

    for user, required, preferred, expected in test_cases:
        result = score_skills_match(user, required, preferred)
        status = "PASS" if result == expected else "FAIL"

        if result == expected:
            passed += 1
            print(f"  {status}: user={user[:2]}..., req={required[:2]}..., pref={preferred[:1]}... -> {result} pts")
        else:
            failed += 1
            print(f"  {status}: user={user}, req={required}, pref={preferred}")
            print(f"         -> {result} pts (expected: {expected})")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def test_score_company_match():
    """Test company preference scoring."""
    print("\n" + "=" * 60)
    print("Testing score_company_match() [0-30 pts]")
    print("=" * 60)

    test_cases = [
        # (user_companies, user_categories, job_company, job_category, expected)
        # Dream company + category match = 30
        (["Google", "Meta"], ["Software Engineering"], "Google", "Software Engineering", 30),
        # Dream company + different category = 20
        (["Google", "Meta"], ["Software Engineering"], "Google", "Product Management", 20),
        # Category match only = 20
        (["Google", "Meta"], ["Software Engineering"], "Amazon", "Software Engineering", 20),
        # No target companies, category match = 20
        ([], ["Software Engineering"], "Random Company", "Software Engineering", 20),
        # No target companies, no user categories = category match (20)
        ([], [], "Random Company", "Software Engineering", 20),
        # No category match, no company match = 0
        (["Google"], ["Software Engineering"], "Amazon", "Product Management", 0),
        # No job category = 0 (can't match)
        (["Google"], ["Software Engineering"], "Amazon", None, 0),
    ]

    passed = 0
    failed = 0

    for targets, categories, company, job_cat, expected in test_cases:
        result = score_company_match(targets, categories, company, job_cat)
        status = "PASS" if result == expected else "FAIL"

        if result == expected:
            passed += 1
            print(f"  {status}: targets={targets}, cat={categories}, company='{company}', job_cat='{job_cat}' -> {result} pts")
        else:
            failed += 1
            print(f"  {status}: targets={targets}, cat={categories}, company='{company}', job_cat='{job_cat}'")
            print(f"         -> {result} pts (expected: {expected})")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def test_score_salary_match():
    """Test salary scoring."""
    print("\n" + "=" * 60)
    print("Testing score_salary_match() [0-10 pts]")
    print("=" * 60)

    test_cases = [
        # (user_min_hourly, job_min, job_max, job_period, expected)
        # Meets user minimum
        (25, 30, 35, "hourly", 10),
        (25, 25, 30, "hourly", 10),
        # Within 80% of minimum
        (25, 20, 22, "hourly", 5),
        (25, 21, None, "hourly", 5),
        # Below 80% of minimum
        (25, 15, 18, "hourly", 0),
        (25, 19, None, "hourly", 0),
        # No user preference = full points
        (0, 20, 25, "hourly", 10),
        # No job salary info = partial
        (25, None, None, None, 5),
        # Yearly salary conversion (e.g., $60k/yr = ~$28.85/hr)
        (25, 60000, 70000, "yearly", 10),
        # Low yearly salary (e.g., $35k/yr = ~$16.83/hr, below 80% of $25)
        (25, 30000, 35000, "yearly", 0),
    ]

    passed = 0
    failed = 0

    for user_min, job_min, job_max, period, expected in test_cases:
        result = score_salary_match(user_min, job_min, job_max, period)
        status = "PASS" if result == expected else "FAIL"

        if result == expected:
            passed += 1
            print(f"  {status}: min=${user_min}/hr, job=${job_min}-{job_max} {period} -> {result} pts")
        else:
            failed += 1
            print(f"  {status}: min=${user_min}/hr, job=${job_min}-{job_max} {period}")
            print(f"         -> {result} pts (expected: {expected})")

    print(f"\nResults: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} tests failed"


def main():
    print("\n" + "=" * 60)
    print("SOFT FIT SCORING TESTS")
    print("=" * 60)

    tests = [
        test_score_major_match,
        test_score_gpa_match,
        test_score_location_match,
        test_score_skills_match,
        test_score_company_match,
        test_score_salary_match,
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

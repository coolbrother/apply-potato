"""
Tests for hard eligibility filters.

Usage:
    pytest tests/test_filters.py -v
"""

import pytest

from src.filters import (
    check_class_standing,
    check_graduation_timeline,
    check_season_year,
    check_work_authorization,
    check_job_type,
    _parse_class_standing,
    _parse_graduation_date,
)


class TestParseClassStanding:
    """Test class standing parsing."""

    @pytest.mark.parametrize("input_text,expected", [
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
        ("Rising Senior", 3),
        ("rising junior", 2),
        ("Rising Sophomore", 1),
        # Entering patterns
        ("Entering junior year", 2),
        ("entering senior year", 3),
        # Special patterns
        ("Penultimate year", 3),
        ("Final year", 4),
        # "Matriculated in undergraduate" patterns
        ("Matriculated in an undergraduate program", 1),
        ("Enrolled in undergraduate program", 1),
        ("Pursuing undergraduate degree", 1),
        ("matriculated in an undergraduate program in good standing", 1),
        # Edge cases
        ("", None),
        ("Unknown", None),
    ])
    def test_parse_class_standing(self, input_text, expected):
        result = _parse_class_standing(input_text) if input_text else None
        assert result == expected

    def test_parse_class_standing_none(self):
        assert _parse_class_standing(None) is None


class TestCheckClassStanding:
    """Test class standing filter."""

    @pytest.mark.parametrize("user_standing,job_requirement,should_pass", [
        # Basic matches
        ("Junior", "Junior", True),
        ("Senior", "Junior", True),
        ("Sophomore", "Junior", False),
        # Rising patterns
        ("Junior", "Rising Senior", True),
        ("Sophomore", "Rising Senior", False),
        ("Junior", "Rising Junior", True),
        # Entering patterns
        ("Sophomore", "Entering junior year", True),
        ("Freshman", "Entering junior year", False),
        # Matriculated in undergraduate
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
    ])
    def test_check_class_standing(self, user_standing, job_requirement, should_pass):
        result, reason = check_class_standing(user_standing, job_requirement)
        assert result == should_pass, f"Expected {should_pass}, got {result}. Reason: {reason}"


class TestParseGraduationDate:
    """Test graduation date parsing."""

    @pytest.mark.parametrize("input_text,exp_year,exp_month", [
        ("May 2026", 2026, 5),
        ("December 2025", 2025, 12),
        ("Spring 2026", 2026, 5),
        ("Fall 2025", 2025, 12),
        ("Summer 2025", 2025, 8),
        ("2026", 2026, 5),
    ])
    def test_parse_graduation_date(self, input_text, exp_year, exp_month):
        result = _parse_graduation_date(input_text)
        assert result is not None
        assert result.year == exp_year
        assert result.month == exp_month

    @pytest.mark.parametrize("input_text", ["", None])
    def test_parse_graduation_date_empty(self, input_text):
        result = _parse_graduation_date(input_text) if input_text else None
        assert result is None


class TestCheckGraduationTimeline:
    """Test graduation timeline filter."""

    @pytest.mark.parametrize("user_grad,job_timeline,should_pass", [
        ("May 2026", "Must graduate by June 2026", True),
        ("May 2026", "Graduate by December 2025", False),
        ("December 2025", "December 2025", True),
        ("May 2025", "2026", True),
        ("May 2027", "2026", False),
        # "between X and Y" range requirements
        ("May 2026", "Expected graduation between December 2025 and June 2027", True),
        ("May 2025", "Expected graduation between December 2025 and June 2027", False),
        ("December 2027", "Expected graduation between December 2025 and June 2027", False),
        ("December 2025", "graduation between December 2025 and June 2027", True),
        ("June 2027", "graduation between December 2025 and June 2027", True),
        # No requirement = pass
        ("May 2026", None, True),
        ("May 2026", "", True),
        # No user date = pass
        (None, "June 2026", True),
        ("", "June 2026", True),
    ])
    def test_check_graduation_timeline(self, user_grad, job_timeline, should_pass):
        result, reason = check_graduation_timeline(user_grad, job_timeline)
        assert result == should_pass, f"Expected {should_pass}, got {result}. Reason: {reason}"


class TestCheckSeasonYear:
    """Test season/year filter."""

    @pytest.mark.parametrize("user_target,job_season_year,should_pass", [
        ("Summer 2025", "Summer 2025", True),
        ("Summer 2025", "summer 2025", True),
        ("Summer 2025", "Fall 2025", True),
        ("Summer 2025", "Summer 2026", False),
        # No preference = pass
        (None, "Summer 2025", True),
        ("", "Fall 2026", True),
        # No job season = pass
        ("Summer 2025", None, True),
        ("Summer 2025", "", True),
        # Job has season but no year = pass
        ("Summer 2026", "Summer", True),
        ("Fall 2025", "Fall", True),
        ("Summer 2026", "Summer Internship", True),
    ])
    def test_check_season_year(self, user_target, job_season_year, should_pass):
        result, reason = check_season_year(user_target, job_season_year)
        assert result == should_pass, f"Expected {should_pass}, got {result}. Reason: {reason}"


class TestCheckWorkAuthorization:
    """Test work authorization filter."""

    @pytest.mark.parametrize("user_auth,job_requirement,sponsorship_available,should_pass", [
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
        ("Need Sponsorship", "Must be authorized", None, True),
        ("Need Sponsorship", None, False, False),
        # OPT/CPT
        ("OPT", "No sponsorship", None, True),
        ("CPT", "Must be authorized", None, True),
        # No requirement = pass
        ("Need Sponsorship", None, None, True),
        ("Need Sponsorship", "", None, True),
        # No user auth = pass
        (None, "No sponsorship", None, True),
    ])
    def test_check_work_authorization(self, user_auth, job_requirement, sponsorship_available, should_pass):
        result, reason = check_work_authorization(user_auth, job_requirement, sponsorship_available)
        assert result == should_pass, f"Expected {should_pass}, got {result}. Reason: {reason}"


class TestCheckJobType:
    """Test job type filter."""

    @pytest.mark.parametrize("user_target,job_type,should_pass", [
        ("Internship", "Internship", True),
        ("Internship", "Summer Internship", True),
        ("Internship", "Full-Time", False),
        ("Full-Time", "Full-Time", True),
        ("Full-Time", "Internship", False),
        ("Both", "Internship", True),
        ("Both", "Full-Time", True),
        ("Both", "Contract", True),
        # No job type = pass
        ("Internship", None, True),
        ("Internship", "", True),
    ])
    def test_check_job_type(self, user_target, job_type, should_pass):
        result, reason = check_job_type(user_target, job_type)
        assert result == should_pass, f"Expected {should_pass}, got {result}. Reason: {reason}"

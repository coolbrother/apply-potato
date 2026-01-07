"""
Tests for soft fit scoring.

Usage:
    pytest tests/test_scoring.py -v
"""

import pytest

from src.scoring import (
    score_major_match,
    score_gpa_match,
    score_location_match,
    score_skills_match,
    score_company_match,
    score_salary_match,
)


class TestScoreMajorMatch:
    """Test major scoring [0-20 pts]."""

    @pytest.mark.parametrize("user_majors,user_minors,job_majors,job_category,expected", [
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
    ])
    def test_score_major_match(self, user_majors, user_minors, job_majors, job_category, expected):
        result = score_major_match(user_majors, user_minors, job_majors, job_category)
        assert result == expected


class TestScoreGpaMatch:
    """Test GPA scoring [0-10 pts]."""

    @pytest.mark.parametrize("user_gpa,job_requirement,expected", [
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
    ])
    def test_score_gpa_match(self, user_gpa, job_requirement, expected):
        result = score_gpa_match(user_gpa, job_requirement)
        assert result == expected


class TestScoreLocationMatch:
    """Test location scoring [0-10 pts]."""

    @pytest.mark.parametrize("user_locs,user_work_model,job_locs,job_remote,job_work_model,expected", [
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
        # State abbreviation doesn't match full state name (edge case)
        (["California"], "On-site", ["San Francisco, CA"], False, None, 0),
        # No match
        (["NYC"], "On-site", ["Seattle"], False, "On-site", 0),
    ])
    def test_score_location_match(self, user_locs, user_work_model, job_locs, job_remote, job_work_model, expected):
        result = score_location_match(user_locs, user_work_model, job_locs, job_remote, job_work_model)
        assert result == expected


class TestScoreSkillsMatch:
    """Test skills scoring [0-20 pts]."""

    @pytest.mark.parametrize("user_skills,job_required,job_preferred,expected", [
        # All skills match
        (["Python", "Java", "SQL"], ["Python", "Java"], ["SQL"], 20),
        # Partial required match
        (["Python"], ["Python", "Java", "Go"], [], 10),
        # No requirements (full credit)
        (["Python", "Java"], [], [], 20),
        # No user skills (partial credit)
        ([], ["Python", "Java"], ["SQL"], 10),
        # Mixed match
        (["Python", "React"], ["Python", "Java"], ["React", "Node"], 9),
    ])
    def test_score_skills_match(self, user_skills, job_required, job_preferred, expected):
        result = score_skills_match(user_skills, job_required, job_preferred)
        assert result == expected


class TestScoreCompanyMatch:
    """Test company preference scoring [0-30 pts]."""

    @pytest.mark.parametrize("user_companies,user_categories,job_company,job_category,expected", [
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
    ])
    def test_score_company_match(self, user_companies, user_categories, job_company, job_category, expected):
        result = score_company_match(user_companies, user_categories, job_company, job_category)
        assert result == expected


class TestScoreSalaryMatch:
    """Test salary scoring [0-10 pts]."""

    @pytest.mark.parametrize("user_min_hourly,job_min,job_max,job_period,expected", [
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
        # Yearly salary conversion
        (25, 60000, 70000, "yearly", 10),
        # Low yearly salary
        (25, 30000, 35000, "yearly", 0),
    ])
    def test_score_salary_match(self, user_min_hourly, job_min, job_max, job_period, expected):
        result = score_salary_match(user_min_hourly, job_min, job_max, job_period)
        assert result == expected

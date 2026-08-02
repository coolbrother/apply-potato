"""
Tests for hard eligibility filters.

Usage:
    pytest tests/test_filters.py -v
"""

import pytest

from src.config import UserProfile
from src.ai_extractor import (
    ClassStandingRange,
    ExtractedJob,
    GraduationWindow,
    SeasonYearParsed,
)
from src.filters import (
    check_class_standing,
    check_graduation_timeline,
    check_season_year,
    check_work_authorization,
    check_job_type,
    passes_hard_filters,
    _extract_years,
    _parse_class_standing,
    _parse_graduation_date,
)

# The posting whose "...last requirement for you to graduate" was misread as the
# Graduate class standing, rejecting an eligible Junior.
GRADUATE_VERB_REQUIREMENT = (
    "At the end of the internship, you must return to school to continue your education "
    "or the internship must be the last requirement for you to graduate."
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
        ("Are enrolled in a Bachelor's degree or above", 1),
        ("Must be enrolled in a college degree program", 1),
        # "graduate" the NOUN is the Graduate standing...
        ("Graduate students only", 5),
        ("Must be enrolled in a graduate program", 5),
        # ...but "graduate" the VERB says nothing about year level
        (GRADUATE_VERB_REQUIREMENT, None),
        ("You must be on track to graduate", None),
        ("Interns are hired before they graduate", None),
        # Known limitation of the text fallback: other standing keywords still fire in
        # unrelated prose. The structured class_standing_range is the real fix; tightening
        # this further would risk false negatives. Asserted to document current behavior.
        ("Report directly to a senior engineer", 4),
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
        # "graduate" as a verb must not impose a Graduate-level floor
        ("Junior", GRADUATE_VERB_REQUIREMENT, True),
        ("Freshman", GRADUATE_VERB_REQUIREMENT, True),
        # ...while the noun sense still does
        ("Junior", "Graduate students only", False),
        ("Masters", "Graduate students only", True),
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


class TestGraduationBareRangeFallback:
    """
    Text-fallback handling of a range stated without the word "between".

    Campus postings often give a bare window ("Graduation Dates: November 2027 - August 2028").
    The fallback used to require the literal "between", so a bare window fell through to the
    default branch, which reads a timeline as a "graduate by" deadline and takes the first date
    it finds. That turned the opening month of the window into a cutoff and rejected every
    applicant graduating after it, i.e. most of the range the posting was inviting.

    The structured path (graduation_window) normally shadows this, since the AI does return
    the window for such postings. These cover the fallback for when it does not.
    """

    @pytest.mark.parametrize("user_grad,job_timeline,should_pass", [
        # The reported case, en dash, applicant inside the window
        ("May 2028", "Graduation Dates: November 2027 – August 2028", True),
        # Same window written with an ascii hyphen and an em dash
        ("May 2028", "Graduation Dates: November 2027 - August 2028", True),
        ("May 2028", "Graduation Dates: November 2027 — August 2028", True),
        # Other separators that introduce a range without saying "between"
        ("May 2028", "Graduation Dates: November 2027 to August 2028", True),
        ("May 2028", "Graduation dates November 2027 through August 2028", True),
        # Both ends of a bare window are still enforced
        ("October 2027", "Graduation Dates: November 2027 – August 2028", False),
        ("December 2028", "Graduation Dates: November 2027 – August 2028", False),
        # Bounds are sorted, so a window written backwards still reads correctly
        ("May 2028", "Graduation Dates: August 2028 – November 2027", True),
        # Season-year bounds, not just month-year
        ("May 2028", "Graduating Fall 2027 - Summer 2028", True),
        # A single date is not a range: the deadline reading must survive
        ("May 2028", "Must graduate by December 2028", True),
        ("May 2028", "Must graduate by December 2027", False),
        # Patterns that run before the range check keep priority
        ("May 2028", "Graduation date December 2027 or later", True),
        ("May 2028", "Must be currently enrolled during Fall 2027", True),
        ("May 2028", "Not graduating before May 2027", True),
    ])
    def test_bare_range(self, user_grad, job_timeline, should_pass):
        result, reason = check_graduation_timeline(user_grad, job_timeline)
        assert result == should_pass, f"Expected {should_pass}, got {result}. Reason: {reason}"


class TestCheckSeasonYear:
    """Test season/year filter."""

    @pytest.mark.parametrize("user_target,job_season_year,should_pass", [
        ("Summer 2025", "Summer 2025", True),
        ("Summer 2025", "summer 2025", True),
        # Text fallback stays year-only: it cannot reliably identify a season in prose.
        # The structured path DOES enforce the season - see TestSeasonYearStructured.
        ("Summer 2025", "Fall 2025", True),
        ("Summer 2025", "Summer 2026", False),
        # Academic-year spans name every year they cover, not just the first
        ("Summer 2027", "2026/2027", True),
        ("Summer 2027", "2026-2027", True),
        ("Summer 2027", "2026/27", True),
        ("Summer 2027", "Summer 2026/2027", True),
        ("Summer 2027", "2025/2026", False),
        ("Summer 2026", "2026/2027", True),
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


class TestExtractYears:
    """Test year extraction, including academic-year spans."""

    @pytest.mark.parametrize("text,expected", [
        ("Summer 2026", {"2026"}),
        ("2026/2027", {"2026", "2027"}),
        ("2026-2027", {"2026", "2027"}),
        # Two-digit shorthand expands against the leading century
        ("2026/27", {"2026", "2027"}),
        ("Summer Internship", set()),
        ("", set()),
        (None, set()),
    ])
    def test_extract_years(self, text, expected):
        assert _extract_years(text) == expected


class TestClassStandingStructured:
    """Test the AI-normalized class standing range, including the upper bound."""

    @pytest.mark.parametrize("user_standing,minimum,maximum,should_pass", [
        # Upper bound: "freshmen and sophomores only" excludes juniors
        ("Freshman", "Freshman", "Sophomore", True),
        ("Sophomore", "Freshman", "Sophomore", True),
        ("Junior", "Freshman", "Sophomore", False),
        ("Senior", "Freshman", "Sophomore", False),
        # Closed list "Junior or Senior"
        ("Sophomore", "Junior", "Senior", False),
        ("Junior", "Junior", "Senior", True),
        ("Senior", "Junior", "Senior", True),
        ("Masters", "Junior", "Senior", False),
        # Open-ended "Junior or above" sets no ceiling
        ("Sophomore", "Junior", None, False),
        ("Junior", "Junior", None, True),
        ("PhD", "Junior", None, True),
        # Enrollment-only postings: any student qualifies
        ("Freshman", "Freshman", None, True),
        ("Junior", "Freshman", None, True),
        # Graduate-only
        ("Senior", "Graduate", "PhD", False),
        ("Masters", "Graduate", "PhD", True),
        # Both bounds absent = no constraint
        ("Freshman", None, None, True),
    ])
    def test_standing_range(self, user_standing, minimum, maximum, should_pass):
        standing_range = ClassStandingRange(minimum=minimum, maximum=maximum)
        result, reason = check_class_standing(user_standing, None, standing_range)
        assert result == should_pass, f"Expected {should_pass}, got {result}. Reason: {reason}"

    def test_structured_wins_over_text(self):
        """The structured range is authoritative when both are present."""
        standing_range = ClassStandingRange(minimum="Freshman", maximum=None)
        # Text alone would parse "graduate" and reject a Junior; the range must win
        result, _ = check_class_standing("Junior", GRADUATE_VERB_REQUIREMENT, standing_range)
        assert result is True

    def test_unrecognized_value_fails_open(self):
        """An out-of-vocabulary standing from the AI must not reject the job."""
        standing_range = ClassStandingRange(minimum="Postdoc", maximum=None)
        result, _ = check_class_standing("Junior", None, standing_range)
        assert result is True

    def test_graduated_user_passes(self):
        """A user with no class standing is unaffected by a range."""
        standing_range = ClassStandingRange(minimum="Freshman", maximum="Sophomore")
        result, _ = check_class_standing(None, None, standing_range)
        assert result is True


class TestGraduationWindowStructured:
    """Test the AI-normalized graduation window."""

    @pytest.mark.parametrize("user_grad,earliest,latest,should_pass", [
        # Floor only ("December 2027 or later", "not graduating before May 2026")
        ("May 2028", "2026-05", None, True),
        ("May 2026", "2026-05", None, True),          # bound is inclusive
        ("December 2025", "2026-05", None, False),
        # Deadline only ("Must graduate by June 2026")
        ("May 2026", None, "2026-06", True),
        ("June 2026", None, "2026-06", True),         # bound is inclusive
        ("December 2026", None, "2026-06", False),
        # Range ("between Dec 2025 and June 2026")
        ("May 2026", "2025-12", "2026-06", True),
        ("May 2025", "2025-12", "2026-06", False),
        ("December 2027", "2025-12", "2026-06", False),
        # Unbounded
        ("May 2026", None, None, True),
    ])
    def test_graduation_window(self, user_grad, earliest, latest, should_pass):
        window = GraduationWindow(earliest=earliest, latest=latest)
        result, reason = check_graduation_timeline(user_grad, None, window)
        assert result == should_pass, f"Expected {should_pass}, got {result}. Reason: {reason}"

    def test_malformed_bound_fails_open(self):
        """An unparseable bound must not reject the job."""
        window = GraduationWindow(earliest="not-a-date", latest=None)
        result, _ = check_graduation_timeline("May 2026", None, window)
        assert result is True

    def test_no_user_date_passes(self):
        result, _ = check_graduation_timeline(None, None, GraduationWindow(earliest="2030-05"))
        assert result is True


class TestInvertedGraduationWindow:
    """
    The AI intermittently files a floor date ("graduating in Fall 2027 or later") as
    "latest", inverting an open-ended minimum into a deadline. When the verbatim timeline
    says "or later" and the window offers only a ceiling, the window contradicts its own
    source text and the text wins.
    """

    @pytest.mark.parametrize("timeline", [
        "graduating in Fall 2027 or later",
        "Graduating in Fall 2027 or later",       # capitalisation varies run to run
        "graduation date December 2027 and later",
        "graduating in 2028 or beyond",
        "must graduate no earlier than May 2027",
        "graduating May 2027 onwards",
    ])
    def test_floor_language_repairs_inverted_window(self, timeline):
        """The PDT Partners case: user graduates after the floor, so the job qualifies."""
        window = GraduationWindow(earliest=None, latest="2027-12")
        result, reason = check_graduation_timeline("May 2028", timeline, window)
        assert result is True, f"Expected pass for {timeline!r}. Reason: {reason}"

    def test_repaired_floor_still_rejects_early_graduate(self):
        """Repairing the bound must not make it toothless — it is still a floor."""
        window = GraduationWindow(earliest=None, latest="2027-12")
        result, _ = check_graduation_timeline("May 2026", "graduating in Fall 2027 or later", window)
        assert result is False

    @pytest.mark.parametrize("timeline", [
        "Must graduate by June 2026",
        "graduation must occur before June 2026",
        "Graduation: May 2026",
        None,
    ])
    def test_genuine_deadline_is_left_alone(self, timeline):
        """No floor language means the ceiling is real and must still reject."""
        window = GraduationWindow(earliest=None, latest="2026-06")
        result, _ = check_graduation_timeline("May 2028", timeline, window)
        assert result is False

    def test_explicit_range_is_not_repaired(self):
        """With an earliest already set the window is a range, not an inversion."""
        window = GraduationWindow(earliest="2025-12", latest="2026-06")
        result, _ = check_graduation_timeline(
            "May 2028", "graduating between Dec 2025 and June 2026 or later", window
        )
        assert result is False


class TestSeasonYearStructured:
    """Test the AI-normalized season/year, which enforces the season."""

    @pytest.mark.parametrize("user_target,season,years,should_pass", [
        ("Summer 2027", "Summer", [2027], True),
        ("Summer 2027", "Summer", [2026, 2027], True),
        # The reported case: an academic-year span covering the target year
        ("Summer 2027", None, [2026, 2027], True),
        ("Summer 2027", None, [2025, 2026], False),
        # Season is now enforced (the text fallback still allows this)
        ("Summer 2027", "Fall", [2027], False),
        ("Summer 2025", "Fall", [2025], False),
        ("Fall 2025", "Fall", [2025], True),
        # A posting naming no season cannot mismatch one
        ("Summer 2027", None, [2027], True),
        # A posting naming no year falls back to the season alone
        ("Summer 2027", "Summer", [], True),
        ("Summer 2027", "Fall", [], False),
        # "autumn" normalizes to "fall"
        ("Fall 2026", "Autumn", [2026], True),
    ])
    def test_season_year_parsed(self, user_target, season, years, should_pass):
        parsed = SeasonYearParsed(season=season, years=years)
        result, reason = check_season_year(user_target, None, parsed)
        assert result == should_pass, f"Expected {should_pass}, got {result}. Reason: {reason}"

    def test_no_user_preference_passes(self):
        parsed = SeasonYearParsed(season="Fall", years=[2030])
        result, _ = check_season_year(None, None, parsed)
        assert result is True


class TestPassesHardFiltersStructured:
    """End-to-end filtering with and without the structured fields."""

    def _user(self):
        """A fresh profile per test - the shared fixture is session-scoped."""
        return UserProfile(
            name="Test User",
            email="test@example.com",
            class_standing="Junior",
            graduation_date="May 2028",
            majors=["Computer Science"],
            minors=[],
            gpa=3.5,
            work_authorization="US Citizen",
            target_job_type="Internship",
            target_season_year="Summer 2027",
            preferred_locations=[],
            work_model="Any",
            min_salary_hourly=0.0,
            target_companies=[],
            skills=[],
            job_categories=["Software Engineering"],
            degree_level="Bachelors",
        )

    def test_reported_posting_now_passes(self):
        """Both reported bugs, exercised through the real entry point."""
        job = ExtractedJob(
            company="Example Co",
            title="Software Engineer Intern",
            job_type="Internship",
            class_standing_requirement=GRADUATE_VERB_REQUIREMENT,
            season_year="2026/2027",
        )
        passed, reason, category = passes_hard_filters(self._user(), job)
        assert passed is True, f"Rejected by {category}: {reason}"

    def test_legacy_job_without_structured_fields_still_filters(self):
        """An ExtractedJob predating the new fields still uses the text path."""
        job = ExtractedJob(
            company="Example Co",
            title="Software Engineer Intern",
            job_type="Internship",
            class_standing_requirement="Must be a Senior",
            season_year="Summer 2027",
        )
        passed, reason, category = passes_hard_filters(self._user(), job)
        assert passed is False
        assert category == "class_standing"

    def test_structured_ceiling_rejects(self):
        """The new upper bound rejects a Junior from a sophomores-only posting."""
        job = ExtractedJob(
            company="Example Co",
            title="Software Engineer Intern",
            job_type="Internship",
            class_standing_requirement="Freshmen and sophomores only",
            class_standing_range=ClassStandingRange(minimum="Freshman", maximum="Sophomore"),
            season_year="Summer 2027",
        )
        passed, reason, category = passes_hard_filters(self._user(), job)
        assert passed is False
        assert category == "class_standing"
        assert "above maximum" in reason

    def test_structured_season_rejects(self):
        """Season enforcement rejects a Fall posting for a Summer target."""
        job = ExtractedJob(
            company="Example Co",
            title="Software Engineer Intern",
            job_type="Internship",
            season_year="Fall 2027",
            season_year_parsed=SeasonYearParsed(season="Fall", years=[2027]),
        )
        passed, reason, category = passes_hard_filters(self._user(), job)
        assert passed is False
        assert category == "season_year"

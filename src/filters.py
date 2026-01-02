"""
Hard eligibility filters for job postings.
Jobs that fail any hard filter are skipped entirely.
"""

import logging
import re
from datetime import datetime
from typing import Optional, Tuple

from .config import Config, UserProfile, get_config
from .ai_extractor import ExtractedJob


logger = logging.getLogger(__name__)


# Class standing levels (higher = more senior)
CLASS_STANDING_LEVELS = {
    "freshman": 1,
    "first year": 1,
    "first-year": 1,
    "1st year": 1,
    "sophomore": 2,
    "second year": 2,
    "second-year": 2,
    "2nd year": 2,
    "junior": 3,
    "third year": 3,
    "third-year": 3,
    "3rd year": 3,
    "senior": 4,
    "fourth year": 4,
    "fourth-year": 4,
    "4th year": 4,
    "graduate": 5,
    "masters": 5,
    "master's": 5,
    "phd": 6,
    "doctoral": 6,
}

# Patterns to extract class standing from job requirements
RISING_PATTERN = re.compile(r"rising\s+(\w+)", re.IGNORECASE)
ENTERING_PATTERN = re.compile(r"entering\s+(\w+)(?:\s+year)?", re.IGNORECASE)
PENULTIMATE_PATTERN = re.compile(r"penultimate\s+year", re.IGNORECASE)
FINAL_YEAR_PATTERN = re.compile(r"final\s+year", re.IGNORECASE)
# "Matriculated in undergraduate" = enrolled in any undergrad program
UNDERGRADUATE_PATTERN = re.compile(r"(matriculated|enrolled|pursuing).{0,20}undergraduate", re.IGNORECASE)
# "Current student" = any currently enrolled student (level 1)
CURRENT_STUDENT_PATTERN = re.compile(r"current\s+student|currently\s+(enrolled|a\s+student)", re.IGNORECASE)

# Work authorization levels (higher = more restrictive requirement the user can meet)
WORK_AUTH_LEVELS = {
    "us citizen": 5,
    "citizen": 5,
    "green card": 4,
    "permanent resident": 4,
    "opt": 3,
    "cpt": 3,
    "h1b": 2,
    "h-1b": 2,
    "need sponsorship": 1,
    "requires sponsorship": 1,
}


def _parse_class_standing(text: str) -> Optional[int]:
    """
    Parse class standing text to a numeric level.

    Handles variations like:
    - "Junior" -> 3
    - "Rising Senior" -> currently Junior (3), seeking Senior internship
    - "Entering junior year" -> currently Sophomore (2)
    - "Penultimate year" -> second-to-last year (depends on program)
    - "Matriculated in undergraduate" -> any undergrad (1)

    Returns the MINIMUM class standing required (what the student must currently be).
    """
    if not text:
        return None

    text_lower = text.lower().strip()

    # Check for "current student" pattern - any student qualifies (level 1)
    if CURRENT_STUDENT_PATTERN.search(text_lower):
        return 1

    # Check for "matriculated/enrolled in undergraduate" pattern
    # This means any undergraduate qualifies (level 1)
    if UNDERGRADUATE_PATTERN.search(text_lower):
        return 1

    # Check for "rising X" pattern (e.g., "rising senior" = currently junior)
    rising_match = RISING_PATTERN.search(text_lower)
    if rising_match:
        target = rising_match.group(1).lower()
        if target in CLASS_STANDING_LEVELS:
            # Rising X means you're currently one level below X
            return max(1, CLASS_STANDING_LEVELS[target] - 1)

    # Check for "entering X year" pattern (e.g., "entering junior year" = currently sophomore)
    entering_match = ENTERING_PATTERN.search(text_lower)
    if entering_match:
        target = entering_match.group(1).lower()
        if target in CLASS_STANDING_LEVELS:
            # Entering X means you're currently one level below X
            return max(1, CLASS_STANDING_LEVELS[target] - 1)

    # Check for "penultimate year" (second-to-last year)
    if PENULTIMATE_PATTERN.search(text_lower):
        # For a 4-year program, penultimate = junior (3)
        return 3

    # Check for "final year"
    if FINAL_YEAR_PATTERN.search(text_lower):
        return 4  # Senior

    # Direct match
    for standing, level in CLASS_STANDING_LEVELS.items():
        if standing in text_lower:
            return level

    return None


def _parse_graduation_date(text: str) -> Optional[datetime]:
    """
    Parse graduation date from text like "May 2026", "Spring 2026", "2026".

    Returns approximate graduation date.
    """
    if not text:
        return None

    text = text.strip()

    # Try common formats
    patterns = [
        # "May 2026", "December 2025"
        (r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})",
         lambda m: datetime(int(m.group(2)), _month_to_num(m.group(1)), 15)),
        # "Spring 2026", "Fall 2025"
        (r"(spring|summer|fall|winter)\s+(\d{4})",
         lambda m: datetime(int(m.group(2)), _season_to_month(m.group(1)), 15)),
        # Just year "2026"
        (r"^(\d{4})$",
         lambda m: datetime(int(m.group(1)), 5, 15)),  # Assume May graduation
    ]

    for pattern, handler in patterns:
        match = re.search(pattern, text.lower())
        if match:
            try:
                return handler(match)
            except (ValueError, TypeError):
                continue

    return None


def _month_to_num(month: str) -> int:
    """Convert month name to number."""
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12
    }
    return months.get(month.lower(), 5)


def _season_to_month(season: str) -> int:
    """Convert season to approximate month."""
    seasons = {
        "spring": 5,   # May
        "summer": 8,   # August
        "fall": 12,    # December
        "winter": 12,  # December
    }
    return seasons.get(season.lower(), 5)


def _parse_work_auth_level(text: str) -> Optional[int]:
    """
    Parse work authorization requirement to a level.

    Higher level = user has better authorization (can meet more restrictions).
    """
    if not text:
        return None

    text_lower = text.lower().strip()

    for auth, level in WORK_AUTH_LEVELS.items():
        if auth in text_lower:
            return level

    return None


def check_class_standing(user_standing: Optional[str], job_requirement: Optional[str]) -> Tuple[bool, str]:
    """
    Check if user's class standing meets job requirement.

    Args:
        user_standing: User's current class standing (e.g., "Junior")
        job_requirement: Job's class standing requirement (e.g., "Rising Senior")

    Returns:
        Tuple of (passes, reason)
    """
    # No requirement = pass
    if not job_requirement:
        return True, "No class standing requirement"

    # User graduated (no class standing) = pass for any job
    if not user_standing:
        return True, "User is graduated"

    user_level = _parse_class_standing(user_standing)
    job_level = _parse_class_standing(job_requirement)

    if user_level is None:
        logger.warning(f"Could not parse user class standing: {user_standing}")
        return True, f"Could not parse user standing: {user_standing}"

    if job_level is None:
        logger.warning(f"Could not parse job class standing requirement: {job_requirement}")
        return True, f"Could not parse job requirement: {job_requirement}"

    if user_level >= job_level:
        return True, f"User ({user_standing}) meets requirement ({job_requirement})"
    else:
        return False, f"User ({user_standing}) does not meet requirement ({job_requirement})"


def check_graduation_timeline(user_grad_date: Optional[str], job_timeline: Optional[str]) -> Tuple[bool, str]:
    """
    Check if user's graduation date fits job's timeline.

    Handles three types of requirements:
    1. Enrollment questions: "Are you enrolled during Summer 2026?" - user must still be a student
    2. Graduate after: "graduation date December 2027 or later" - user must graduate AFTER date
    3. Graduate by: "Must graduate by June 2026" - user must graduate BEFORE date

    Args:
        user_grad_date: User's expected graduation (e.g., "May 2028")
        job_timeline: Job's graduation requirement

    Returns:
        Tuple of (passes, reason)
    """
    # No requirement = pass
    if not job_timeline:
        return True, "No graduation timeline requirement"

    # No user graduation date = pass (already graduated or not specified)
    if not user_grad_date:
        return True, "User graduation date not specified"

    job_lower = job_timeline.lower()
    user_date = _parse_graduation_date(user_grad_date)

    if user_date is None:
        logger.warning(f"Could not parse user graduation date: {user_grad_date}")
        return True, f"Could not parse user graduation: {user_grad_date}"

    # Pattern 1: Enrollment questions ("enrolled during X", "currently enrolled", "pursuing")
    # If asking about enrollment during a period, user must NOT have graduated yet by then
    if "enrolled" in job_lower or "pursuing" in job_lower:
        period_date = _parse_graduation_date(job_timeline)
        if period_date:
            # User must still be enrolled (not graduated) during that period
            # If user graduates AFTER the period, they're enrolled during it
            if user_date > period_date:
                return True, f"User will be enrolled during requested period"
            else:
                return False, f"User graduates ({user_grad_date}) before enrollment period ends"
        return True, "Could not parse enrollment period"

    # Pattern 2: "Graduate after/later than X" requirements
    if any(kw in job_lower for kw in ["or later", "and later", "after", "no earlier than"]):
        min_date = _parse_graduation_date(job_timeline)
        if min_date:
            if user_date >= min_date:
                return True, f"User graduates ({user_grad_date}) meets minimum requirement"
            else:
                return False, f"User graduates ({user_grad_date}) before minimum ({job_timeline})"
        return True, "Could not parse minimum graduation date"

    # Pattern 3: "between X and Y" range requirement
    if "between" in job_lower:
        # Extract all month+year dates from the range
        dates = re.findall(
            r"((?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4})",
            job_lower
        )
        if len(dates) >= 2:
            min_date = _parse_graduation_date(dates[0])
            max_date = _parse_graduation_date(dates[1])
            if min_date and max_date:
                if min_date <= user_date <= max_date:
                    return True, f"User graduates ({user_grad_date}) within range"
                else:
                    return False, f"User graduates ({user_grad_date}) outside range ({job_timeline})"
        return True, "Could not parse graduation range"

    # Pattern 3.5: "Not graduating before X" - must graduate ON or AFTER X (minimum requirement)
    # This must come BEFORE Pattern 4 to avoid false match on "before"
    if re.search(r"not\s+graduat\w*\s+before", job_lower):
        min_date = _parse_graduation_date(job_timeline)
        if min_date:
            if user_date >= min_date:
                return True, f"User graduates ({user_grad_date}) on/after minimum ({job_timeline})"
            else:
                return False, f"User graduates ({user_grad_date}) before minimum ({job_timeline})"
        return True, "Could not parse minimum graduation date"

    # Pattern 4: "Graduate by/before X" requirements (deadline)
    if any(kw in job_lower for kw in ["by", "before", "no later than", "must graduate"]):
        max_date = _parse_graduation_date(job_timeline)
        if max_date:
            if user_date <= max_date:
                return True, f"User graduates ({user_grad_date}) before deadline"
            else:
                return False, f"User graduates ({user_grad_date}) after deadline ({job_timeline})"
        return True, "Could not parse graduation deadline"

    # Default: Try to parse as a simple date and use original logic
    job_date = _parse_graduation_date(job_timeline)
    if job_date is None:
        logger.debug(f"Could not parse job graduation timeline: {job_timeline}")
        return True, f"Could not parse job timeline: {job_timeline}"

    # Default behavior: treat as deadline (graduate by)
    if user_date <= job_date:
        return True, f"User graduates ({user_grad_date}) before deadline ({job_timeline})"
    else:
        return False, f"User graduates ({user_grad_date}) after deadline ({job_timeline})"


def check_season_year(user_target: Optional[str], job_season_year: Optional[str]) -> Tuple[bool, str]:
    """
    Check if job's season/year matches user's preference.

    Args:
        user_target: User's target season/year (e.g., "Summer 2025") or None for any
        job_season_year: Job's season/year (e.g., "Summer 2025")

    Returns:
        Tuple of (passes, reason)
    """
    # User has no preference = pass
    if not user_target:
        return True, "User has no season/year preference"

    # Job has no season/year specified = pass
    if not job_season_year:
        return True, "Job has no season/year specified"

    # Normalize and compare
    user_norm = user_target.lower().strip()
    job_norm = job_season_year.lower().strip()

    if user_norm == job_norm:
        return True, f"Season/year matches: {job_season_year}"

    # Check if year matches at least
    user_year = re.search(r"\d{4}", user_target)
    job_year = re.search(r"\d{4}", job_season_year)

    # Job has no year specified (e.g., just "Summer") = pass (can't determine mismatch)
    if not job_year:
        return True, f"Job has no year specified: {job_season_year}"

    if user_year and job_year and user_year.group() == job_year.group():
        # Same year, different season - might be close enough
        return True, f"Year matches: {job_year.group()}"

    return False, f"Season/year mismatch: user wants {user_target}, job is {job_season_year}"


def check_work_authorization(user_auth: Optional[str], job_requirement: Optional[str],
                             sponsorship_available: Optional[bool] = None) -> Tuple[bool, str]:
    """
    Check if user's work authorization meets job requirement.

    Args:
        user_auth: User's work authorization (e.g., "US Citizen", "Need Sponsorship")
        job_requirement: Job's authorization requirement (e.g., "Must be authorized to work")
        sponsorship_available: Whether job offers sponsorship

    Returns:
        Tuple of (passes, reason)
    """
    if not user_auth:
        return True, "User authorization not specified"

    user_lower = user_auth.lower()

    # Check sponsorship_available flag first (explicit signal)
    if sponsorship_available is False:
        if "need sponsorship" in user_lower or "requires sponsorship" in user_lower:
            return False, "User needs sponsorship but job does not sponsor"

    # No text requirement and sponsorship not explicitly denied = pass
    if not job_requirement:
        return True, "No work authorization requirement"

    job_lower = job_requirement.lower()
    user_lower = user_auth.lower()

    # Check if job explicitly says no sponsorship
    no_sponsorship_keywords = [
        "no sponsorship", "not sponsor", "cannot sponsor", "won't sponsor",
        "will not sponsor", "unable to sponsor", "not able to sponsor",
        "without sponsorship", "not provide sponsorship"
    ]

    job_no_sponsorship = any(kw in job_lower for kw in no_sponsorship_keywords)

    # If sponsorship_available is explicitly False, same as no sponsorship
    if sponsorship_available is False:
        job_no_sponsorship = True

    # User needs sponsorship but job doesn't offer it
    if "need sponsorship" in user_lower or "requires sponsorship" in user_lower:
        if job_no_sponsorship:
            return False, f"User needs sponsorship but job does not sponsor"
        # Job might sponsor, pass
        return True, "User needs sponsorship, job may sponsor"

    # User is citizen/green card - meets any requirement
    if any(auth in user_lower for auth in ["citizen", "green card", "permanent resident"]):
        return True, f"User ({user_auth}) meets any authorization requirement"

    # User has OPT/CPT
    if any(auth in user_lower for auth in ["opt", "cpt"]):
        if job_no_sponsorship:
            # OPT/CPT might work temporarily but they'll eventually need sponsorship
            # This is a gray area - pass with warning
            return True, f"User ({user_auth}) may meet temporary requirement"
        return True, f"User ({user_auth}) authorized to work"

    # Default: pass (can't determine for sure)
    return True, f"Could not determine authorization match"


def check_job_type(user_target: str, job_type: Optional[str]) -> Tuple[bool, str]:
    """
    Check if job type matches user's preference.

    Args:
        user_target: User's target job type ("Internship", "Full-Time", "Both")
        job_type: Job's type ("Internship", "Full-Time", etc.)

    Returns:
        Tuple of (passes, reason)
    """
    # User wants both = pass
    if user_target.lower() == "both":
        return True, "User accepts any job type"

    # Job type not specified = pass
    if not job_type:
        return True, "Job type not specified"

    # Normalize and compare
    user_norm = user_target.lower().strip()
    job_norm = job_type.lower().strip()

    if user_norm in job_norm or job_norm in user_norm:
        return True, f"Job type matches: {job_type}"

    return False, f"Job type mismatch: user wants {user_target}, job is {job_type}"


def passes_hard_filters(user: UserProfile, job: ExtractedJob) -> Tuple[bool, str]:
    """
    Check if a job passes all hard eligibility filters.

    Args:
        user: User profile from config
        job: Extracted job data

    Returns:
        Tuple of (passes, reason for failure if any)
    """
    # Check job type first (most common filter)
    passed, reason = check_job_type(user.target_job_type, job.job_type)
    if not passed:
        logger.debug(f"Job failed job type filter: {reason}")
        return False, reason

    # Check class standing
    passed, reason = check_class_standing(user.class_standing, job.class_standing_requirement)
    if not passed:
        logger.debug(f"Job failed class standing filter: {reason}")
        return False, reason

    # Check graduation timeline
    passed, reason = check_graduation_timeline(user.graduation_date, job.graduation_timeline)
    if not passed:
        logger.debug(f"Job failed graduation timeline filter: {reason}")
        return False, reason

    # Check season/year
    passed, reason = check_season_year(user.target_season_year, job.season_year)
    if not passed:
        logger.debug(f"Job failed season/year filter: {reason}")
        return False, reason

    # Check work authorization
    passed, reason = check_work_authorization(
        user.work_authorization,
        job.work_authorization,
        job.sponsorship_available
    )
    if not passed:
        logger.debug(f"Job failed work authorization filter: {reason}")
        return False, reason

    return True, "Passed all hard filters"


def filter_job(job: ExtractedJob, config: Optional[Config] = None) -> Tuple[bool, str]:
    """
    Convenience function to filter a job using global config.

    Args:
        job: Extracted job data
        config: Optional config (uses global if not provided)

    Returns:
        Tuple of (passes, reason)
    """
    if config is None:
        config = get_config()

    return passes_hard_filters(config.user, job)

"""
Hard eligibility filters for job postings.
Jobs that fail any hard filter are skipped entirely.
"""

import logging
import re
from datetime import datetime
from typing import Optional, Tuple

from .config import (
    Config,
    UserProfile,
    get_config,
    ELIGIBILITY_MODE_AI,
)
from .ai_extractor import (
    ClassStandingRange,
    ExtractedJob,
    GraduationWindow,
    SeasonYearParsed,
)


logger = logging.getLogger(__name__)


# Class standing levels (higher = more senior)
CLASS_STANDING_LEVELS = {
    "undergraduate": 1,
    "undergrad": 1,
    "bachelor": 1,  # covers bachelor's, bachelors
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
APPROACHING_PATTERN = re.compile(r"approaching\s+(?:their\s+|the\s+)?(?:final|last)\s+year", re.IGNORECASE)
PENULTIMATE_PATTERN = re.compile(r"penultimate\s+year", re.IGNORECASE)
FINAL_YEAR_PATTERN = re.compile(r"final\s+year", re.IGNORECASE)
# "Matriculated/enrolled in undergraduate/bachelor's/college degree" = any undergrad student (level 1)
UNDERGRADUATE_PATTERN = re.compile(
    r"(matriculated|enrolled|pursuing).{0,30}(undergraduate|bachelor|college\s+degree|degree\s+program)",
    re.IGNORECASE,
)
# "Current student" = any currently enrolled student (level 1)
CURRENT_STUDENT_PATTERN = re.compile(r"current\s+student|currently\s+(enrolled|a\s+student)", re.IGNORECASE)

# "graduate" is a class standing only in the NOUN sense ("graduate student"). As a verb
# ("...the last requirement for you to graduate") it describes finishing a degree and says
# nothing about year level, so it must not be read as level 5. A bare "Graduate" on its own
# is a standing value, hence the second alternative.
GRADUATE_NOUN_PATTERN = re.compile(
    r"\b(?:graduate|grad)\s+(?:student|program|degree|school|studies|level|candidate|standing)s?\b"
    r"|^\s*graduate\s*$",
    re.IGNORECASE,
)

# Standings whose bare keyword is ambiguous in prose and needs its own pattern above.
AMBIGUOUS_STANDINGS = {"graduate"}

# Word-boundary matcher per standing keyword. Plain substring matching lets a keyword fire
# inside an unrelated word or phrase; re.escape() escapes spaces on this interpreter, so
# multi-word keys are rebuilt with \s+ to tolerate irregular spacing.
CLASS_STANDING_PATTERNS = [
    (
        re.compile(r"\b" + re.escape(standing).replace("\\ ", r"\s+") + r"\b", re.IGNORECASE),
        level,
    )
    for standing, level in CLASS_STANDING_LEVELS.items()
    if standing not in AMBIGUOUS_STANDINGS
]

# Phrases that make a graduation date a FLOOR ("graduate on or after X") rather than a
# deadline. Deliberately narrower than the Pattern 2 keyword list below: a bare "after"
# appears in unrelated prose ("after the internship"), and this set is used to overrule
# the AI, so it must not fire on an ambiguous match.
GRADUATION_FLOOR_PHRASES = (
    "or later",
    "and later",
    "or beyond",
    "and beyond",
    "or after",
    "no earlier than",
    "onwards",
    "onward",
)

# Academic-year shorthand: "2026/27" covers both 2026 and 2027.
ACADEMIC_YEAR_PATTERN = re.compile(r"\b(\d{4})\s*[/-]\s*(\d{2})\b")

# Seasons as written in postings; "autumn" normalizes to "fall".
SEASON_PATTERN = re.compile(r"\b(spring|summer|fall|autumn|winter)\b", re.IGNORECASE)

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

    # Check for "approaching their final year" — currently one year before final (Junior)
    if APPROACHING_PATTERN.search(text_lower):
        return 3

    # Check for "penultimate year" (second-to-last year)
    if PENULTIMATE_PATTERN.search(text_lower):
        # For a 4-year program, penultimate = junior (3)
        return 3

    # Check for "final year"
    if FINAL_YEAR_PATTERN.search(text_lower):
        return 4  # Senior

    # Collect all direct matches and return the minimum (OR semantics — satisfying any one is enough)
    matched_levels = [level for pattern, level in CLASS_STANDING_PATTERNS if pattern.search(text_lower)]

    # "graduate" only counts in the noun sense, so it is matched separately
    if GRADUATE_NOUN_PATTERN.search(text_lower):
        matched_levels.append(CLASS_STANDING_LEVELS["graduate"])

    if matched_levels:
        return min(matched_levels)

    return None


def _standing_to_level(standing: Optional[str]) -> Optional[int]:
    """
    Map a normalized standing name from the AI to its numeric level.

    This is a controlled vocabulary (Freshman..PhD), so it is a plain lookup with no
    prose parsing. An unrecognized value returns None, which the caller treats as
    "no bound" — consistent with the module's fail-open design.
    """
    if not standing:
        return None

    level = CLASS_STANDING_LEVELS.get(standing.strip().lower())
    if level is None:
        logger.warning(f"Unrecognized normalized class standing from AI: {standing!r}")
    return level


def _parse_year_month(text: Optional[str]) -> Optional[datetime]:
    """
    Parse a "YYYY-MM" bound from the AI's graduation_window.

    Falls back to the free-text parser so an unexpected format ("May 2026") still works.
    Uses day 15 to match _parse_graduation_date, so bound comparisons are inclusive.
    """
    if not text:
        return None

    match = re.match(r"^\s*(\d{4})-(\d{1,2})\s*$", str(text))
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return datetime(year, month, 15)
        logger.warning(f"Invalid month in graduation_window bound: {text!r}")
        return None

    return _parse_graduation_date(str(text))


def _extract_years(text: Optional[str]) -> set:
    """
    All 4-digit years named in text, expanding "YYYY/YY" academic-year shorthand.

    An academic year names every year it spans, so "2026/2027" and "2026/27" both
    yield {"2026", "2027"}. Returned as strings for direct set comparison.
    """
    if not text:
        return set()

    years = set(re.findall(r"\b(\d{4})\b", text))
    for full_year, suffix in ACADEMIC_YEAR_PATTERN.findall(text):
        years.add(full_year)
        years.add(full_year[:2] + suffix)

    return years


def _extract_season(text: Optional[str]) -> Optional[str]:
    """Extract a normalized lowercase season name from text, or None."""
    if not text:
        return None

    match = SEASON_PATTERN.search(text)
    if not match:
        return None

    season = match.group(1).lower()
    return "fall" if season == "autumn" else season


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


def check_class_standing(user_standing: Optional[str], job_requirement: Optional[str],
                         standing_range: Optional["ClassStandingRange"] = None) -> Tuple[bool, str]:
    """
    Check if user's class standing meets job requirement.

    Prefers the AI's normalized bounds when available and falls back to parsing the
    verbatim requirement text. Only the structured path enforces an upper bound — prose
    gives no reliable way to tell a ceiling from a list of examples.

    Args:
        user_standing: User's current class standing (e.g., "Junior")
        job_requirement: Job's class standing requirement (e.g., "Rising Senior")
        standing_range: AI-normalized minimum/maximum bounds, if extracted

    Returns:
        Tuple of (passes, reason)
    """
    # User graduated (no class standing) = pass for any job
    if not user_standing:
        return True, "User is graduated"

    user_level = _parse_class_standing(user_standing)

    if user_level is None:
        logger.warning(f"Could not parse user class standing: {user_standing}")
        return True, f"Could not parse user standing: {user_standing}"

    # Structured path: compare against the AI's normalized bounds
    if standing_range is not None:
        min_level = _standing_to_level(standing_range.minimum)
        max_level = _standing_to_level(standing_range.maximum)

        if min_level is not None and user_level < min_level:
            # A claim that the user must be a graduate student has to be visible in the
            # text the AI itself quoted. It repeatedly is not: given "Graduating between
            # December 2027 and July 2028" it files that sentence as the class standing
            # requirement and reads "Graduating" as the Graduate level, rejecting an
            # undergraduate the posting invited. Millennium and PDT Partners both failed
            # this way, and five successive prompt rules did not stop it.
            #
            # Only graduate-and-above minimums are checked. Freshman..Senior come from
            # year-level words that are rarely invented, and demanding evidence for them
            # would weaken filters that work. Consistent with this module's fail-open
            # design: an unverifiable bound yields to the other filters rather than
            # rejecting outright.
            # Only fires when there IS quoted text that fails to support the claim. With
            # no text there is nothing contradicting the AI, so the bound stands — this
            # overrules the extractor using its own evidence, never in the absence of it.
            if min_level >= CLASS_STANDING_LEVELS["graduate"] and job_requirement:
                text_level = _parse_class_standing(job_requirement)
                if text_level is None or text_level < min_level:
                    logger.info(
                        f"Class standing: normalized minimum ({standing_range.minimum}) is "
                        f"unsupported by the quoted requirement {job_requirement!r}; ignoring it"
                    )
                    return True, (
                        f"User ({user_standing}) kept — normalized minimum "
                        f"({standing_range.minimum}) unsupported by the posting text"
                    )

            return False, f"User ({user_standing}) is below minimum standing ({standing_range.minimum})"

        if max_level is not None and user_level > max_level:
            return False, f"User ({user_standing}) is above maximum standing ({standing_range.maximum})"

        return True, f"User ({user_standing}) is within required standing range"

    # No requirement = pass
    if not job_requirement:
        return True, "No class standing requirement"

    job_level = _parse_class_standing(job_requirement)

    if job_level is None:
        logger.warning(f"Could not parse job class standing requirement: {job_requirement}")
        return True, f"Could not parse job requirement: {job_requirement}"

    if user_level >= job_level:
        return True, f"User ({user_standing}) meets requirement ({job_requirement})"
    else:
        return False, f"User ({user_standing}) does not meet requirement ({job_requirement})"


def check_graduation_timeline(user_grad_date: Optional[str], job_timeline: Optional[str],
                              grad_window: Optional["GraduationWindow"] = None) -> Tuple[bool, str]:
    """
    Check if user's graduation date fits job's timeline.

    Prefers the AI's normalized window when available. The text fallback below handles
    these requirement shapes:
    1. Enrollment questions: "Are you enrolled during Summer 2026?" - user must still be a student
    2. Graduate after: "graduation date December 2027 or later" - user must graduate AFTER date
    3. Graduate by: "Must graduate by June 2026" - user must graduate BEFORE date

    Args:
        user_grad_date: User's expected graduation (e.g., "May 2028")
        job_timeline: Job's graduation requirement
        grad_window: AI-normalized earliest/latest bounds, if extracted

    Returns:
        Tuple of (passes, reason)
    """
    # No user graduation date = pass (already graduated or not specified)
    if not user_grad_date:
        return True, "User graduation date not specified"

    user_date = _parse_graduation_date(user_grad_date)

    if user_date is None:
        logger.warning(f"Could not parse user graduation date: {user_grad_date}")
        return True, f"Could not parse user graduation: {user_grad_date}"

    # Structured path: bounds check against the AI's normalized window
    if grad_window is not None:
        earliest = _parse_year_month(grad_window.earliest)
        latest = _parse_year_month(grad_window.latest)

        # Repair an inverted window. "graduating in Fall 2027 or later" is a floor, but the
        # AI intermittently files that date as "latest", which flips an open-ended minimum
        # into a deadline and rejects exactly the students the posting invited. The verbatim
        # timeline is quoted from the posting and is the more reliable of the two fields, so
        # when it plainly says "or later" and the window offers only a ceiling, the window is
        # contradicting its own source text: trust the text.
        if (
            latest is not None
            and earliest is None
            and job_timeline
            and any(phrase in job_timeline.lower() for phrase in GRADUATION_FLOOR_PHRASES)
        ):
            logger.info(
                f"Graduation window inverted: {job_timeline!r} is a floor, but the AI set "
                f"latest={grad_window.latest!r} with no earliest. Reading it as earliest."
            )
            earliest, latest = latest, None

        if earliest is not None and user_date < earliest:
            return False, f"User graduates ({user_grad_date}) before earliest allowed ({grad_window.earliest})"

        if latest is not None and user_date > latest:
            return False, f"User graduates ({user_grad_date}) after latest allowed ({grad_window.latest})"

        return True, f"User graduates ({user_grad_date}) within the allowed window"

    # No requirement = pass
    if not job_timeline:
        return True, "No graduation timeline requirement"

    job_lower = job_timeline.lower()

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

    # Pattern 3: range requirement. "between X and Y" is the explicit form, but postings also
    # state a bare window ("Graduation Dates: November 2027 - August 2028") with no keyword at
    # all. Reading the opening date of such a window as a deadline rejects everyone who
    # graduates after it, which is most of the range the posting is actually inviting.
    dates = re.findall(
        r"((?:january|february|march|april|may|june|july|august|september|october|november|december"
        r"|spring|summer|fall|winter)\s+\d{4})",
        job_lower
    )
    has_range_separator = any(sep in job_lower for sep in ("between", " to ", "through", "-", "–", "—"))

    if len(dates) >= 2 and has_range_separator:
        bounds = [d for d in (_parse_graduation_date(text) for text in dates) if d is not None]
        if len(bounds) >= 2:
            # Sort rather than trust document order, so a reversed window still reads correctly
            min_date, max_date = min(bounds), max(bounds)
            if min_date <= user_date <= max_date:
                return True, f"User graduates ({user_grad_date}) within range ({job_timeline})"
            else:
                return False, f"User graduates ({user_grad_date}) outside range ({job_timeline})"

    if "between" in job_lower:
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


def check_season_year(user_target: Optional[str], job_season_year: Optional[str],
                      season_parsed: Optional["SeasonYearParsed"] = None) -> Tuple[bool, str]:
    """
    Check if job's season/year matches user's preference.

    The structured path enforces the season as well as the year. The text fallback stays
    year-only: it cannot reliably identify a season in prose, and guessing one there would
    manufacture false negatives.

    Args:
        user_target: User's target season/year (e.g., "Summer 2025") or None for any
        job_season_year: Job's season/year (e.g., "Summer 2025")
        season_parsed: AI-normalized season and years, if extracted

    Returns:
        Tuple of (passes, reason)
    """
    # User has no preference = pass
    if not user_target:
        return True, "User has no season/year preference"

    # Structured path: compare years as sets, then require the season to agree
    if season_parsed is not None:
        user_years = _extract_years(user_target)
        job_years = {str(year) for year in season_parsed.years}

        if job_years and user_years and not (user_years & job_years):
            return False, f"Year mismatch: user wants {user_target}, job covers {sorted(job_years)}"

        # A posting that names no season cannot mismatch one, so it passes on years alone.
        # Both sides go through _extract_season so "autumn" normalizes to "fall" on either.
        job_season = _extract_season(season_parsed.season)
        user_season = _extract_season(user_target)

        if job_season and user_season and job_season != user_season:
            return False, f"Season mismatch: user wants {user_target}, job is {season_parsed.season}"

        return True, f"Season/year matches user target ({user_target})"

    # Job has no season/year specified = pass
    if not job_season_year:
        return True, "Job has no season/year specified"

    # Normalize and compare
    user_norm = user_target.lower().strip()
    job_norm = job_season_year.lower().strip()

    if user_norm == job_norm:
        return True, f"Season/year matches: {job_season_year}"

    # Compare every year named on each side — an academic year like "2026/2027" names two,
    # and matching only the first one rejects jobs the user is eligible for
    user_years = _extract_years(user_target)
    job_years = _extract_years(job_season_year)

    # Job has no year specified (e.g., just "Summer") = pass (can't determine mismatch)
    if not job_years:
        return True, f"Job has no year specified: {job_season_year}"

    overlap = user_years & job_years
    if overlap:
        # Same year, different season - might be close enough
        return True, f"Year matches: {sorted(overlap)[0]}"

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


def passes_hard_filters(
    user: UserProfile,
    job: ExtractedJob,
    judgment=None,
    mode: Optional[str] = None,
) -> Tuple[bool, str, str]:
    """
    Check if a job passes all hard eligibility filters.

    Args:
        user: User profile from config
        job: Extracted job data
        judgment: Optional EligibilityJudgment from the AI pass. Only consulted in
            "ai" mode, and only when it validated — an unusable judgment falls through
            to the code path rather than deciding anything.
        mode: Eligibility mode override. Defaults to config.eligibility_mode.

    Returns:
        Tuple of (passes, reason, category) where category is the failing filter name
        or "none" if all passed.
    """
    # Check job type first (most common filter). Mechanical filters run in code in
    # every mode: they are exact comparisons over well-formed fields, they work, and
    # they cost nothing.
    passed, reason = check_job_type(user.target_job_type, job.job_type)
    if not passed:
        logger.debug(f"Job failed job type filter: {reason}")
        return False, reason, "job_type"

    # In "ai" mode the pass owns class standing, graduation and work authorization.
    # It is consulted only after the mechanical filters, so a job the user never
    # wanted is rejected on the cheap check rather than on an eligibility verdict.
    if mode is None:
        mode = get_config().eligibility_mode
    if mode == ELIGIBILITY_MODE_AI and judgment is not None and judgment.usable:
        if not judgment.eligible:
            failing = judgment.failing_check
            category = failing.dimension if failing else "eligibility"
            logger.debug(f"Job failed AI eligibility: {judgment.reason()}")
            return False, judgment.reason(), category
        # The pass cleared the prose dimensions; season/year still applies.
        passed, reason = check_season_year(
            user.target_season_year,
            job.season_year,
            job.season_year_parsed,
        )
        if not passed:
            logger.debug(f"Job failed season/year filter: {reason}")
            return False, reason, "season_year"
        return True, "Passed all hard filters (AI eligibility)", "none"

    # Check class standing
    passed, reason = check_class_standing(
        user.class_standing,
        job.class_standing_requirement,
        job.class_standing_range
    )
    if not passed:
        logger.debug(f"Job failed class standing filter: {reason}")
        return False, reason, "class_standing"

    # Check graduation timeline
    passed, reason = check_graduation_timeline(
        user.graduation_date,
        job.graduation_timeline,
        job.graduation_window
    )
    if not passed:
        logger.debug(f"Job failed graduation timeline filter: {reason}")
        return False, reason, "graduation"

    # Check season/year
    passed, reason = check_season_year(
        user.target_season_year,
        job.season_year,
        job.season_year_parsed
    )
    if not passed:
        logger.debug(f"Job failed season/year filter: {reason}")
        return False, reason, "season_year"

    # Check work authorization
    passed, reason = check_work_authorization(
        user.work_authorization,
        job.work_authorization,
        job.sponsorship_available
    )
    if not passed:
        logger.debug(f"Job failed work authorization filter: {reason}")
        return False, reason, "work_auth"

    return True, "Passed all hard filters", "none"


def filter_job(job: ExtractedJob, config: Optional[Config] = None) -> Tuple[bool, str, str]:
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

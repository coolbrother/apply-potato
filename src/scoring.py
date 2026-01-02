"""
Soft fit scoring for job postings.
Calculates a 0-100 score to help users prioritize jobs.
Score is informational only - does not filter jobs.
"""

import logging
from typing import List, Optional, Tuple

from .config import Config, UserProfile, get_config
from .ai_extractor import ExtractedJob


logger = logging.getLogger(__name__)


# Common city abbreviations/aliases
CITY_ALIASES = {
    "nyc": ["new york", "new york city", "manhattan"],
    "sf": ["san francisco", "san fran"],
    "la": ["los angeles"],
    "dc": ["washington dc", "washington d.c."],
    "chi": ["chicago"],
    "atl": ["atlanta"],
    "sea": ["seattle"],
    "bos": ["boston"],
    "aus": ["austin"],
    "den": ["denver"],
}


# Major to job category mapping for relevance scoring
MAJOR_CATEGORY_MAP = {
    # Computer Science related
    "computer science": ["Software Engineering", "Data Science/AI/ML", "Quantitative Finance"],
    "cs": ["Software Engineering", "Data Science/AI/ML", "Quantitative Finance"],
    "software engineering": ["Software Engineering"],
    "computer engineering": ["Software Engineering", "Hardware Engineering"],
    "electrical engineering": ["Hardware Engineering", "Software Engineering"],
    "information technology": ["Software Engineering"],
    "information systems": ["Software Engineering"],
    # Data Science related
    "data science": ["Data Science/AI/ML", "Quantitative Finance"],
    "statistics": ["Data Science/AI/ML", "Quantitative Finance"],
    "mathematics": ["Data Science/AI/ML", "Quantitative Finance", "Software Engineering"],
    "applied mathematics": ["Data Science/AI/ML", "Quantitative Finance"],
    # Business related
    "business": ["Product Management"],
    "business administration": ["Product Management"],
    "mba": ["Product Management"],
    "economics": ["Product Management", "Quantitative Finance"],
    "finance": ["Quantitative Finance", "Product Management"],
    # Other technical
    "physics": ["Data Science/AI/ML", "Quantitative Finance", "Hardware Engineering"],
    "mechanical engineering": ["Hardware Engineering"],
}


def _normalize_text(text: str) -> str:
    """Normalize text for comparison."""
    return text.lower().strip() if text else ""


def score_major_match(user_majors: List[str], user_minors: List[str],
                      job_required_majors: List[str], job_category: Optional[str]) -> int:
    """
    Score major match (0-20 points).

    Args:
        user_majors: User's major(s)
        user_minors: User's minor(s)
        job_required_majors: Job's required majors (if any)
        job_category: Job category (Software Engineering, etc.)

    Returns:
        Score 0-20
    """
    # No job requirement = full points
    if not job_required_majors and not job_category:
        return 20

    user_majors_norm = [_normalize_text(m) for m in user_majors]
    user_minors_norm = [_normalize_text(m) for m in user_minors]

    # Check direct major match
    if job_required_majors:
        job_majors_norm = [_normalize_text(m) for m in job_required_majors]

        for user_major in user_majors_norm:
            for job_major in job_majors_norm:
                # Direct match or partial match
                if user_major in job_major or job_major in user_major:
                    return 20
                # "or equivalent" often means flexible
                if "equivalent" in job_major:
                    return 15

        # Check minor match
        for user_minor in user_minors_norm:
            for job_major in job_majors_norm:
                if user_minor in job_major or job_major in user_minor:
                    return 10

    # Check category relevance
    if job_category:
        job_cat_norm = _normalize_text(job_category)

        for user_major in user_majors_norm:
            if user_major in MAJOR_CATEGORY_MAP:
                relevant_categories = [c.lower() for c in MAJOR_CATEGORY_MAP[user_major]]
                if job_cat_norm in relevant_categories:
                    return 15

    # No direct match but job might still accept
    if job_required_majors:
        # Check if "or related" or "or equivalent" in requirements
        for req in job_required_majors:
            if "related" in req.lower() or "equivalent" in req.lower():
                return 10

    return 5  # Base score for attempting


def score_gpa_match(user_gpa: float, job_gpa_requirement: Optional[float]) -> int:
    """
    Score GPA match (0-10 points).

    Args:
        user_gpa: User's GPA
        job_gpa_requirement: Job's minimum GPA requirement

    Returns:
        Score 0-10
    """
    # No requirement = full points
    if job_gpa_requirement is None or job_gpa_requirement <= 0:
        return 10

    # No user GPA = partial points
    if user_gpa <= 0:
        return 5

    # Meets or exceeds
    if user_gpa >= job_gpa_requirement:
        return 10

    # Within 0.3
    if user_gpa >= job_gpa_requirement - 0.3:
        return 5

    return 0


def score_location_match(user_locations: List[str], user_work_model: str,
                         job_locations: List[str], job_is_remote: Optional[bool],
                         job_work_model: Optional[str]) -> int:
    """
    Score location match (0-10 points).

    Args:
        user_locations: User's preferred locations
        user_work_model: User's work model preference (Remote, Hybrid, On-site, Any)
        job_locations: Job's locations
        job_is_remote: Whether job is remote
        job_work_model: Job's work model

    Returns:
        Score 0-10
    """
    user_work_model_norm = _normalize_text(user_work_model)

    # User accepts any = full points
    if not user_locations or user_work_model_norm == "any":
        return 10

    # Remote job
    if job_is_remote or (job_work_model and "remote" in job_work_model.lower()):
        if user_work_model_norm in ["remote", "any", ""]:
            return 10
        # User prefers on-site but job is remote
        return 5

    # Check location match
    if job_locations and user_locations:
        user_locs_norm = [_normalize_text(loc) for loc in user_locations]
        job_locs_norm = [_normalize_text(loc) for loc in job_locations]

        for user_loc in user_locs_norm:
            for job_loc in job_locs_norm:
                # Direct match
                if user_loc in job_loc or job_loc in user_loc:
                    return 10

                # Check city aliases
                if user_loc in CITY_ALIASES:
                    aliases = CITY_ALIASES[user_loc]
                    if any(alias in job_loc for alias in aliases):
                        return 10

                # Check reverse aliases (job uses abbreviation)
                for abbrev, aliases in CITY_ALIASES.items():
                    if abbrev in job_loc and any(alias in user_loc for alias in aliases):
                        return 10
                    if any(alias in job_loc for alias in aliases) and abbrev in user_loc:
                        return 10

                # State/region partial match
                user_parts = user_loc.replace(",", " ").split()
                job_parts = job_loc.replace(",", " ").split()
                if any(up in job_parts for up in user_parts if len(up) > 1):
                    return 7

    # No location match
    return 0


def score_skills_match(user_skills: List[str],
                       job_required_skills: List[str],
                       job_preferred_skills: List[str]) -> int:
    """
    Score skills match (0-20 points).

    Args:
        user_skills: User's skills
        job_required_skills: Job's required skills
        job_preferred_skills: Job's preferred skills

    Returns:
        Score 0-20 (15 for required, 5 for preferred)
    """
    if not user_skills:
        return 10  # Can't assess, give partial credit

    user_skills_norm = set(_normalize_text(s) for s in user_skills)

    required_score = 0
    preferred_score = 0

    # Score required skills (up to 15 points)
    if job_required_skills:
        matches = 0
        for skill in job_required_skills:
            skill_norm = _normalize_text(skill)
            # Check for exact or partial match
            for user_skill in user_skills_norm:
                if user_skill in skill_norm or skill_norm in user_skill:
                    matches += 1
                    break
                # Also check individual words
                skill_words = set(skill_norm.split())
                user_words = set(user_skill.split())
                if skill_words & user_words:
                    matches += 0.5
                    break

        match_ratio = min(matches / len(job_required_skills), 1.0)
        required_score = int(match_ratio * 15)
    else:
        required_score = 15  # No requirements = full credit

    # Score preferred skills (up to 5 points)
    if job_preferred_skills:
        matches = 0
        for skill in job_preferred_skills:
            skill_norm = _normalize_text(skill)
            for user_skill in user_skills_norm:
                if user_skill in skill_norm or skill_norm in user_skill:
                    matches += 1
                    break

        match_ratio = min(matches / len(job_preferred_skills), 1.0)
        preferred_score = int(match_ratio * 5)
    else:
        preferred_score = 5  # No preferred = full credit

    return required_score + preferred_score


def score_company_match(user_target_companies: List[str], user_job_categories: List[str],
                        job_company: str, job_category: Optional[str]) -> int:
    """
    Score company preference match (0-30 points).

    Args:
        user_target_companies: User's target/dream companies
        user_job_categories: User's preferred job categories
        job_company: Job's company name
        job_category: Job's category (Software Engineering, etc.)

    Returns:
        Score 0-30
    """
    target_company_match = False
    category_match = False

    # Check target company match
    if user_target_companies and job_company:
        job_company_norm = _normalize_text(job_company)
        user_companies_norm = [_normalize_text(c) for c in user_target_companies]
        for target in user_companies_norm:
            if target in job_company_norm or job_company_norm in target:
                target_company_match = True
                break

    # Check job category match
    if job_category:
        job_cat_norm = _normalize_text(job_category)
        if not user_job_categories:
            # User has no category preference = category matches
            category_match = True
        else:
            user_cats_norm = [_normalize_text(c) for c in user_job_categories]
            for user_cat in user_cats_norm:
                if user_cat in job_cat_norm or job_cat_norm in user_cat:
                    category_match = True
                    break

    # Scoring logic
    if target_company_match and category_match:
        return 30  # Dream company + right category
    elif category_match:
        return 20  # Right category (or user has no target companies)
    elif target_company_match:
        return 20  # Dream company but different category
    else:
        return 0


def score_salary_match(user_min_hourly: float, job_salary_min: Optional[float],
                       job_salary_max: Optional[float], job_salary_period: Optional[str]) -> int:
    """
    Score salary match (0-10 points).

    Args:
        user_min_hourly: User's minimum hourly rate requirement
        job_salary_min: Job's minimum salary
        job_salary_max: Job's maximum salary
        job_salary_period: Salary period (hourly, monthly, yearly)

    Returns:
        Score 0-10
    """
    # No user preference = full points
    if user_min_hourly <= 0:
        return 10

    # No job salary info = partial points
    if job_salary_min is None and job_salary_max is None:
        return 5

    # Convert to hourly rate
    salary = job_salary_max or job_salary_min
    job_hourly = None

    if salary and job_salary_period:
        if job_salary_period == "hourly":
            job_hourly = salary
        elif job_salary_period == "yearly":
            job_hourly = salary / 2080  # 40 hrs/week * 52 weeks
        elif job_salary_period == "monthly":
            job_hourly = salary / 173  # ~2080/12

    if job_hourly is None:
        return 5

    # Score based on how salary compares to user minimum
    if job_hourly >= user_min_hourly:
        return 10
    elif job_hourly >= user_min_hourly * 0.8:
        return 5
    else:
        return 0


def calculate_fit_score(user: UserProfile, job: ExtractedJob) -> Tuple[int, List[str]]:
    """
    Calculate total fit score for a job (0-100).

    Args:
        user: User profile from config
        job: Extracted job data

    Returns:
        Tuple of (total_score, notes_list)
    """
    notes = []

    # Calculate individual scores
    scores = {
        "company": score_company_match(
            user.target_companies,
            user.job_categories,
            job.company,
            job.job_category
        ),
        "major": score_major_match(user.majors, user.minors, job.required_majors, job.job_category),
        "skills": score_skills_match(user.skills, job.required_skills, job.preferred_skills),
        "location": score_location_match(
            user.preferred_locations,
            user.work_model,
            job.locations,
            job.is_remote,
            job.work_model
        ),
        "salary": score_salary_match(
            user.min_salary_hourly,
            job.salary_min,
            job.salary_max,
            job.salary_period
        ),
        "gpa": score_gpa_match(user.gpa, job.gpa_requirement),
    }

    # Generate notes for mismatches
    if scores["major"] < 15 and job.required_majors:
        notes.append(f"Major: requires {', '.join(job.required_majors[:2])}")

    if scores["location"] < 5 and job.locations:
        notes.append(f"Location: {', '.join(job.locations[:2])}")

    if scores["salary"] == 0 and user.min_salary_hourly > 0:
        # Calculate job hourly for note
        salary = job.salary_max or job.salary_min
        if salary and job.salary_period:
            if job.salary_period == "hourly":
                notes.append(f"Salary: ${salary:.0f}/hr (min ${user.min_salary_hourly:.0f}/hr)")
            elif job.salary_period == "yearly":
                hourly = salary / 2080
                notes.append(f"Salary: ${hourly:.0f}/hr (min ${user.min_salary_hourly:.0f}/hr)")

    total = sum(scores.values())

    logger.debug(f"Fit score for {job.company} - {job.title}: {total}")
    logger.debug(f"  Breakdown: {scores}")
    if notes:
        logger.debug(f"  Notes: {notes}")

    return total, notes


def score_job(job: ExtractedJob, config: Optional[Config] = None) -> Tuple[int, List[str]]:
    """
    Convenience function to score a job using global config.

    Args:
        job: Extracted job data
        config: Optional config (uses global if not provided)

    Returns:
        Tuple of (fit_score, notes_list)
    """
    if config is None:
        config = get_config()

    return calculate_fit_score(config.user, job)

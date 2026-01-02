#!/usr/bin/env python3
"""
Unified test script for the job scraping pipeline.

Supports testing individual components or the full pipeline:
- GitHub parsing only (--parse-only)
- Scraping only (--scrape-only)
- Extraction only (--extract-only)
- Full pipeline with filtering and scoring

Usage:
    # From a single URL
    python test_e2e.py <url>                      # Full pipeline (dry run)
    python test_e2e.py <url> --save               # Full pipeline + add to Sheets
    python test_e2e.py <url> --scrape-only        # Stop after scraping
    python test_e2e.py <url> --extract-only       # Stop after AI extraction
    python test_e2e.py <url> --save-content <f>   # Save scraped content to file

    # From GitHub job lists
    python test_e2e.py --from-github              # Full pipeline, 1 job
    python test_e2e.py --from-github --count 5    # Full pipeline, 5 jobs
    python test_e2e.py --from-github --parse-only # Just show parsed jobs
    python test_e2e.py --from-github --scrape-only --save-content ./scraped/
"""

import argparse
import asyncio
import sys
import io
import logging
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_config
from src.scraper import PlaywrightScraper
from src.ai_extractor import AIExtractor
from src.filters import passes_hard_filters
from src.scoring import calculate_fit_score
from src.sheets import SheetsClient
from src.deduplication import normalize_url
from src.github_parser import GitHubParser

# Setup logging
_config = get_config()
_log_level = getattr(logging, _config.log_level, logging.INFO)

log_file = Path(__file__).parent.parent / "logs" / "e2e.log"
log_file.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=_log_level,
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def log_job_details(job):
    """Log extracted job fields."""
    logger.info(f"  Company: {job.company}")
    logger.info(f"  Title: {job.title}")
    logger.info(f"  Job Type: {job.job_type}")
    logger.info(f"  Category: {job.job_category}")
    logger.info(f"  Location: {job.locations}")
    logger.info(f"  Work Model: {job.work_model}")
    logger.info(f"  Season/Year: {job.season_year}")
    if job.salary_min or job.salary_max:
        logger.info(f"  Salary: ${job.salary_min or '?'}-${job.salary_max or '?'} {job.salary_period or ''}")
    if job.class_standing_requirement:
        logger.info(f"  Class Standing: {job.class_standing_requirement}")
    if job.graduation_timeline:
        logger.info(f"  Graduation Timeline: {job.graduation_timeline}")
    if job.work_authorization:
        logger.info(f"  Work Auth: {job.work_authorization}")
    if job.gpa_requirement:
        logger.info(f"  GPA Requirement: {job.gpa_requirement}")
    if job.required_majors:
        logger.info(f"  Required Majors: {job.required_majors}")
    if job.required_skills:
        logger.info(f"  Required Skills: {job.required_skills[:5]}{'...' if len(job.required_skills) > 5 else ''}")


def format_salary(job):
    """Format salary for display."""
    if not job.salary_min and not job.salary_max:
        return ""

    if job.salary_min and job.salary_max:
        salary = f"${job.salary_min:,.0f}-${job.salary_max:,.0f}"
    elif job.salary_max:
        salary = f"${job.salary_max:,.0f}"
    else:
        salary = f"${job.salary_min:,.0f}"

    if job.salary_period:
        salary += f"/{job.salary_period}"

    return salary


def format_locations(job):
    """Format locations for display."""
    if not job.locations:
        return ""

    locs = ", ".join(job.locations[:3])
    if len(job.locations) > 3:
        locs += f" (+{len(job.locations) - 3} more)"

    if job.is_remote:
        locs = f"Remote / {locs}" if locs else "Remote"

    return locs


def truncate(text: str, max_length: int = 2000) -> str:
    """Truncate text with ellipsis indicator."""
    if len(text) <= max_length:
        return text
    return text[:max_length] + f"\n\n... [truncated, {len(text) - max_length} more chars]"


def show_github_jobs(jobs):
    """Display parsed GitHub jobs."""
    # Group by source repo
    by_repo = {}
    for job in jobs:
        if job.source_repo not in by_repo:
            by_repo[job.source_repo] = []
        by_repo[job.source_repo].append(job)

    for repo, repo_jobs in by_repo.items():
        logger.info("")
        logger.info("=" * 70)
        logger.info(f"=== {repo} ({len(repo_jobs)} jobs) ===")
        logger.info("=" * 70)

        for job in repo_jobs:
            age_str = f"{job.age_days}d" if job.age_days < 999 else "unknown"
            logger.info(f"  Company: {job.company}")
            logger.info(f"  Role: {job.title}")
            logger.info(f"  Location: {job.location}")
            logger.info(f"  URL: {job.url}")
            logger.info(f"  Age: {age_str}")
            logger.info("")


async def run_pipeline(args):
    """Run the pipeline based on provided arguments."""
    config = get_config()

    logger.info("\n" + "=" * 70)
    logger.info("PIPELINE TEST")
    logger.info("=" * 70)

    # Determine job sources
    jobs_to_process = []

    if args.from_github:
        # Fetch from GitHub
        logger.info(f"\n[GITHUB PARSER]")
        logger.info(f"  Repos: {config.github_repos}")

        with GitHubParser(config) as parser:
            github_jobs = parser.fetch_all_jobs()

        logger.info(f"  Found {len(github_jobs)} jobs")

        if not github_jobs:
            logger.error("  ERROR: No jobs found from GitHub")
            return False

        # --parse-only: just show jobs and exit
        if args.parse_only:
            show_github_jobs(github_jobs)
            logger.info("=" * 70)
            logger.info(f"Total: {len(github_jobs)} jobs parsed")
            logger.info("=" * 70)
            return True

        # Select jobs to process
        jobs_to_process = [
            {"url": job.url, "company": job.company, "title": job.title, "source": job.source_repo}
            for job in github_jobs[:args.count]
        ]
        logger.info(f"  Processing {len(jobs_to_process)} job(s)")

    else:
        # Single URL mode
        if not args.url:
            logger.error("ERROR: Must provide <url> or use --from-github")
            return False

        jobs_to_process = [{"url": args.url, "company": None, "title": None, "source": "cli"}]

    # Process each job
    results = {"scraped": 0, "extracted": 0, "passed": 0, "added": 0}

    async with PlaywrightScraper(config) as scraper:
        extractor = AIExtractor(config) if not args.scrape_only else None
        sheets = SheetsClient(config) if args.save else None

        for i, job_info in enumerate(jobs_to_process):
            url = job_info["url"]

            logger.info(f"\n{'=' * 70}")
            if job_info["company"]:
                logger.info(f"[{i+1}/{len(jobs_to_process)}] {job_info['company']} - {job_info['title']}")
            else:
                logger.info(f"[{i+1}/{len(jobs_to_process)}] {url}")
            logger.info("=" * 70)

            # Pipeline-level retry: scrape + extract together
            # Retries with increasing render delay when extraction fails
            max_attempts = config.max_retries
            jobs = None
            final_url = None
            content = None
            was_blocked = False  # Track if we broke due to anti-scraping

            for attempt in range(1, max_attempts + 1):
                # Calculate render delay: increase on each retry
                render_delay = config.render_delay_seconds * attempt

                # Step 1: Scrape
                if attempt == 1:
                    logger.info(f"\n[1] SCRAPING")
                else:
                    logger.info(f"\n[1] SCRAPING (retry {attempt}/{max_attempts}, render_delay={render_delay}s)")
                logger.info(f"  URL: {url}")

                content, final_url, is_blocked = await scraper.fetch_page(url, render_delay=render_delay)

                if not content:
                    logger.error("  ERROR: Failed to scrape page")
                    continue  # Retry

                # Check for anti-scraping block - don't retry if detected
                if is_blocked:
                    logger.error("  ERROR: Anti-scraping protection detected (403/Forbidden)")
                    was_blocked = True
                    break  # Don't retry - won't help

                final_url = normalize_url(final_url)
                logger.info(f"  Final URL: {final_url}")
                logger.info(f"  Content: {len(content):,} chars")

                # --scrape-only: don't retry, just show result
                if args.scrape_only:
                    results["scraped"] += 1
                    break

                # Step 2: AI Extract
                logger.info(f"\n[2] AI EXTRACTION")

                try:
                    jobs = extractor.extract(content, source_url=final_url)
                except Exception as e:
                    logger.error(f"  ERROR: Extraction failed - {e}")
                    continue  # Retry

                if jobs:
                    logger.info(f"  Extracted {len(jobs)} job(s)")
                    results["scraped"] += 1
                    results["extracted"] += len(jobs)
                    break  # Success!

                logger.warning(f"  No jobs extracted (attempt {attempt}/{max_attempts})")

            # Handle scrape-only mode
            if args.scrape_only:
                if content:
                    logger.info(f"\n  [scrape-only mode - stopping here]")
                    logger.info("-" * 40)
                    logger.info(truncate(content))
                    logger.info("-" * 40)
                continue

            # Save content if requested (save last attempt's content)
            if args.save_content and content:
                save_path = Path(args.save_content)
                if not save_path.is_absolute():
                    save_path = Path(__file__).parent.parent / save_path

                # If it's a directory, generate filename
                if save_path.is_dir() or str(args.save_content).endswith("/"):
                    save_path.mkdir(parents=True, exist_ok=True)
                    if job_info["company"]:
                        safe_name = f"{job_info['company']}_{job_info['title']}"[:80]
                    else:
                        safe_name = url.replace("https://", "").replace("http://", "")[:80]
                    safe_name = safe_name.replace(" ", "_").replace("/", "_")
                    save_path = save_path / f"{safe_name}.txt"
                else:
                    save_path.parent.mkdir(parents=True, exist_ok=True)

                save_path.write_text(content, encoding="utf-8")
                logger.info(f"  Saved content to: {save_path}")

            # Check if extraction succeeded after all retries
            # Skip error message if we already logged anti-scraping error
            if not jobs and not was_blocked:
                logger.error(f"  ERROR: No jobs extracted after {max_attempts} attempts")
            if not jobs:
                continue

            # --extract-only: show extraction and stop
            if args.extract_only:
                for job in jobs:
                    logger.info(f"\n  EXTRACTED DATA:")
                    log_job_details(job)
                continue

            # Step 3: Filter and Score each extracted job
            for j, job in enumerate(jobs):
                if len(jobs) > 1:
                    logger.info(f"\n--- Position {j+1}/{len(jobs)} ---")

                log_job_details(job)

                # Filter
                logger.info(f"\n[3] HARD FILTERS")
                passed, reason = passes_hard_filters(config.user, job)

                if passed:
                    logger.info(f"  Result: PASS")
                    logger.info(f"  Reason: {reason}")
                    results["passed"] += 1
                else:
                    logger.info(f"  Result: FAIL")
                    logger.info(f"  Reason: {reason}")
                    continue

                # Score
                logger.info(f"\n[4] FIT SCORE")
                score, notes = calculate_fit_score(config.user, job)
                logger.info(f"  Score: {score}/100")
                if notes:
                    for note in notes:
                        logger.info(f"  Note: {note}")

                # Add to Sheets
                logger.info(f"\n[5] GOOGLE SHEETS")
                if args.save:
                    try:
                        job_data = {
                            "company": job.company,
                            "position": job.title,
                            "position_url": final_url,
                            "job_posting_date": job.posted_date or "",
                            "fit_score": score,
                            "salary": format_salary(job),
                            "job_type": job.job_type or "",
                            "work_model": job.work_model or "",
                            "location": format_locations(job),
                            "season_year": job.season_year or "",
                            "deadline": job.deadline or "",
                            "source": job_info["source"],
                            "notes": "; ".join(notes) if notes else "",
                        }

                        row_num = sheets.add_job(job_data)
                        logger.info(f"  Added to Sheets at row {row_num}")
                        results["added"] += 1

                    except Exception as e:
                        logger.error(f"  ERROR: Failed to add to Sheets - {e}")
                else:
                    logger.info("  Skipped (use --save to add to Sheets)")

    # Summary
    logger.info(f"\n{'=' * 70}")
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info(f"  Jobs scraped: {results['scraped']}")
    if not args.scrape_only:
        logger.info(f"  Jobs extracted: {results['extracted']}")
    if not args.scrape_only and not args.extract_only:
        logger.info(f"  Jobs passed filters: {results['passed']}")
        if args.save:
            logger.info(f"  Jobs added to Sheets: {results['added']}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Unified pipeline test script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test single URL (full pipeline, dry run)
  python test_e2e.py https://jobs.lever.co/company/job-id

  # Test single URL and add to Sheets
  python test_e2e.py https://jobs.lever.co/company/job-id --save

  # Test from GitHub (1 job by default)
  python test_e2e.py --from-github

  # Test 5 jobs from GitHub
  python test_e2e.py --from-github --count 5

  # Just show parsed GitHub jobs (no scraping)
  python test_e2e.py --from-github --parse-only

  # Test scraping only
  python test_e2e.py https://example.com/job --scrape-only

  # Save scraped content for later AI testing
  python test_e2e.py https://example.com/job --save-content ./scraped/
        """
    )

    # Input source
    parser.add_argument("url", nargs="?", help="Job posting URL to test")
    parser.add_argument("--from-github", action="store_true",
                        help="Fetch jobs from GitHub instead of URL")
    parser.add_argument("--count", "-c", type=int, default=None,
                        help="Number of jobs to process from GitHub (default: all)")

    # Pipeline control
    parser.add_argument("--parse-only", action="store_true",
                        help="Stop after GitHub parsing (show jobs only)")
    parser.add_argument("--scrape-only", action="store_true",
                        help="Stop after scraping (no AI extraction)")
    parser.add_argument("--extract-only", action="store_true",
                        help="Stop after AI extraction (no filtering/scoring)")

    # Output options
    parser.add_argument("--save", action="store_true",
                        help="Add qualifying jobs to Google Sheets")
    parser.add_argument("--save-content", metavar="PATH",
                        help="Save scraped content to file or directory")

    args = parser.parse_args()

    # Validation
    if not args.url and not args.from_github:
        parser.print_help()
        print("\nError: Must provide <url> or use --from-github")
        sys.exit(1)

    if args.parse_only and not args.from_github:
        print("Error: --parse-only requires --from-github")
        sys.exit(1)

    success = asyncio.run(run_pipeline(args))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

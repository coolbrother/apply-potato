#!/usr/bin/env python3
"""
Main job scraping pipeline for ApplyPotato.

Workflow:
1. Fetch job listings from GitHub repos
2. Check for duplicates against Google Sheets
3. Scrape new job pages with Playwright
4. Extract structured data with AI
5. Apply hard eligibility filters
6. Calculate fit scores
7. Add qualifying jobs to Google Sheets

Usage:
    python scrape_jobs.py              # Run once
    python scrape_jobs.py --scheduled  # Run on schedule (every N minutes)
    python scrape_jobs.py --limit 5    # Process max 5 new jobs
"""

import argparse
import asyncio
import logging
import time
from datetime import datetime
from typing import List, Optional

from apscheduler.schedulers.blocking import BlockingScheduler

from src.config import get_config, Config
from src.logging_config import setup_logging
from src.github_parser import GitHubParser, JobListing
from src.scraper import PlaywrightScraper
from src.ai_extractor import AIExtractor, ExtractedJob
from src.deduplication import DeduplicationChecker, get_dedup_checker, normalize_url
from src.filters import passes_hard_filters
from src.scoring import calculate_fit_score
from src.sheets import SheetsClient, get_sheets_client
from src.notifications import notify_dream_company_job, is_dream_company


logger = logging.getLogger(__name__)


class JobScraper:
    """
    Main job scraping pipeline.

    Orchestrates the flow from GitHub → Sheets.
    """

    def __init__(self, config: Optional[Config] = None):
        """
        Initialize the job scraper.

        Args:
            config: Optional config object. Uses global config if not provided.
        """
        self.config = config or get_config()
        self.github_parser = GitHubParser(self.config)
        self.ai_extractor = AIExtractor(self.config)
        self.dedup_checker = get_dedup_checker(self.config)
        self.sheets_client = get_sheets_client()

        # Log AI provider once at startup
        if self.config.ai_provider == "openai":
            model_name = self.config.openai_model
        else:
            model_name = self.config.gemini_model
        logger.info(f"AI Extractor: {self.config.ai_provider} ({model_name})")

        # Ensure headers and date formatting exist
        self.sheets_client.ensure_headers()

        # Stats
        self.stats = {
            "listings_found": 0,
            "duplicates_skipped": 0,
            "filtered_skipped": 0,
            "scrape_failures": 0,
            "extraction_failures": 0,
            "filtered_out": 0,
            "jobs_added": 0,
        }

    def close(self):
        """Close resources."""
        self.github_parser.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _format_salary(self, job: ExtractedJob) -> str:
        """Format salary information for display."""
        if not job.salary_min and not job.salary_max:
            return ""

        parts = []
        if job.currency:
            parts.append(job.currency)

        if job.salary_min and job.salary_max:
            if job.salary_min == job.salary_max:
                parts.append(f"{job.salary_min:,.0f}")
            else:
                parts.append(f"{job.salary_min:,.0f}-{job.salary_max:,.0f}")
        elif job.salary_min:
            parts.append(f"{job.salary_min:,.0f}+")
        elif job.salary_max:
            parts.append(f"up to {job.salary_max:,.0f}")

        if job.salary_period:
            parts.append(f"/{job.salary_period}")

        return " ".join(parts)

    def _format_locations(self, job: ExtractedJob) -> str:
        """Format locations for display."""
        if not job.locations:
            if job.is_remote:
                return "Remote"
            return ""

        locs = "; ".join(job.locations[:3])  # Max 3 locations
        if len(job.locations) > 3:
            locs += f" +{len(job.locations) - 3} more"

        if job.is_remote:
            locs = f"Remote / {locs}"

        return locs

    def _prepare_job_data(self, listing: JobListing, extracted: ExtractedJob,
                          fit_score: int, score_notes: list, final_url: str) -> dict:
        """Prepare job data dict for adding to Sheets."""
        return {
            "company": extracted.company or listing.company,
            "position": extracted.title or listing.title,
            "position_url": final_url,  # Use final URL after redirects for cross-source dedup
            "job_posting_date": extracted.posted_date or listing.date_posted or "",
            "fit_score": fit_score,
            "salary": self._format_salary(extracted),
            "job_type": extracted.job_type or "",
            "work_model": extracted.work_model or "",
            "location": self._format_locations(extracted),
            "season_year": extracted.season_year or "",
            "deadline": extracted.deadline or "",
            "source": listing.source_repo,
            "notes": "; ".join(score_notes) if score_notes else "",
        }

    async def _process_listing(self, listing: JobListing, scraper: PlaywrightScraper) -> Optional[bool]:
        """
        Process a single job listing.

        Args:
            listing: Job listing from GitHub
            scraper: Playwright scraper instance

        Returns:
            None if skipped (duplicate/previously filtered)
            True if job was added to Sheets
            False if job was processed but filtered out or extraction failed
        """
        # Normalize URL before scraping (e.g., strip /apply from Lever URLs)
        job_url = normalize_url(listing.url)
        logger.info("")  # Visual separator between jobs
        logger.info(f"Processing: {listing.company} - {listing.title}")

        # Pre-scrape check: skip if we've already processed this source URL
        # This avoids wasting scrape calls on URLs we've seen before
        if self.dedup_checker.is_seen_source(listing.url):
            logger.info(f"  Skipping: already processed this source URL")
            self.stats["duplicates_skipped"] += 1
            return None

        # Pipeline-level retry: scrape + extract together
        # Retries with increasing render delay when extraction fails
        extracted_jobs = None
        final_url = None
        max_attempts = self.config.max_retries

        for attempt in range(1, max_attempts + 1):
            # Calculate render delay: increase on each retry
            render_delay = self.config.render_delay_seconds * attempt

            # Scrape the job page
            if attempt > 1:
                logger.info(f"  Retry {attempt}/{max_attempts} with render_delay={render_delay}s")
            logger.debug(f"  Scraping: {job_url}")

            try:
                content, final_url, is_blocked = await scraper.fetch_page(job_url, render_delay=render_delay)
                if not content:
                    logger.warning(f"  Failed to scrape: {job_url}")
                    continue  # Retry
                if is_blocked:
                    logger.warning(f"  Skipping: site blocked scraping (403)")
                    self.stats["scrape_failures"] += 1
                    return False  # Counts as new job (processed but failed)
            except Exception as e:
                logger.error(f"  Scrape error: {e}")
                continue  # Retry

            # Normalize final URL (strip tracking params, etc.)
            final_url = normalize_url(final_url)

            # Check for duplicate using final URL (after redirects)
            # This enables cross-source dedup (GitHub, email alerts, etc.)
            if self.dedup_checker.job_exists(final_url):
                logger.info(f"  Skipping: duplicate (already in Sheets)")
                self.stats["duplicates_skipped"] += 1
                return None  # Skipped, doesn't count toward limit

            # Check if previously filtered (saves AI tokens)
            if self.dedup_checker.is_filtered(final_url):
                logger.info(f"  Skipping: previously filtered")
                self.stats["filtered_skipped"] += 1
                return None  # Skipped, doesn't count toward limit

            # Extract job data with AI
            logger.debug(f"  Extracting with AI...")
            try:
                extracted_jobs = self.ai_extractor.extract(content, source_url=final_url)
                if extracted_jobs:
                    break  # Success! Exit retry loop
                logger.warning(f"  AI extraction returned no jobs (attempt {attempt}/{max_attempts})")
            except Exception as e:
                logger.error(f"  Extraction error: {e}")

        # All retries exhausted
        if not extracted_jobs:
            logger.warning(f"  Failed to extract jobs after {max_attempts} attempts")
            self.stats["extraction_failures"] += 1
            return False

        # Process each extracted job (some postings have multiple positions)
        added_any = False
        for extracted in extracted_jobs:
            # Log extracted data for debugging
            logger.debug(f"  Extracted job: company={extracted.company}, title={extracted.title}")
            logger.debug(f"    job_type={extracted.job_type}, season_year={extracted.season_year}")
            logger.debug(f"    class_standing={extracted.class_standing_requirement}, work_auth={extracted.work_authorization}")

            # Apply hard filters
            passed, reason = passes_hard_filters(self.config.user, extracted)
            if not passed:
                logger.warning(f"  Filtered out: {extracted.company} - {reason}")
                self.stats["filtered_out"] += 1
                # Mark as filtered to skip on future runs
                self.dedup_checker.mark_as_filtered(final_url)
                continue
            logger.debug(f"  Passed filters: {reason}")

            # Calculate fit score
            fit_score, score_notes = calculate_fit_score(self.config.user, extracted)
            logger.debug(f"  Fit score: {fit_score}")
            if score_notes:
                logger.debug(f"  Score notes: {score_notes}")

            # Prepare and add to Sheets
            job_data = self._prepare_job_data(listing, extracted, fit_score, score_notes, final_url)

            try:
                row_num = self.sheets_client.add_job(job_data)
                logger.info(f"  Added to Sheets (row {row_num}): {extracted.company} - {extracted.title} (score: {fit_score})")
                self.stats["jobs_added"] += 1
                added_any = True

                # Add to dedup cache
                url = job_data["position_url"]
                self.dedup_checker.add_to_cache(url)

                # Send Discord notification if dream company
                if self.config.discord.enabled and extracted.company:
                    if is_dream_company(
                        extracted.company,
                        self.config.user.target_companies,
                        self.config.discord.dream_company_match_threshold
                    ):
                        logger.info(f"  Dream company detected! Sending Discord notification...")
                        try:
                            notify_dream_company_job(extracted.company, extracted.title, final_url)
                        except Exception as discord_error:
                            logger.warning(f"  Failed to send Discord notification: {discord_error}")

            except Exception as e:
                logger.error(f"  Failed to add to Sheets: {e}")

        # Mark source URL as seen so we skip it on future runs
        self.dedup_checker.mark_source_seen(listing.url)

        return added_any

    async def run(self, limit: Optional[int] = None) -> dict:
        """
        Run the job scraping pipeline.

        Args:
            limit: Optional maximum number of new jobs to process

        Returns:
            Dict with statistics about the run
        """
        start_time = time.time()
        logger.info("=" * 60)
        logger.info("Starting job scraping pipeline")
        logger.info("=" * 60)

        # Reset stats
        self.stats = {k: 0 for k in self.stats}

        # Refresh dedup cache from Sheets
        logger.info("Refreshing deduplication cache from Google Sheets...")
        self.dedup_checker.refresh_cache()

        # Fetch job listings from GitHub repos
        repo_list = [f"{r.owner_repo}@{r.branch}" for r in self.config.github_repos]
        logger.info(f"Fetching jobs from GitHub repos: {repo_list}")
        try:
            all_listings = self.github_parser.fetch_all_jobs()
            logger.info(f"Found {len(all_listings)} total listings")
        except Exception as e:
            logger.error(f"Failed to fetch jobs from GitHub: {e}")
            all_listings = []

        self.stats["listings_found"] = len(all_listings)
        logger.info(f"Total listings found: {len(all_listings)}")

        # Filter by job age if configured
        if self.config.job_age_limit_days > 0:
            before_count = len(all_listings)
            all_listings = [
                listing for listing in all_listings
                if listing.age_days <= self.config.job_age_limit_days
            ]
            filtered = before_count - len(all_listings)
            if filtered > 0:
                logger.info(f"Filtered {filtered} listings older than {self.config.job_age_limit_days} days")

        # Process listings with Playwright
        # Limit applies to NEW jobs only (not duplicates or previously filtered)
        if all_listings:
            new_jobs_processed = 0
            async with PlaywrightScraper(self.config) as scraper:
                for listing in all_listings:
                    try:
                        result = await self._process_listing(listing, scraper)
                        # result is None for skipped (dup/filtered), True/False for processed
                        if result is not None:
                            new_jobs_processed += 1
                            if limit and new_jobs_processed >= limit:
                                logger.info(f"Reached limit of {limit} new jobs")
                                break
                    except Exception as e:
                        logger.error(f"Error processing {listing.url}: {e}")

                    # Small delay between requests
                    await asyncio.sleep(1)

        # Log summary
        elapsed = time.time() - start_time
        logger.info("")  # Visual separator before summary
        logger.info("=" * 60)
        logger.info("Pipeline complete!")
        logger.info(f"  Time elapsed: {elapsed:.1f}s")
        logger.info(f"  Listings found: {self.stats['listings_found']}")
        logger.info(f"  Duplicates skipped: {self.stats['duplicates_skipped']}")
        logger.info(f"  Filtered skipped: {self.stats['filtered_skipped']}")
        logger.info(f"  Scrape failures: {self.stats['scrape_failures']}")
        logger.info(f"  Extraction failures: {self.stats['extraction_failures']}")
        logger.info(f"  Filtered out: {self.stats['filtered_out']}")
        logger.info(f"  Jobs added: {self.stats['jobs_added']}")
        logger.info("=" * 60)

        return self.stats


def run_once(limit: Optional[int] = None):
    """Run the pipeline once."""
    config = get_config()
    setup_logging("scrape", config, console=True)

    with JobScraper(config) as scraper:
        stats = asyncio.run(scraper.run(limit=limit))

    return stats


def run_scheduled():
    """Run the pipeline on a schedule."""
    config = get_config()
    setup_logging("scrape", config, console=True)

    interval = config.scrape_interval_minutes
    logger.info(f"Starting scheduled scraper (every {interval} minutes)")

    scheduler = BlockingScheduler()

    def job():
        try:
            with JobScraper(config) as scraper:
                asyncio.run(scraper.run())
        except Exception as e:
            logger.error(f"Scheduled job failed: {e}")

    # Run immediately on start
    job()

    # Schedule recurring runs
    scheduler.add_job(job, 'interval', minutes=interval)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


def clear_filtered():
    """Clear the filtered jobs cache."""
    config = get_config()
    setup_logging("scrape", config, console=True)

    checker = get_dedup_checker(config)
    checker.clear_filtered_jobs()
    print("Filtered jobs cache cleared.")


def clear_seen():
    """Clear the seen sources cache."""
    config = get_config()
    setup_logging("scrape", config, console=True)

    checker = get_dedup_checker(config)
    checker.clear_seen_sources()
    print("Seen sources cache cleared.")


def main():
    parser = argparse.ArgumentParser(description="ApplyPotato Job Scraper")
    parser.add_argument("--scheduled", action="store_true", help="Run on schedule")
    parser.add_argument("--limit", type=int, help="Max jobs to process")
    parser.add_argument("--clear-filtered", action="store_true",
                        help="Clear filtered jobs cache (use when profile changes)")
    parser.add_argument("--clear-seen", action="store_true",
                        help="Clear seen sources cache (re-process all URLs)")
    args = parser.parse_args()

    if args.clear_filtered:
        clear_filtered()
    elif args.clear_seen:
        clear_seen()
    elif args.scheduled:
        run_scheduled()
    else:
        run_once(limit=args.limit)


if __name__ == "__main__":
    main()

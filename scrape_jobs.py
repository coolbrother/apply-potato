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
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from apscheduler.schedulers.blocking import BlockingScheduler

from src.config import (
    get_config,
    Config,
    ELIGIBILITY_MODES,
    ELIGIBILITY_MODE_AI,
    ELIGIBILITY_MODE_CODE,
    ELIGIBILITY_MODE_SHADOW,
)
from src.logging_config import setup_logging
from src.github_parser import GitHubParser, JobListing
from src.newsletter_parser import NewsletterParser
from src.sheets_job_list_parser import SheetsJobListParser
from src.scraper import PlaywrightScraper
from src.ai_extractor import AIExtractor, ExtractedJob
from src.deduplication import DeduplicationChecker, get_dedup_checker, normalize_url
from src.filters import passes_hard_filters
from src.eligibility import EligibilityUnavailable, get_eligibility_judge
from src.eligibility_log import record_disagreement
from src.scoring import calculate_fit_score
from src.sheets import SheetsClient, get_sheets_client
from src.notifications import notify_dream_company_job, is_dream_company
from src.project_fit import run_project_fit_skill


logger = logging.getLogger(__name__)


# Phrases that indicate a job posting is closed/removed (404 or expired listing).
# Matched against scraped page text when the content is short (i.e. not a real JD).
CLOSED_JOB_INDICATORS = (
    "job you're looking for is now closed",
    "job you are looking for is now closed",
    "this job is no longer available",
    "this position is no longer available",
    "no longer accepting applications",
    "position has been filled",
    "posting is no longer active",
    "job posting has expired",
    "404",
    "page not found",
)


def _is_closed_job(content: str) -> bool:
    """Return True if the scraped page looks like a closed/removed job posting.

    Only flags short pages (real job descriptions are long); a long page that merely
    mentions "404" somewhere is not treated as closed.
    """
    if not content or len(content) > 2000:
        return False
    lowered = content.lower()
    return any(phrase in lowered for phrase in CLOSED_JOB_INDICATORS)


class JobScraper:
    """
    Main job scraping pipeline.

    Orchestrates the flow from GitHub → Sheets.
    """

    def __init__(self, config: Optional[Config] = None, eligibility_mode: Optional[str] = None):
        """
        Initialize the job scraper.

        Args:
            config: Optional config object. Uses global config if not provided.
            eligibility_mode: Override ELIGIBILITY_MODE for this run, so a mode can be
                tried against live postings without editing .env or restarting the
                service. An unrecognized value falls back to the configured mode.
        """
        self.config = config or get_config()

        self.eligibility_mode = self.config.eligibility_mode
        if eligibility_mode:
            candidate = eligibility_mode.strip().lower()
            if candidate in ELIGIBILITY_MODES:
                self.eligibility_mode = candidate
            else:
                logger.warning(
                    f"Unknown --eligibility-mode {eligibility_mode!r}; "
                    f"using {self.eligibility_mode!r} from config"
                )
        if self.eligibility_mode != ELIGIBILITY_MODE_CODE:
            logger.info(f"Eligibility mode: {self.eligibility_mode}")
        self.github_parser = GitHubParser(self.config)
        self.ai_extractor = AIExtractor(self.config)
        self.dedup_checker = get_dedup_checker(self.config)
        self.sheets_client = get_sheets_client()
        self.job_list_parser: Optional[SheetsJobListParser] = (
            SheetsJobListParser(self.config) if self.config.job_list_sheet_id else None
        )

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
            "duplicate_postings": 0,
            "applied_company_skipped": 0,
            "filtered_skipped": 0,
            "scrape_failures": 0,
            "extraction_failures": 0,
            "filtered_out": 0,
            "jobs_added": 0,
            "eligibility_disagreements": 0,
            "eligibility_unavailable": 0,
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

    def _log_filtered(self, company: str, title: str, category: str) -> None:
        """Append a filtered-out event to data/filter_log.json for daily summary reporting."""
        log_path = Path(__file__).parent / "data" / "filter_log.json"
        try:
            entries = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
        except (json.JSONDecodeError, OSError):
            entries = []
        entries.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "category": category,
            "company": company or "",
            "title": title or "",
        })
        log_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

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

    def _judge_eligibility(self, content: Optional[str]):
        """
        Run the AI eligibility pass, or return None when the mode does not need it.

        None means "no judgment" everywhere downstream: filters.py falls back to the
        code path. Wrapped so a failure in the new path can never cost a job that the
        old path would have accepted.
        """
        if self.eligibility_mode == ELIGIBILITY_MODE_CODE or not content:
            return None
        try:
            return get_eligibility_judge(self.config).judge(content, self.config.user)
        except Exception as e:
            logger.warning(f"  Eligibility judgment failed, falling back to filters: {e}")
            return None

    def _record_eligibility_disagreement(
        self,
        extracted: ExtractedJob,
        final_url: str,
        judgment,
        passed: bool,
        reason: str,
        category: str,
    ) -> None:
        """
        In shadow mode, log the postings the two paths decide differently.

        Only disagreements are written. A log where the interesting rows are a
        fraction of the lines does not get read, and reading it is the entire point.
        """
        if self.eligibility_mode != ELIGIBILITY_MODE_SHADOW or judgment is None:
            return
        if not judgment.usable or judgment.eligible == passed:
            return
        try:
            written = record_disagreement(
                self.config.data_dir,
                url=final_url,
                company=extracted.company or "",
                title=extracted.title or "",
                code_passed=passed,
                code_reason=reason,
                code_category=category,
                judgment=judgment,
            )
            if written:
                verdicts = (
                    f"code={'pass' if passed else 'reject'} "
                    f"ai={'pass' if judgment.eligible else 'reject'}"
                )
                logger.info(
                    f"  Eligibility disagreement ({verdicts}): "
                    f"{extracted.company} - {extracted.title}"
                )
                self.stats["eligibility_disagreements"] += 1
        except Exception as e:
            logger.warning(f"  Could not record eligibility disagreement: {e}")

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
        content = None
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
                    return "Blocked (scrape failed)"
                if _is_closed_job(content):
                    logger.warning(f"  Skipping: job posting is closed/removed (404)")
                    self.stats["scrape_failures"] += 1
                    self.dedup_checker.mark_source_seen(listing.url)
                    return "Closed: Job posting closed or removed (404)"
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
                # Mark source as seen so we don't re-scrape next run
                self.dedup_checker.mark_source_seen(listing.url)
                return None  # Skipped, doesn't count toward limit

            # Check if previously filtered (saves AI tokens)
            if self.dedup_checker.is_filtered(final_url):
                logger.info(f"  Skipping: previously filtered")
                self.stats["filtered_skipped"] += 1
                # Mark source as seen so we don't re-scrape next run
                self.dedup_checker.mark_source_seen(listing.url)
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
            # Save extraction failure for review
            self.dedup_checker.save_extraction_failure(
                url=final_url or job_url,
                company=listing.company,
                title=listing.title,
                content=content or "",
                reason="AI extraction returned no valid jobs"
            )
            return "Failed (extraction error)"

        # Judge eligibility once per page, before looping over its positions. Skipped
        # entirely in "code" mode, so the default costs nothing.
        judgment = self._judge_eligibility(content)

        # Process each extracted job (some postings have multiple positions)
        added_any = False
        filter_reason = None
        for extracted in extracted_jobs:
            # Log extracted data for debugging
            logger.debug(f"  Extracted job: company={extracted.company}, title={extracted.title}")
            logger.debug(f"    job_type={extracted.job_type}, season_year={extracted.season_year}")
            logger.debug(f"    class_standing={extracted.class_standing_requirement}, work_auth={extracted.work_authorization}")

            # Apply hard filters. The judgment is computed once for the page and
            # shared across every position on it — a multi-position posting states its
            # eligibility once, so judging each position separately would pay for the
            # same answer repeatedly.
            try:
                passed, reason, category = passes_hard_filters(
                    self.config.user, extracted, judgment=judgment, mode=self.eligibility_mode
                )
            except EligibilityUnavailable as e:
                # No verdict, so no decision. Returning here skips mark_as_filtered and
                # mark_source_seen, leaving the posting to be picked up again rather than
                # cached as rejected on the strength of an outage.
                logger.warning(
                    f"  Eligibility unavailable for {extracted.company} - {e}; "
                    f"leaving undecided for a later run"
                )
                self.stats["eligibility_unavailable"] += 1
                return "Failed (eligibility unavailable)"
            self._record_eligibility_disagreement(
                extracted, final_url, judgment, passed, reason, category
            )
            if not passed:
                logger.warning(f"  Filtered out: {extracted.company} - {reason}")
                self.stats["filtered_out"] += 1
                self._log_filtered(extracted.company, extracted.title, category)
                filter_reason = reason
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

            # The same seat can reach the sheet under several URLs — GDIT posted one
            # Praxis internship four times under four requisition ids, giving four rows
            # and an ambiguous match for every Praxis email afterwards. URL dedup cannot
            # see that; the job's own attributes can.
            # An application to this company is already in flight. Identity dedup
            # cannot catch these: American Express relists one Campus Undergraduate
            # programme per location and Booz Allen separates its 2027 Summer Games
            # roles by a comma, so company, position, location and term never line up.
            # A company whose applications have all been rejected or ghosted is not
            # skipped — that seat is open again.
            if self.config.skip_applied_companies:
                applied_as = self.dedup_checker.company_has_live_application(
                    job_data["company"]
                )
                if applied_as:
                    logger.info(
                        f"  Skipping: application already in flight at {applied_as} — "
                        f"{job_data['company']} - {job_data['position']}"
                    )
                    self.stats["applied_company_skipped"] += 1
                    continue

            if self.dedup_checker.job_exists_by_identity(
                job_data["company"], job_data["position"],
                job_data["location"], job_data["season_year"],
            ):
                logger.info(
                    f"  Skipping: same posting already on the sheet — "
                    f"{job_data['company']} - {job_data['position']} "
                    f"({job_data['location'] or 'no location'}, "
                    f"{job_data['season_year'] or 'no term'})"
                )
                self.stats["duplicate_postings"] += 1
                continue

            try:
                row_num = self.sheets_client.add_job(job_data)
                logger.info(f"  Added to Sheets (row {row_num}): {extracted.company} - {extracted.title} (score: {fit_score})")
                self.stats["jobs_added"] += 1
                added_any = True

                # Add to dedup cache — both keys, so a second requisition for the same
                # seat is caught within this run and not only on the next one.
                url = job_data["position_url"]
                self.dedup_checker.add_to_cache(url)
                self.dedup_checker.add_identity_to_cache(
                    job_data["company"], job_data["position"],
                    job_data["location"], job_data["season_year"],
                )

                # Dream Company = in user's named list OR meets salary threshold
                is_dream = extracted.company and is_dream_company(
                    extracted.company,
                    self.config.user.target_companies,
                    salary_min=extracted.salary_min,
                    salary_max=extracted.salary_max,
                    salary_period=extracted.salary_period,
                    min_salary_annual=self.config.discord.dream_company_min_salary_annual,
                    min_salary_hourly=self.config.discord.dream_company_min_salary_hourly,
                )

                # Send Discord notification if dream company
                if self.config.discord.enabled and is_dream:
                    logger.info(f"  Dream company detected! Sending Discord notification...")
                    try:
                        notify_dream_company_job(extracted.company, extracted.title, final_url)
                    except Exception as discord_error:
                        logger.warning(f"  Failed to send Discord notification: {discord_error}")

                # Update project-fit report for dream jobs
                if is_dream:
                    try:
                        run_project_fit_skill(final_url, "apply-potato", Path(__file__).parent, page_content=content)
                    except Exception as e:
                        logger.warning(f"  project-fit skill failed: {e}")

                # Phase 1: detect requirements and save job description
                needs_resume = False
                needs_cover_letter = False
                if self.config.auto_apply.detect_requirements:
                    logger.info(f"  Detecting application requirements...")
                    try:
                        from src.auto_apply import AutoApplyOrchestrator
                        orchestrator = AutoApplyOrchestrator(self.config)
                        needs_resume, needs_cover_letter = await orchestrator.detect_only(
                            extracted=extracted,
                            job_url=final_url,
                            page_content=content,
                            scraper=scraper,
                        )
                        logger.info(f"  Requirements: resume={needs_resume}, cover_letter={needs_cover_letter}")
                    except Exception as e:
                        logger.warning(f"  Requirement detection failed (non-fatal): {e}")

                # Update Sheets with Dream / Resume / Cover Letter columns
                try:
                    self.sheets_client.update_job(row_num, {
                        "dream": "Yes" if is_dream else "No",
                        "resume_needed": "Yes" if needs_resume else "No",
                        "cover_letter_needed": "Yes" if needs_cover_letter else "No",
                    })
                except Exception as e:
                    logger.warning(f"  Failed to update Resume/CL columns: {e}")

                # Save job description to Resume repo folder
                try:
                    from src.job_desc import save_job_description, commit_and_push_job_folder
                    import re as _re
                    company_safe = _re.sub(r'[^\w\s-]', '', extracted.company or "Company").strip()
                    company_safe = _re.sub(r'[\s]+', '_', company_safe)[:50]
                    stem = f"{row_num}_{company_safe}"
                    md_path = save_job_description(
                        row_num=row_num,
                        company=extracted.company or "Company",
                        page_content=content,
                        extracted=extracted,
                        base_dir=self.config.job_desc_output_dir,
                        project_root=self.config.base_dir,
                    )
                    if md_path:
                        commit_and_push_job_folder(
                            folder=md_path.parent,
                            repo_dir=self.config.job_desc_output_dir,
                            stem=stem,
                        )
                except Exception as e:
                    logger.warning(f"  Job description save failed (non-fatal): {e}")

            except Exception as e:
                logger.error(f"  Failed to add to Sheets: {e}")

        # Mark source URL as seen so we skip it on future runs
        self.dedup_checker.mark_source_seen(listing.url)

        if added_any:
            return True
        if filter_reason:
            return f"Filtered: {filter_reason}"
        return False

    def _build_sources(self, only: Optional[List[str]] = None) -> List[tuple]:
        """
        Return a list of (name, fetch_callable) pairs for enabled sources.

        Args:
            only: If provided, restrict to these source names ("github", "newsletter", "sheets").
                  Defaults to all enabled sources.
        """
        candidates = []

        # Job List first, deliberately. run() processes all_listings in order, so
        # whatever a source contributes last waits behind every listing before it.
        # These URLs were pasted in by hand — they are the ones the user actually
        # wants — while the repo sources are a bulk feed. Ordering github first put
        # 4 hand-picked postings behind 335 never-seen repo listings after the
        # 2026-08-30 outage, two of them with application mail already sitting in
        # needs_review waiting for a row that had not been created yet.
        if self.job_list_parser:
            candidates.append((
                "sheets",
                lambda: self.job_list_parser.fetch_all_jobs(),
                f"sheet: {self.config.job_list_sheet_id}",
            ))

        repo_list = [f"{r.owner_repo}@{r.branch}" for r in self.config.github_repos]
        candidates.append((
            "github",
            lambda: self.github_parser.fetch_all_jobs(),
            f"repos: {repo_list}",
        ))

        if self.config.newsletter_enabled and self.config.newsletter_sources:
            names = [s.name for s in self.config.newsletter_sources]
            candidates.append((
                "newsletter",
                lambda: NewsletterParser(self.config).fetch_all_jobs(),
                f"sources: {names}",
            ))

        if only is not None:
            candidates = [(name, fn, detail) for name, fn, detail in candidates if name in only]

        return candidates

    async def run(self, limit: Optional[int] = None, only: Optional[List[str]] = None) -> dict:
        """
        Run the job scraping pipeline.

        Args:
            limit: Optional maximum number of new jobs to process
            only: If provided, only run these sources ("github", "newsletter", "sheets").
                  Defaults to all enabled sources.

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

        # Fetch job listings from all active sources
        all_listings = []
        for source_name, fetch, detail in self._build_sources(only):
            logger.info("")
            logger.info(f"Fetching jobs from source: {source_name} ({detail})")
            try:
                listings = fetch()
                logger.info(f"  Found {len(listings)} listings")
                all_listings.extend(listings)
            except Exception as e:
                logger.error(f"  Failed to fetch from {source_name}: {e}")

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
                    if limit is not None and new_jobs_processed >= limit:
                        logger.info(f"Reached limit of {limit} new jobs")
                        break
                    hit_limit = False
                    try:
                        result = await self._process_listing(listing, scraper)
                        # result is None for skipped (dup/filtered), True/False/str for processed
                        if result is not None:
                            new_jobs_processed += 1
                    except Exception as e:
                        logger.error(f"Error processing {listing.url}: {e}")
                        result = None

                    # Write result back to job list sheet if this listing came from there
                    if self.job_list_parser and listing.source_repo == "sheets-list":
                        if result is None:
                            self.job_list_parser.mark_row(listing.url, "Already Processed")
                        elif result is True:
                            self.job_list_parser.mark_row(listing.url, "Done")
                        elif isinstance(result, str) and result.startswith("Filtered: "):
                            self.job_list_parser.mark_row(listing.url, "Filtered out", notes=result[len("Filtered: "):])
                        elif isinstance(result, str) and result.startswith("Closed: "):
                            self.job_list_parser.mark_row(listing.url, "Closed", notes=result[len("Closed: "):])
                        elif result is False:
                            self.job_list_parser.mark_row(listing.url, "Filtered out")
                        else:  # failure string (blocked, extraction error, etc.)
                            self.job_list_parser.mark_row(listing.url, "Failed", notes=result)

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
        logger.info(f"  Same posting, different URL: {self.stats['duplicate_postings']}")
        logger.info(f"  Company already applied to: {self.stats['applied_company_skipped']}")
        logger.info(f"  Filtered skipped: {self.stats['filtered_skipped']}")
        logger.info(f"  Scrape failures: {self.stats['scrape_failures']}")
        logger.info(f"  Extraction failures: {self.stats['extraction_failures']}")
        logger.info(f"  Filtered out: {self.stats['filtered_out']}")
        if self.eligibility_mode == ELIGIBILITY_MODE_SHADOW:
            logger.info(f"  Eligibility disagreements: {self.stats['eligibility_disagreements']}")
        if self.stats["eligibility_unavailable"]:
            logger.warning(
                f"  Eligibility unavailable (left undecided, will retry): "
                f"{self.stats['eligibility_unavailable']}"
            )
        logger.info(f"  Jobs added: {self.stats['jobs_added']}")
        logger.info("=" * 60)

        return self.stats


def run_once(limit: Optional[int] = None, only: Optional[List[str]] = None,
             with_docs: bool = False, eligibility_mode: Optional[str] = None):
    """Run Phase 1. If with_docs=True, also run Phase 2 afterwards."""
    config = get_config()
    setup_logging("scrape", config, console=True)

    with JobScraper(config, eligibility_mode=eligibility_mode) as scraper:
        stats = asyncio.run(scraper.run(limit=limit, only=only))

    if with_docs:
        logger.info("")
        logger.info("=" * 60)
        logger.info("Running Phase 2 — document generation")
        logger.info("=" * 60)
        from generate_docs import run_all
        run_all(config)

    return stats


async def _run_single_url(url: str, eligibility_mode: Optional[str] = None):
    """Run Phase 1 against a single job URL (for testing). Returns the result."""
    from src.scraper import PlaywrightScraper

    config = get_config()
    listing = JobListing(
        company="Unknown",
        title="Unknown",
        location="",
        url=url,
        date_posted="",
        source_repo="--url",
        age_days=0,
    )

    with JobScraper(config, eligibility_mode=eligibility_mode) as job_scraper:
        async with PlaywrightScraper(config) as scraper:
            result = await job_scraper._process_listing(listing, scraper)

    logger.info(f"Result: {result}")
    return result


def run_single_url(url: str, with_docs: bool = False, eligibility_mode: Optional[str] = None):
    config = get_config()
    setup_logging("scrape", config, console=True)
    asyncio.run(_run_single_url(url, eligibility_mode=eligibility_mode))

    if with_docs:
        logger.info("")
        logger.info("=" * 60)
        logger.info("Running Phase 2 — document generation")
        logger.info("=" * 60)
        from generate_docs import run_all
        run_all(config)


def run_scheduled(eligibility_mode: Optional[str] = None):
    """Run the pipeline on a schedule."""
    config = get_config()
    setup_logging("scrape", config, console=True)

    interval = config.scrape_interval_minutes
    logger.info(f"Starting scheduled scraper (every {interval} minutes)")

    scheduler = BlockingScheduler()

    def job():
        try:
            with JobScraper(config, eligibility_mode=eligibility_mode) as scraper:
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
    parser.add_argument("--only", type=str, metavar="SOURCES",
                        help="Comma-separated sources to run: github, newsletter, sheets (default: all enabled)")
    parser.add_argument("--clear-filtered", action="store_true",
                        help="Clear filtered jobs cache (use when profile changes)")
    parser.add_argument("--clear-seen", action="store_true",
                        help="Clear seen sources cache (re-process all URLs)")
    parser.add_argument("--url", type=str,
                        help="Run the full pipeline against a single job URL (for testing)")
    parser.add_argument("--with-docs", action="store_true",
                        help="Also run Phase 2 (doc generation) after Phase 1 completes")
    parser.add_argument("--eligibility-mode", type=str, choices=ELIGIBILITY_MODES,
                        help="Override ELIGIBILITY_MODE for this run: 'code' (filters.py "
                             "alone), 'shadow' (both run, filters.py decides, "
                             "disagreements logged) or 'ai' (the eligibility pass decides "
                             "class standing, graduation and work auth)")
    args = parser.parse_args()

    if args.clear_filtered:
        clear_filtered()
    elif args.clear_seen:
        clear_seen()
    elif args.scheduled:
        run_scheduled(eligibility_mode=args.eligibility_mode)
    elif args.url:
        run_single_url(args.url, with_docs=args.with_docs,
                       eligibility_mode=args.eligibility_mode)
    else:
        only = [s.strip() for s in args.only.split(",")] if args.only else None
        run_once(limit=args.limit, only=only, with_docs=args.with_docs,
                 eligibility_mode=args.eligibility_mode)


if __name__ == "__main__":
    main()

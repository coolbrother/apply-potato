#!/usr/bin/env python3
"""
Gmail status tracking for ApplyPotato.

Monitors Gmail for job application status updates and updates Google Sheets.

Workflow:
1. Fetch recent emails from Gmail (with privacy filters)
2. Classify emails with AI (confirmation, OA, interview, offer, rejection)
3. Match emails to existing jobs by company name
4. Update job status in Google Sheets

Usage:
    python check_gmail.py              # Run once
    python check_gmail.py --scheduled  # Run on schedule (every N minutes)
"""

import argparse
import logging
import time
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler

from src.config import get_config, Config
from src.logging_config import setup_logging
from src.gmail import GmailClient, get_gmail_client, EmailMessage
from src.email_filters import apply_privacy_filters
from src.email_classifier import EmailClassifier, get_classifier, EmailClassification
from src.sheets import SheetsClient, get_sheets_client, JobRow, normalize_date
from src.notifications import is_dream_company, notify_status_change


logger = logging.getLogger(__name__)


# Mapping of email categories to job status values
CATEGORY_TO_STATUS = {
    "confirmation": "Applied",
    "oa": "OA",
    "phone": "Phone",
    "technical": "Technical",
    "offer": "Offer",
    "rejection": "Rejected",
}

# Mapping of email categories to date columns
CATEGORY_TO_DATE_COLUMN = {
    "confirmation": "application_date",
    "oa": "oa_date",
    "phone": "phone_date",
    "technical": "tech_date",
}


class GmailChecker:
    """
    Main Gmail checking pipeline.

    Orchestrates the flow from Gmail → Classification → Sheets update.
    """

    def __init__(self, config: Optional[Config] = None, reprocess: bool = False):
        """
        Initialize the Gmail checker.

        Args:
            config: Optional config object. Uses global config if not provided.
            reprocess: If True, reprocess all emails ignoring processed_emails.json
        """
        self.config = config or get_config()
        self.reprocess = reprocess
        self.gmail_client = get_gmail_client()
        self.classifier = get_classifier(self.config)
        self.sheets_client = get_sheets_client()

        # Stats
        self.stats = {
            "emails_fetched": 0,
            "filtered_out": 0,
            "classified": 0,
            "matched": 0,
            "updated": 0,
            "no_match": 0,
            "unknown_category": 0,
        }

    def _find_matching_job(self, classification: EmailClassification) -> Optional[JobRow]:
        """
        Find a matching job in Sheets by trying all company candidates.

        Args:
            classification: Email classification with company candidates and position

        Returns:
            Matching JobRow or None if not found (or ambiguous)
        """
        if not classification.company_candidates:
            return None

        position = classification.position

        # Try each company candidate
        for company in classification.company_candidates:
            # Try company + position first (if position available)
            if position:
                matches = self.sheets_client.find_jobs_by_company_and_position(company, position)
                if len(matches) == 1:
                    logger.debug(f"Matched by company + position: {company} + {position}")
                    return matches[0]
                elif len(matches) > 1:
                    logger.warning(f"Multiple jobs match '{company}' + '{position}', trying next candidate")
                    continue

            # Fall back to company only
            matches = self.sheets_client.find_jobs_by_company(company)

            # Try alternative company name formats if no match
            if not matches:
                alternatives = [
                    company.replace(" LLC", "").replace(" Inc", "").replace(" Corp", "").strip(),
                    company.split()[0] if " " in company else company,  # First word
                ]
                for alt in alternatives:
                    if alt != company:
                        matches = self.sheets_client.find_jobs_by_company(alt)
                        if matches:
                            logger.debug(f"Found match using alternative company name: {alt}")
                            break

            if len(matches) == 1:
                logger.debug(f"Matched by company only: {company}")
                return matches[0]
            elif len(matches) > 1:
                logger.warning(f"Multiple jobs for '{company}', trying next candidate")
                continue

        # No match found with any candidate
        logger.info(f"No matching job found for companies: {classification.company_candidates}")
        return None

    def _update_job_status(
        self,
        job: JobRow,
        classification: EmailClassification,
        email: EmailMessage
    ) -> bool:
        """
        Update job status based on email classification.

        Args:
            job: Job to update
            classification: Email classification
            email: Original email message

        Returns:
            True if updated successfully
        """
        category = classification.category

        # Skip unknown category
        if category == "unknown":
            return False

        new_status = CATEGORY_TO_STATUS.get(category)
        if not new_status:
            logger.warning(f"No status mapping for category: {category}")
            return False

        updates = {}

        # Update status
        updates["status"] = new_status

        # Update relevant date column
        date_column = CATEGORY_TO_DATE_COLUMN.get(category)
        if date_column:
            date_str = datetime.now().strftime("%m/%d/%Y")
            if classification.date_mentioned:
                # Try to use the date from the email
                try:
                    date_str = normalize_date(classification.date_mentioned)
                except Exception:
                    pass

            # Add date (handles multiple dates)
            self.sheets_client.add_date_to_column(job.row_number, date_column, date_str)
            # Don't include in updates dict since add_date_to_column handles it

        # For offer/rejection, add details to notes
        if category in ("offer", "rejection"):
            note_parts = [f"Email received: {email.date.strftime('%Y-%m-%d')}"]
            if classification.key_details:
                note_parts.append(classification.key_details)
            if classification.action_required:
                note_parts.append(f"Action: {classification.action_required}")

            note = "; ".join(note_parts)
            self.sheets_client.append_to_notes(job.row_number, note)

        # Apply status update
        try:
            self.sheets_client.update_job(job.row_number, updates)
            logger.info(f"Updated job status: {job.company} - {job.position} -> {new_status}")

            # Apply row color based on status
            self.sheets_client.apply_status_color(job.row_number, new_status)

            # Send Discord notification if dream company
            if self.config.discord.enabled and job.company:
                if is_dream_company(
                    job.company,
                    self.config.user.target_companies,
                    self.config.discord.dream_company_match_threshold
                ):
                    logger.info(f"  Dream company status change! Sending Discord notification...")
                    try:
                        notify_status_change(job.company, job.position, new_status, job.position_url or "")
                    except Exception as discord_error:
                        logger.warning(f"  Failed to send Discord notification: {discord_error}")

            return True
        except Exception as e:
            logger.error(f"Failed to update job: {e}")
            return False

    def _process_email(self, email: EmailMessage) -> bool:
        """
        Process a single email.

        Args:
            email: Email message to process

        Returns:
            True if job was updated, False otherwise
        """
        # Log email details for verification
        date_str = email.date.strftime("%Y-%m-%d %H:%M")
        logger.info(f"Processing: {email.sender_email} | {date_str} | [{email.category}] {email.subject}")

        # Apply privacy filters (Layer 2 & 3)
        passed, reason = apply_privacy_filters(email)
        if not passed:
            logger.debug(f"Email filtered: {reason}")
            self.stats["filtered_out"] += 1
            # Still mark as processed to avoid re-checking
            self.gmail_client.mark_as_processed(email.message_id)
            return False

        # Classify with AI
        classification = self.classifier.classify(email)
        if classification is None:
            logger.warning("Classification failed")
            self.gmail_client.mark_as_processed(email.message_id)
            return False

        self.stats["classified"] += 1
        logger.info(f"AI extracted - Category: {classification.category}, Companies: {classification.company_candidates}, Position: {classification.position}")

        # Skip unknown category
        if classification.category == "unknown":
            logger.debug("Unknown category, skipping")
            self.stats["unknown_category"] += 1
            self.gmail_client.mark_as_processed(email.message_id)
            return False

        # Find matching job
        job = self._find_matching_job(classification)
        if job is None:
            self.stats["no_match"] += 1
            self.gmail_client.mark_as_processed(email.message_id)
            return False

        self.stats["matched"] += 1
        logger.debug(f"Matched to: {job.company} - {job.position} (row {job.row_number})")

        # Update job status
        updated = self._update_job_status(job, classification, email)
        if updated:
            self.stats["updated"] += 1

        # Mark as processed
        self.gmail_client.mark_as_processed(email.message_id)

        return updated

    def run(self) -> dict:
        """
        Run the Gmail checking pipeline.

        Returns:
            Dict with statistics about the run
        """
        start_time = time.time()
        logger.info("=" * 60)
        logger.info("Starting Gmail status check")
        logger.info("=" * 60)

        # Reset stats
        self.stats = {k: 0 for k in self.stats}

        # Fetch recent emails
        logger.info(f"Fetching emails from last {self.config.gmail_lookback_days} day(s)...")
        if self.reprocess:
            logger.info("Reprocess mode: ignoring processed_emails.json")
        try:
            emails = self.gmail_client.fetch_recent_emails(skip_processed=not self.reprocess)
            self.stats["emails_fetched"] = len(emails)
            logger.info(f"Found {len(emails)} new emails to process")

            # Log summary of all fetched emails
            if emails:
                logger.info("-" * 60)
                logger.info("Emails found:")
                for i, email in enumerate(emails, 1):
                    date_str = email.date.strftime("%Y-%m-%d %H:%M")
                    subject_preview = email.subject[:50] + "..." if len(email.subject) > 50 else email.subject
                    logger.info(f"  {i}. [{email.category}] {email.sender_email}")
                    logger.info(f"     {date_str} | {subject_preview}")
                logger.info("-" * 60)
        except Exception as e:
            logger.error(f"Failed to fetch emails: {e}")
            emails = []

        # Process each email
        for email in emails:
            try:
                self._process_email(email)
            except Exception as e:
                logger.error(f"Error processing email '{email.subject[:40]}...': {e}")
                # Still mark as processed to avoid infinite retries
                self.gmail_client.mark_as_processed(email.message_id)

        # Log summary
        elapsed = time.time() - start_time
        logger.info("=" * 60)
        logger.info("Gmail check complete!")
        logger.info(f"  Time elapsed: {elapsed:.1f}s")
        logger.info(f"  Emails fetched: {self.stats['emails_fetched']}")
        logger.info(f"  Filtered out: {self.stats['filtered_out']}")
        logger.info(f"  Classified: {self.stats['classified']}")
        logger.info(f"  Unknown category: {self.stats['unknown_category']}")
        logger.info(f"  Matched to jobs: {self.stats['matched']}")
        logger.info(f"  No match found: {self.stats['no_match']}")
        logger.info(f"  Jobs updated: {self.stats['updated']}")
        logger.info("=" * 60)

        return self.stats


def run_once(reprocess: bool = False):
    """Run the pipeline once."""
    config = get_config()
    setup_logging("gmail", config, console=True)

    checker = GmailChecker(config, reprocess=reprocess)
    stats = checker.run()

    return stats


def run_scheduled():
    """Run the pipeline on a schedule."""
    config = get_config()
    setup_logging("gmail", config, console=True)

    interval = config.gmail_check_interval_minutes
    logger.info(f"Starting scheduled Gmail checker (every {interval} minutes)")

    scheduler = BlockingScheduler()

    def job():
        try:
            checker = GmailChecker(config)
            checker.run()
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


def main():
    parser = argparse.ArgumentParser(description="ApplyPotato Gmail Status Checker")
    parser.add_argument("--scheduled", action="store_true", help="Run on schedule")
    parser.add_argument("--reprocess", action="store_true", help="Reprocess all emails (ignore processed_emails.json)")
    args = parser.parse_args()

    if args.scheduled:
        run_scheduled()
    else:
        run_once(reprocess=args.reprocess)


if __name__ == "__main__":
    main()

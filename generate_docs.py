#!/usr/bin/env python3
"""
Phase 2: Generate tailored resume and/or cover letter for jobs in Google Sheets.

Reads Resume/Cover Letter columns and generates missing documents for jobs
added within JOB_AGE_LIMIT_DAYS. Idempotent — skips files that already exist.

Usage:
    python generate_docs.py 3013               # specific row number
    python generate_docs.py 3013_Pathos        # specific folder stem
    python generate_docs.py --all              # all recent rows needing docs
    python generate_docs.py --scheduled        # daemon mode (SCRAPE_INTERVAL_MINUTES)
"""

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from src.config import get_config, Config
from src.logging_config import setup_logging
from src.sheets import get_sheets_client
from src.job_desc import commit_and_push_job_folder

logger = logging.getLogger(__name__)


def _sanitize(s: str) -> str:
    s = re.sub(r'[^\w\s-]', '', s).strip()
    return re.sub(r'[\s]+', '_', s)[:50]


def _stem(row_num: int, company: str) -> str:
    return f"{row_num}_{_sanitize(company)}"


def _parse_date(date_str) -> datetime:
    # Sheets is read with valueRenderOption="FORMULA", so date-formatted cells come
    # back as a Google Sheets serial number (days since 1899-12-30), not a string.
    try:
        serial = float(str(date_str).strip())
        return datetime(1899, 12, 30) + timedelta(days=serial)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.strptime(str(date_str).strip(), "%m/%d/%Y")
    except (ValueError, AttributeError):
        return datetime.min


def process_job(job_row, config: Config) -> bool:
    """
    Generate documents for a single Sheets row. Returns True if anything was generated.
    """
    from src.auto_apply import AutoApplyOrchestrator

    row_num = job_row.row_number
    company = job_row.company
    stem = _stem(row_num, company)
    folder = config.job_desc_output_dir / stem

    needs_resume = job_row.resume_needed.strip().lower() == "yes"
    needs_cl = job_row.cover_letter_needed.strip().lower() == "yes"

    if not needs_resume and not needs_cl:
        logger.debug(f"  Row {row_num} ({company}): no docs needed")
        return False

    if not folder.exists():
        logger.warning(f"  Row {row_num} ({company}): folder not found ({folder}) — skipping")
        return False

    resume_out = folder / f"{stem}_Resume.docx"
    cl_out = folder / f"{stem}_Cover_Letter.docx"

    do_resume = needs_resume and not resume_out.exists()
    do_cl = needs_cl and not cl_out.exists()

    if not do_resume and not do_cl:
        logger.info(f"  Row {row_num} ({company}): docs already exist — skipping")
        return False

    logger.info(
        f"  Row {row_num} ({company}): "
        f"generating {'resume' if do_resume else ''}"
        f"{' + ' if do_resume and do_cl else ''}"
        f"{'cover letter' if do_cl else ''}..."
    )

    try:
        orchestrator = AutoApplyOrchestrator(config)
        generated = orchestrator.generate_for_folder(
            folder=folder,
            stem=stem,
            needs_resume=do_resume,
            needs_cover_letter=do_cl,
        )
    except Exception as e:
        logger.error(f"  Row {row_num} ({company}): generation failed — {e}")
        return False

    if generated:
        for p in generated:
            logger.info(f"  Generated: {p.name}")
        try:
            commit_and_push_job_folder(folder, config.job_desc_output_dir, stem)
        except Exception as e:
            logger.warning(f"  Git push failed (non-fatal): {e}")
        return True

    logger.warning(f"  Row {row_num} ({company}): no files produced")
    return False


def run_for_target(target: str, config: Config) -> None:
    """Process a single row by row number or folder stem."""
    sheets = get_sheets_client()
    all_jobs = sheets.get_all_jobs()

    try:
        row_num = int(target)
        matches = [j for j in all_jobs if j.row_number == row_num]
    except ValueError:
        matches = [
            j for j in all_jobs
            if _stem(j.row_number, j.company).lower().startswith(target.lower())
        ]

    if not matches:
        logger.error(f"No Sheets row found for: {target}")
        sys.exit(1)

    job = matches[0]
    process_job(job, config)


def run_all(config: Config) -> dict:
    """Process all rows added within JOB_AGE_LIMIT_DAYS that need docs."""
    sheets = get_sheets_client()
    all_jobs = sheets.get_all_jobs()

    cutoff = datetime.now() - timedelta(days=config.job_age_limit_days)
    pending = [
        j for j in all_jobs
        if _parse_date(j.added_date) >= cutoff
        and (
            j.resume_needed.strip().lower() == "yes"
            or j.cover_letter_needed.strip().lower() == "yes"
        )
    ]

    logger.info(f"Found {len(pending)} recent job(s) potentially needing docs")

    stats = {"processed": 0, "generated": 0, "skipped": 0, "failed": 0}
    for job in pending:
        stats["processed"] += 1
        try:
            result = process_job(job, config)
            if result:
                stats["generated"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:
            logger.error(f"  Unexpected error for row {job.row_number}: {e}")
            stats["failed"] += 1

    logger.info(
        f"Done — generated: {stats['generated']}, "
        f"skipped: {stats['skipped']}, failed: {stats['failed']}"
    )
    return stats


def run_scheduled(config: Config) -> None:
    """Run in daemon mode on SCRAPE_INTERVAL_MINUTES."""
    interval = config.scrape_interval_minutes
    logger.info(f"Starting scheduled doc generator (every {interval} minutes)")

    scheduler = BlockingScheduler()

    def job():
        try:
            run_all(config)
        except Exception as e:
            logger.error(f"Scheduled run failed: {e}")

    job()
    scheduler.add_job(job, 'interval', minutes=interval)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


def main():
    parser = argparse.ArgumentParser(description="ApplyPotato Phase 2 — Document Generator")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("target", nargs="?",
                       help="Row number or folder stem (e.g. 3013 or 3013_Pathos)")
    group.add_argument("--all", action="store_true",
                       help=f"Process all recent rows needing docs (within JOB_AGE_LIMIT_DAYS)")
    group.add_argument("--scheduled", action="store_true",
                       help="Daemon mode — run on SCRAPE_INTERVAL_MINUTES")
    args = parser.parse_args()

    config = get_config()
    setup_logging("generate_docs", config, console=True)

    if args.scheduled:
        run_scheduled(config)
    elif args.all:
        run_all(config)
    elif args.target:
        run_for_target(args.target, config)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Send a Discord summary of today's pipeline activity.

Scheduled via Windows Task Scheduler at 09:00 and 17:00 daily:
    python scripts/daily_summary.py

Summary covers:
  - Jobs discovered today (added_date == today)
  - Jobs with docs ready / Phase 2 complete (status New + docs on disk)
  - Forms filled but not yet submitted (filled_forms.json, status != Applied)
  - Jobs applied today (application_date == today)
  - Total pending (status == New)
"""

import json
import re
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from src.config import get_config
from src.logging_config import setup_logging, get_logger
from src.sheets import get_sheets_client


def _parse_date(value) -> date | None:
    """Parse a Sheets cell value to a date. Handles serial numbers and MM/DD/YYYY strings."""
    try:
        serial = float(str(value).strip())
        return (datetime(1899, 12, 30) + timedelta(days=serial)).date()
    except (ValueError, TypeError):
        pass
    try:
        return datetime.strptime(str(value).strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


def _sanitize(s: str) -> str:
    s = re.sub(r'[^\w\s-]', '', s).strip()
    return re.sub(r'[\s]+', '_', s)[:50]


def _stem(row_num: int, company: str) -> str:
    return f"{row_num}_{_sanitize(company)}"


def main() -> None:
    config = get_config()
    setup_logging("daily_summary", config, console=False)
    logger = get_logger(__name__)
    webhook_url = config.discord.form_fill_webhook_url
    if not webhook_url:
        logger.error("FORM_FILL_DISCORD_WEBHOOK not set in .env")
        sys.exit(1)

    sheets = get_sheets_client()
    jobs = sheets.get_all_jobs()

    today = datetime.now().date()
    today_str = today.strftime("%m/%d/%Y")

    # row_number → status lookup for cross-referencing filled_forms.json
    row_status = {job.row_number: job.status for job in jobs}

    discovered_today = 0
    dream_today = 0
    applied_today = 0
    new_total = 0
    docs_ready = 0

    output_dir = config.job_desc_output_dir

    for job in jobs:
        if _parse_date(job.added_date) == today:
            discovered_today += 1
            if job.dream == "Yes":
                dream_today += 1

        if job.application_date and _parse_date(job.application_date) == today:
            applied_today += 1

        if job.status == "New":
            new_total += 1
            if _parse_date(job.added_date) == today:
                stem = _stem(job.row_number, job.company)
                folder = output_dir / stem
                has_docs = (
                    (folder / f"{stem}_Resume.docx").exists()
                    or (folder / f"{stem}_Cover_Letter.docx").exists()
                )
                if has_docs:
                    docs_ready += 1

    # Count forms filled today that haven't been submitted yet
    filled_not_submitted = 0
    filled_path = Path(__file__).parent.parent / "data" / "filled_forms.json"
    if filled_path.exists():
        try:
            entries = json.loads(filled_path.read_text(encoding="utf-8"))
            for entry in entries:
                if entry.get("filled_at") == today_str:
                    status = row_status.get(entry.get("row"), "")
                    if status != "Applied":
                        filled_not_submitted += 1
        except (json.JSONDecodeError, OSError):
            pass

    date_label = f"{today.strftime('%B')} {today.day}, {today.year}"
    divider = "─" * 35

    msg = (
        f"📊 **Pipeline Summary — {date_label}**\n"
        f"{divider}\n"
        f"🔍 Discovered today:       **{discovered_today}**\n"
        f"⭐ Dream companies:         **{dream_today}**\n"
        f"📄 Docs ready (Phase 2):   **{docs_ready}**\n"
        f"📋 Filled, not submitted:  **{filled_not_submitted}**\n"
        f"✅ Applied today:           **{applied_today}**\n"
        f"{divider}\n"
        f"📥 Total pending (New):    **{new_total}**"
    )

    try:
        resp = httpx.post(webhook_url, json={"content": msg}, timeout=10.0)
        resp.raise_for_status()
        logger.info("Pipeline summary sent.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Error sending Discord message: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

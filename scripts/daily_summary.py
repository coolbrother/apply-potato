#!/usr/bin/env python3
"""
Send a Discord summary of recent pipeline activity, windowed by run time.

Scheduled via Windows Task Scheduler at 09:00 and 17:00 daily:
    python scripts/daily_summary.py

Each run reports only what happened since the previous run:
  - 5pm (evening) run -> window is [today 09:00, now]
  - 9am (morning) run -> window is [yesterday 17:00, now]

The window is inferred from the clock (morning if hour < 13, else evening) and can
be overridden with --window for manual/test runs.

Windowed metrics (by timestamp stored in the Sheet / filled_forms.json):
  - Jobs discovered in window (added_date within window)
  - Dream-company jobs in window
  - Jobs with docs ready / Phase 2 complete (status New + docs on disk)
  - Forms filled but not yet submitted (filled_forms.json, status != Applied)
  - Jobs applied in window (application_date within window)
Running total (NOT windowed):
  - Total pending (status == New)
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from src.config import get_config
from src.logging_config import setup_logging, get_logger
from src.sheets import get_sheets_client


def _parse_dt(value) -> datetime | None:
    """Parse a Sheets/JSON cell value to a datetime.

    Handles Google Sheets serial numbers (read via valueRenderOption=FORMULA, which
    carry a fractional part for the time), "M/D/YYYY H:MM:SS" and "M/D/YYYY" strings,
    and semicolon-separated multi-date cells (the most recent / last segment is used).
    Returns None if unparseable.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    # Semicolon-separated cells (e.g. "6/13/2026; 6/14/2026 10:00:00") -> use last
    if ";" in text:
        text = text.split(";")[-1].strip()

    # Google Sheets serial number (days since 1899-12-30), with fractional time
    try:
        serial = float(text)
        return datetime(1899, 12, 30) + timedelta(days=serial)
    except (ValueError, TypeError):
        pass

    # ISO format (used by filter_log.json)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass

    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _sanitize(s: str) -> str:
    s = re.sub(r'[^\w\s-]', '', s).strip()
    return re.sub(r'[\s]+', '_', s)[:50]


def _stem(row_num: int, company: str) -> str:
    return f"{row_num}_{_sanitize(company)}"


def _resolve_window(window: str | None, now: datetime) -> tuple[str, datetime, datetime]:
    """Return (window_name, start, end) for the summary.

    If window is None, infer from the clock: morning if before 13:00, else evening.
    """
    if window is None:
        window = "morning" if now.hour < 13 else "evening"

    if window == "evening":
        start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    else:  # morning
        start = (now - timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)

    return window, start, now


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a windowed Discord pipeline summary.")
    parser.add_argument(
        "--window",
        choices=["morning", "evening"],
        default=None,
        help="Force the summary window. Defaults to clock inference (morning if <13:00).",
    )
    args = parser.parse_args()

    config = get_config()
    setup_logging("daily_summary", config, console=False)
    logger = get_logger(__name__)
    webhook_url = config.discord.form_fill_webhook_url
    if not webhook_url:
        logger.error("FORM_FILL_DISCORD_WEBHOOK not set in .env")
        sys.exit(1)

    now = datetime.now()
    window, start, end = _resolve_window(args.window, now)
    logger.info(f"Window: {window} [{start.isoformat()} .. {end.isoformat()}]")

    def in_window(dt: datetime | None) -> bool:
        return dt is not None and start <= dt <= end

    sheets = get_sheets_client()
    jobs = sheets.get_all_jobs()

    # row_number -> status lookup for cross-referencing filled_forms.json
    row_status = {job.row_number: job.status for job in jobs}

    discovered = 0
    dream = 0
    applied = 0
    new_total = 0
    docs_ready = 0

    output_dir = config.job_desc_output_dir

    for job in jobs:
        added_dt = _parse_dt(job.added_date)
        if in_window(added_dt):
            discovered += 1
            if job.dream == "Yes":
                dream += 1
            if job.status == "New":
                stem = _stem(job.row_number, job.company)
                folder = output_dir / stem
                has_docs = (
                    (folder / f"{stem}_Resume.docx").exists()
                    or (folder / f"{stem}_Cover_Letter.docx").exists()
                )
                if has_docs:
                    docs_ready += 1

        if job.application_date and in_window(_parse_dt(job.application_date)):
            applied += 1

        if job.status == "New":
            new_total += 1

    # Count forms filled in window that haven't been submitted yet
    filled_not_submitted = 0
    filled_path = Path(__file__).parent.parent / "data" / "filled_forms.json"
    if filled_path.exists():
        try:
            entries = json.loads(filled_path.read_text(encoding="utf-8"))
            for entry in entries:
                if in_window(_parse_dt(entry.get("filled_at"))):
                    status = row_status.get(entry.get("row"), "")
                    if status != "Applied":
                        filled_not_submitted += 1
        except (json.JSONDecodeError, OSError):
            pass

    # Count filtered-out jobs in window, grouped by category
    filter_counts: dict[str, int] = {}
    filter_log_path = Path(__file__).parent.parent / "data" / "filter_log.json"
    if filter_log_path.exists():
        try:
            filter_entries = json.loads(filter_log_path.read_text(encoding="utf-8"))
            for entry in filter_entries:
                if in_window(_parse_dt(entry.get("timestamp"))):
                    cat = entry.get("category", "other")
                    filter_counts[cat] = filter_counts.get(cat, 0) + 1
        except (json.JSONDecodeError, OSError):
            pass

    total_filtered = sum(filter_counts.values())
    scanned = discovered + total_filtered

    # %-d / %-I are not supported on Windows; build the label manually.
    def _fmt(dt: datetime) -> str:
        hour12 = dt.hour % 12 or 12
        ampm = "am" if dt.hour < 12 else "pm"
        return f"{dt.month}/{dt.day} {hour12}:{dt.minute:02d}{ampm}"

    range_label = f"{_fmt(start)} – {_fmt(end)}"
    divider = "─" * 35

    category_labels = {
        "job_type": "Job type",
        "class_standing": "Class standing",
        "graduation": "Graduation",
        "season_year": "Season/year",
        "work_auth": "Work auth",
        "other": "Other",
    }

    filter_lines = "".join(
        f"\n     ↳ {category_labels.get(cat, cat)}: {count}"
        for cat, count in sorted(filter_counts.items())
        if count > 0
    )

    msg = (
        f"📊 **Pipeline Summary — {range_label}**\n"
        f"{divider}\n"
        f"🔎 Scanned:                **{scanned}**\n"
        f"🚫 Filtered out:           **{total_filtered}**{filter_lines}\n"
        f"🔍 Discovered:             **{discovered}**\n"
        f"⭐ Dream companies:         **{dream}**\n"
        f"📄 Docs ready (Phase 2):   **{docs_ready}**\n"
        f"📋 Filled, not submitted:  **{filled_not_submitted}**\n"
        f"✅ Applied:                 **{applied}**\n"
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

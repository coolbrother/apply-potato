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
  - Status emails needing review (needs_review.json), grouped by reason
Running totals (NOT windowed):
  - Total pending (status == New)
  - Season funnel: cumulative count of jobs that have reached each stage this
    season (Applied / OA / Phone / Technical / Offer / Rejected / Ghosted).
    "Reached" is date-driven where a date column exists, so a job that is now
    Rejected still counts toward Applied/OA/Phone/Technical.
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
from src.needs_review import REASON_LABELS, load_needs_review
from src.sheets import (
    get_sheets_client,
    parse_sheet_datetime, 
    split_date_cell,
    STATUS_APPLIED, STATUS_GHOSTED, STATUS_NEW, STATUS_OA, STATUS_OFFER,
    STATUS_PHONE, STATUS_REJECTED, STATUS_TECHNICAL,
)


def _parse_dt(value) -> datetime | None:
    """Parse a Sheets/JSON cell value to a datetime, or None if unparseable.

    A multi-date cell (e.g. "6/13/2026; 6/14/2026 10:00:00") resolves to its last
    segment — for a summary, the most recent round is the one that matters. Parsing
    a single value lives in src.sheets.parse_sheet_datetime.
    """
    segments = split_date_cell(value)
    return parse_sheet_datetime(segments[-1]) if segments else None


def _sanitize(s: str) -> str:
    s = re.sub(r'[^\w\s-]', '', s).strip()
    return re.sub(r'[\s]+', '_', s)[:50]


def _stem(row_num: int, company: str) -> str:
    return f"{row_num}_{_sanitize(company)}"


# Statuses that imply the application was submitted, even if the Application Date
# cell was never filled in (e.g. status advanced by the Gmail classifier).
POST_APPLY_STATUSES = {
    STATUS_APPLIED, STATUS_OA, STATUS_PHONE, STATUS_TECHNICAL,
    STATUS_OFFER, STATUS_REJECTED, STATUS_GHOSTED,
}

# Statuses where the pipeline has stopped moving for that job.
CLOSED_STATUSES = {STATUS_OFFER, STATUS_REJECTED, STATUS_GHOSTED}


def _has_value(cell) -> bool:
    """Whether a Sheets cell holds anything. Date cells come back as serial floats."""
    return bool(str(cell or "").strip())


def _season_matches(target: str | None, job_season_year: str | None) -> bool:
    """Whether a job row belongs to the user's target season.

    Mirrors filters.check_season_year: no target, or a job with no season/year (or
    no year in it) counts as a match, since those rows were admitted to the sheet
    as candidates for the current season.
    """
    if not target:
        return True

    job = str(job_season_year or "").strip()
    if not job:
        return True

    job_year = re.search(r"\d{4}", job)
    if not job_year:
        return True

    target_year = re.search(r"\d{4}", target)
    if not target_year:
        return True

    return target_year.group() == job_year.group()


def _season_totals(jobs, target_season_year: str | None) -> dict[str, int]:
    """Cumulative per-stage counts for every job in the target season.

    Each stage counts jobs that ever *reached* it, not jobs currently sitting in
    it — a job now marked Rejected still counts toward Applied and any interview
    stage whose date column is filled. Stages are therefore not mutually
    exclusive and will not sum to the season total.
    """
    totals = {
        "in_season": 0, "applied": 0, "oa": 0, "phone": 0, "technical": 0,
        "offer": 0, "rejected": 0, "ghosted": 0, "awaiting": 0,
    }

    for job in jobs:
        if not _season_matches(target_season_year, job.season_year):
            continue
        totals["in_season"] += 1

        if _has_value(job.application_date) or job.status in POST_APPLY_STATUSES:
            totals["applied"] += 1
            if job.status not in CLOSED_STATUSES:
                totals["awaiting"] += 1
        if _has_value(job.oa_date) or job.status == STATUS_OA:
            totals["oa"] += 1
        if _has_value(job.phone_date) or job.status == STATUS_PHONE:
            totals["phone"] += 1
        if _has_value(job.tech_date) or job.status == STATUS_TECHNICAL:
            totals["technical"] += 1
        if job.status == STATUS_OFFER:
            totals["offer"] += 1
        elif job.status == STATUS_REJECTED:
            totals["rejected"] += 1
        elif job.status == STATUS_GHOSTED:
            totals["ghosted"] += 1

    return totals


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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the summary instead of posting it to Discord.",
    )
    args = parser.parse_args()

    config = get_config()
    setup_logging("daily_summary", config, console=False)
    logger = get_logger(__name__)
    webhook_url = config.discord.form_fill_webhook_url
    if not webhook_url and not args.dry_run:
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
            if job.status == STATUS_NEW:
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

        if job.status == STATUS_NEW:
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
                    if status != STATUS_APPLIED:
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

    # Status emails check_gmail.py could not attribute to a row, grouped by reason.
    # These were marked processed and will not come back on their own — the summary
    # is the only place they surface.
    review_counts: dict[str, int] = {}
    for entry in load_needs_review(Path(__file__).parent.parent / "data"):
        if in_window(_parse_dt(entry.get("timestamp"))):
            reason = entry.get("reason", "other")
            review_counts[reason] = review_counts.get(reason, 0) + 1

    total_review = sum(review_counts.values())

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

    review_lines = "".join(
        f"\n     ↳ {REASON_LABELS.get(reason, reason)}: {count}"
        for reason, count in sorted(review_counts.items())
        if count > 0
    )

    target_season = config.user.target_season_year
    totals = _season_totals(jobs, target_season)
    season_label = target_season or "All Time"

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
        f"⚠️ Needs review:           **{total_review}**{review_lines}\n"
        f"{divider}\n"
        f"📥 Total pending (New):    **{new_total}**\n"
        f"\n"
        f"🏆 **Season Totals — {season_label}**\n"
        f"{divider}\n"
        f"✅ Applied:                 **{totals['applied']}**\n"
        f"📝 OA:                      **{totals['oa']}**\n"
        f"📞 Phone interview:        **{totals['phone']}**\n"
        f"💻 Technical interview:    **{totals['technical']}**\n"
        f"🎉 Offer:                   **{totals['offer']}**\n"
        f"❌ Rejected:                **{totals['rejected']}**\n"
        f"👻 Ghosted:                 **{totals['ghosted']}**\n"
        f"⏳ Awaiting response:      **{totals['awaiting']}**\n"
        f"_Cumulative — a job counts toward every stage it reached._"
    )

    if args.dry_run:
        print(msg)
        sys.exit(0)

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

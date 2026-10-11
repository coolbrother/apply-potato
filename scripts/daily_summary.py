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
  - New rejections in window, each named by row, company and position
  - Status emails needing review (needs_review.json), grouped by reason
  - Jev second opinion (jev_shadow.json): emails where Jev disagreed with the LLM or
    saw a likely status in one the LLM called unknown; omitted when there are none
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
from src.jev_log import load_entries as load_jev_entries, summary_entries, summary_line
from src.logging_config import setup_logging, get_logger
from src.needs_review import (
    REASON_DUPLICATE_ROWS,
    REASON_LABELS,
    REASON_UNCATEGORIZED,
    load_needs_review,
)
from src.sheets import (
    COMPLETABLE_STAGES,
    event_key,
    get_sheets_client,
    parse_sheet_datetime,
    reached_stage,
    split_date_cell,
    split_stages,
    STATUS_APPLIED, STATUS_GHOSTED, STATUS_NEW, STATUS_OA, STATUS_OFFER,
    STATUS_PHONE, STATUS_REJECTED, STATUS_TECHNICAL, STATUS_WITHDRAWN,
)

# Discord rejects a webhook message whose content exceeds this outright, so the
# outstanding-work block is trimmed to fit rather than risking the whole summary.
DISCORD_LIMIT = 2000


# Detail lines listed under each review reason before collapsing into a "+N more".
# Discord caps a message at 2000 characters, so this stays small.
MAX_REVIEW_DETAIL = 4

# Offers and rejections named one per line before collapsing into a "+N more".
MAX_OUTCOME_DETAIL = 8

# Jev-flagged emails named before collapsing into a "+N more". The backtest month
# averaged about one per summary, so this is headroom for a busy day.
MAX_JEV_DETAIL = 4

# Withdrawn rows some summary has already named. A withdrawal is listed once, then
# counted; see _withdrawn_block.
WITHDRAWN_REPORTED_FILENAME = "withdrawn_reported.json"

# Last Email Time is the email's own timestamp, not the moment check_gmail.py got to it.
# A rejection that arrives at 8:57 and is processed at 9:05 is too late for the morning
# summary and dated before the evening window opens, so it would be named by neither.
# Opening the window early closes that gap; the price is that an outcome landing in the
# margin can be named twice, which beats never.
OUTCOME_WINDOW_MARGIN = timedelta(minutes=30)


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
    STATUS_OFFER, STATUS_REJECTED, STATUS_GHOSTED, STATUS_WITHDRAWN,
}

# Statuses where the pipeline has stopped moving for that job.
CLOSED_STATUSES = {STATUS_OFFER, STATUS_REJECTED, STATUS_GHOSTED, STATUS_WITHDRAWN}


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
        "offer": 0, "rejected": 0, "ghosted": 0, "withdrawn": 0, "awaiting": 0,
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
        elif job.status == STATUS_WITHDRAWN:
            totals["withdrawn"] += 1

    return totals


# Events that hand the applicant something new to do. Anything else as the last event —
# a rejection, an offer, a submitted assessment — leaves nothing outstanding.
INVITATION_CATEGORIES = {"oa": "OA", "phone": "Phone", "technical": "Technical"}


def _stage_progress(jobs, struck: set, target_season_year: str | None) -> dict[str, dict]:
    """
    Per stage: what is outstanding, how many are completed, and how many there are.

    Four counts, from different sources, because no one column answers all of it:

      total      every row that reached the stage this season. Deliberately the same
                 rule _season_totals uses, so this figure equals the stage's line in
                 Season Totals rather than quietly disagreeing with it.
      todo       rows sitting at the stage with work still owed.
      withdrawn  Withdrawn rows whose furthest stage is this one — the stage the user
                 walked away from (see _withdrawn_at). Earlier stages were passed, so
                 they stay completed.
      completed  the rest — total minus todo minus withdrawn.

    The parts partition the total, so the figures always sum. Row 308 finished one
    Chicago assessment and was sent a second: it counts as outstanding, not completed,
    because something is still owed there. Counting it in both would read 3/7/9 against a
    total of 9, which invites exactly the doubt the three numbers exist to remove.

    That makes "completed" mean *nothing further owed at this stage*, which is not the
    same question as "how many assessments have I sat" — that one is answered by counting
    rows whose Completed Stages contains the stage, and 308 is included there.

    Outstanding cannot be read from Completed Stages alone: 308's record correctly says
    "OA" while an assessment is still owed. The last email settles it — an invitation
    means new work, a receipt means none.
    """
    progress = {
        stage: {"todo": [], "withdrawn": 0, "completed": 0, "total": 0}
        for stage in COMPLETABLE_STAGES
    }

    for job in _season_rows(jobs, struck, target_season_year):
        for stage in COMPLETABLE_STAGES:
            if reached_stage(job, stage):
                progress[stage]["total"] += 1

        if job.status == STATUS_WITHDRAWN:
            stage = _withdrawn_at(job)
            if stage in progress:
                progress[stage]["withdrawn"] += 1
            continue

        # Only a row still sitting at a stage can owe work there.
        recorded = split_stages(job.completed_stages)
        if job.status in progress and _owes_work(job, job.status, recorded):
            progress[job.status]["todo"].append(job)

    # Derived rather than counted, so the parts cannot fail to partition the total.
    for stage in COMPLETABLE_STAGES:
        p = progress[stage]
        p["completed"] = p["total"] - len(p["todo"]) - p["withdrawn"]

    return progress


def _season_rows(jobs, struck: set, target_season_year: str | None):
    """Named, un-struck rows in the target season — what every block below reads."""
    for job in jobs:
        if not (job.company or "").strip() or job.row_number in struck:
            continue
        if _season_matches(target_season_year, job.season_year):
            yield job


def _withdrawn_at(job) -> str:
    """
    The stage the user withdrew from: the furthest one the row reached.

    Withdrawing ends the process wherever it stood, so the earlier stages were passed.
    A row that never reached an assessment or interview was withdrawn as an application.
    """
    reached = [stage for stage in COMPLETABLE_STAGES if reached_stage(job, stage)]
    return reached[-1] if reached else STATUS_APPLIED


def _withdrawals(jobs, struck: set, target_season_year: str | None) -> list:
    """Every withdrawn row this season, with its stage, in pipeline order then row."""
    order = (STATUS_APPLIED,) + COMPLETABLE_STAGES
    found = [
        (job, _withdrawn_at(job))
        for job in _season_rows(jobs, struck, target_season_year)
        if job.status == STATUS_WITHDRAWN
    ]
    found.sort(key=lambda pair: (order.index(pair[1]), pair[0].row_number))
    return found


def _owes_work(job, stage: str, recorded: list) -> bool:
    """Whether this row has work outstanding at `stage`."""
    invited_to = INVITATION_CATEGORIES.get(event_key(job.last_event))
    if invited_to == stage:
        return True
    # No invitation outstanding, so fall back to the record. This covers rows predating
    # the category column and rows whose last mail was something else.
    return stage not in recorded


def _stage_block(progress: dict[str, dict], budget: int) -> str:
    """
    Render the outstanding work, trimming to fit `budget` characters.

    Discord rejects a message over 2000 characters outright, so this degrades in two
    steps rather than risking the whole summary: positions are dropped first, then the
    list is truncated with a count of what was left out. The counts always survive.
    """
    def render(with_position: bool, limit: int | None) -> str:
        lines = []
        for stage in COMPLETABLE_STAGES:
            todo = progress[stage]["todo"]
            completed, total = progress[stage]["completed"], progress[stage]["total"]
            if not total:
                continue
            # outstanding / completed / withdrawn / total. The last figure matches this
            # stage's line in Season Totals, and the first three sum to it.
            withdrawn = progress[stage]["withdrawn"]
            lines.append(f"**{stage} ({len(todo)}/{completed}/{withdrawn}/{total})**")
            if not todo:
                continue
            shown = todo if limit is None else todo[:limit]
            for job in shown:
                label = f"{job.company} — {job.position}" if with_position else job.company
                lines.append(f"  • {label}, row {job.row_number}")
            if len(todo) > len(shown):
                lines.append(f"  • …and {len(todo) - len(shown)} more")
        return "\n".join(lines)

    for with_position, limit in ((True, None), (False, None), (False, 3), (False, 0)):
        block = render(with_position, limit)
        if len(block) <= budget:
            return block
    return ""


def _load_reported(path: Path) -> set:
    """Row numbers of withdrawals a summary has already named. Empty when missing."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(r) for r in data} if isinstance(data, list) else set()
    except (OSError, ValueError, TypeError):
        return set()


def _save_reported(path: Path, rows: set) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(rows)), encoding="utf-8")


def _withdrawn_block(new: list, others: int, budget: int) -> str:
    """
    One line per newly withdrawn row — company, row, and the stage it was withdrawn at —
    then a count of the withdrawals already reported in an earlier summary.

    A withdrawal is news once. Listing all of them twice a day pushed the useful sections
    out of Discord's limit for no new information; the season total is already above.
    Trimmed the same way as _stage_block if the new ones alone do not fit.
    """
    lines = [f"  • {job.company}, row {job.row_number} ({stage})" for job, stage in new]
    tail = [f"  • +{others} other withdrawn"] if others else []
    for shown in range(len(lines), -1, -1):
        block = lines[:shown]
        if shown < len(lines):
            block.append(f"  • …and {len(lines) - shown} more new")
        text = "\n".join(block + tail)
        if len(text) <= budget:
            return text
    return ""


def _new_outcomes(jobs, struck: set, start: datetime, end: datetime,
                  status: str, event: str) -> list:
    """
    Rows the Gmail checker moved to `status` inside the window, oldest first.

    The checker writes Status, Last Event and Last Email Time together when it applies
    an outcome email, so all three must agree. Status alone is not enough: an Offer row
    whose last mail was an interview invite was touched in the window but did not become
    an offer in it. An outcome typed into the sheet by hand carries no timestamp and
    cannot be dated, so it cannot be listed.

    `event` is the classifier key behind the Last Event cell — "rejection" or "offer".
    """
    opens = start - OUTCOME_WINDOW_MARGIN
    found = []
    for job in jobs:
        if job.row_number in struck or job.status != status:
            continue
        if event_key(job.last_event) != event:
            continue
        decided_at = _parse_dt(job.last_email_time)
        if decided_at is not None and opens <= decided_at <= end:
            found.append((decided_at, job))

    found.sort(key=lambda pair: pair[0])
    return [job for _, job in found]


def _outcome_block(rows: list, budget: int) -> str:
    """
    Render one line per row, trimming to fit `budget` characters.

    Same reason as _stage_block: Discord rejects an over-long message outright, so the
    list shortens into a "+N more" rather than costing the whole summary.
    """
    def label(job) -> str:
        company = (job.company or "").strip()
        position = (job.position or "").strip()
        named = f"{company} — {position}" if position else company
        return f"row {job.row_number} · {named}"

    def render(limit: int) -> str:
        shown = rows[:limit]
        body = "".join(f"\n     • {label(job)}" for job in shown)
        if len(rows) > len(shown):
            body += f"\n     • +{len(rows) - len(shown)} more"
        return body

    for limit in (MAX_OUTCOME_DETAIL, 4, 2, 0):
        block = render(limit)
        if len(block) <= budget:
            return block
    return ""


def _jev_block(entries: list) -> str:
    """
    The "Jev second opinion" line and its detail, or "" when nothing was flagged.

    An empty section is left out entirely rather than shown as a zero: the LLM still
    decides everything, so a quiet window has nothing to say.
    """
    if not entries:
        return ""
    shown = entries[:MAX_JEV_DETAIL]
    body = "".join(f"\n          • {summary_line(e)}" for e in shown)
    if len(entries) > len(shown):
        body += f"\n          • +{len(entries) - len(shown)} more"
    return f"🤖 Jev second opinion:     **{len(entries)}**{body}\n"


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

    # Retired duplicates are struck rather than deleted. Listing them as outstanding work
    # would be noise, so the outstanding block skips them the way the matcher does.
    try:
        struck_rows = sheets.get_struck_rows()
    except Exception as e:
        logger.warning(f"Could not read struck rows, treating none as struck: {e}")
        struck_rows = set()

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
    review_entries: dict[str, list[dict]] = {}
    for entry in load_needs_review(Path(__file__).parent.parent / "data"):
        if in_window(_parse_dt(entry.get("timestamp"))):
            reason = entry.get("reason", "other")
            review_counts[reason] = review_counts.get(reason, 0) + 1
            review_entries.setdefault(reason, []).append(entry)

    total_review = sum(review_counts.values())

    # Emails Jev flagged in review mode. Read from the file whatever JEV_MODE is now, so
    # turning shadow off does not hide flags recorded earlier in the window.
    jev_lines = _jev_block(summary_entries(
        load_jev_entries(Path(__file__).parent.parent / "data"), start, end))

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

    # A bare count per reason is not actionable: it says four emails went unmatched
    # without saying which applications they were about. Name each one, since the
    # whole point of the line is to send the user somewhere.
    def _review_detail(entry: dict) -> str:
        company = (entry.get("company") or "").strip()
        candidates = entry.get("candidates") or []
        rows = ", ".join(str(c.get("row")) for c in candidates if c.get("row"))

        if entry.get("reason") == REASON_DUPLICATE_ROWS and candidates:
            # The row numbers ARE the fix here: mark_canonical.py takes one.
            position = (candidates[0].get("position") or "").strip()
            return f"{company} · {position} · rows {rows}"

        if entry.get("reason") == REASON_UNCATEGORIZED and candidates:
            # The row was tinted, but a tint is unfindable among thousands of rows,
            # so lead with the row number and say what the mail actually was.
            c = candidates[0]
            position = (c.get("position") or "").strip()
            note = (c.get("note") or "").strip()
            return f"row {c.get('row')} · {company} · {position} · {note}"

        subject = (entry.get("subject") or "").strip()
        if len(subject) > 55:
            subject = subject[:54] + "…"
        if company and candidates:
            return f"{company} · {len(candidates)} rows · {subject}"
        if company:
            return f"{company} · {subject}"
        return subject or "(no subject)"

    def _review_block(reason: str, count: int) -> str:
        head = f"\n     ↳ {REASON_LABELS.get(reason, reason)}: {count}"
        entries = review_entries.get(reason, [])
        shown = entries[:MAX_REVIEW_DETAIL]
        body = "".join(f"\n          • {_review_detail(e)}" for e in shown)
        if len(entries) > len(shown):
            body += f"\n          • +{len(entries) - len(shown)} more"
        return head + body

    review_lines = "".join(
        _review_block(reason, count)
        for reason, count in sorted(review_counts.items())
        if count > 0
    )

    target_season = config.user.target_season_year
    totals = _season_totals(jobs, target_season)
    season_label = target_season or "All Time"

    offers = _new_outcomes(jobs, struck_rows, start, end, STATUS_OFFER, "offer")
    rejections = _new_outcomes(jobs, struck_rows, start, end, STATUS_REJECTED, "rejection")
    logger.info(f"New offers in window: {[job.row_number for job in offers]}")
    logger.info(f"New rejections in window: {[job.row_number for job in rejections]}")

    def _message(offer_lines: str, rejection_lines: str) -> str:
        # An offer is rare and worth a headline of its own; a standing "New offers: 0"
        # twice a day is neither news nor encouragement, and the cumulative figure is
        # already in Season Totals. So this line appears only when there is one, unlike
        # rejections, which are routine enough that a zero is worth stating.
        offer_line = (
            f"🎉 **NEW OFFER{'S' if len(offers) > 1 else ''}: "
            f"{len(offers)}**{offer_lines}\n"
            if offers else ""
        )
        return (
            f"📊 **Pipeline Summary — {range_label}**\n"
            f"{divider}\n"
            f"{offer_line}"
            f"🔎 Scanned:                **{scanned}**\n"
            f"🚫 Filtered out:           **{total_filtered}**{filter_lines}\n"
            f"🔍 Discovered:             **{discovered}**\n"
            f"⭐ Dream companies:         **{dream}**\n"
            f"📄 Docs ready (Phase 2):   **{docs_ready}**\n"
            f"📋 Filled, not submitted:  **{filled_not_submitted}**\n"
            f"✅ Applied:                 **{applied}**\n"
            f"❌ New rejections:          **{len(rejections)}**{rejection_lines}\n"
            f"⚠️ Needs review:           **{total_review}**{review_lines}\n"
            f"{jev_lines}"
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
            f"🚪 Withdrawn:               **{totals['withdrawn']}**\n"
            f"⏳ Awaiting response:      **{totals['awaiting']}**\n"
            f"_Cumulative — a job counts toward every stage it reached._"
        )

    # Both lists are sized against the message without them, so naming outcomes can
    # shorten the Outstanding block below but never push the summary over the limit.
    # Offers are measured first and rejections take what is left: if room ever runs
    # short, the thing worth reading is the offer.
    room = DISCORD_LIMIT - len(_message("", ""))
    offer_lines = _outcome_block(offers, budget=room)
    rejection_lines = _outcome_block(rejections, budget=room - len(offer_lines))
    msg = _message(offer_lines, rejection_lines)

    # Outstanding work, appended within whatever room Discord's 2000-character limit
    # leaves. Counts are cumulative above; this block is strictly "what is still on you".
    stage_block = _stage_block(_stage_progress(jobs, struck_rows, target_season),
                               budget=DISCORD_LIMIT - len(msg) - 80)
    if stage_block:
        msg += f"\n\n📌 **Outstanding**\n{divider}\n{stage_block}"

    # Withdrawals last: nothing is owed on them, so they get whatever room is left. Only
    # rows no earlier summary named are listed; the rest are a count. Withdrawn has no
    # date column — the status is set by hand — so "new" means "not yet reported",
    # tracked in a file and saved only after the summary is actually posted.
    withdrawals = _withdrawals(jobs, struck_rows, target_season)
    reported_path = Path(__file__).parent.parent / "data" / WITHDRAWN_REPORTED_FILENAME
    reported = _load_reported(reported_path)
    new_withdrawals = [(job, stage) for job, stage in withdrawals
                       if job.row_number not in reported]
    withdrawn_header = f"\n\n🚪 **Withdrawn**\n{divider}\n"
    withdrawn_block = _withdrawn_block(
        new_withdrawals,
        others=len(withdrawals) - len(new_withdrawals),
        budget=DISCORD_LIMIT - len(msg) - len(withdrawn_header),
    )
    if withdrawn_block:
        msg += withdrawn_header + withdrawn_block

    if args.dry_run:
        print(msg)
        sys.exit(0)

    try:
        resp = httpx.post(webhook_url, json={"content": msg}, timeout=10.0)
        resp.raise_for_status()
        logger.info("Pipeline summary sent.")
        if new_withdrawals:
            _save_reported(reported_path, reported | {job.row_number for job, _ in withdrawals})
        sys.exit(0)
    except Exception as e:
        logger.error(f"Error sending Discord message: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

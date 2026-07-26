#!/usr/bin/env python3
"""
Mark one row as the real application and strike through the rest.

The Gmail matcher identifies a job by fuzzy company (+ position) text, recomputed
every run. When a company has several rows it cannot choose, refuses to guess, and
the status email is dropped — five DRW rows made a generic DRW confirmation
unmatchable. Setting a status by hand does not help: the matcher never reads the
status column.

Striking through the rows that are *not* the real application does help. The
matcher skips struck rows, so the tie collapses to the one row left standing.

    python scripts/mark_canonical.py 315
        Keep row 315. Strike every other row with the same company.

    python scripts/mark_canonical.py 312 "Software Engineering Intern"
        Keep row 312. Strike only same-company rows whose position matches — for
        duplicate postings of one role (row 312 vs 347) that reached the sheet
        under different URLs, leaving the company's other roles alone.

    python scripts/mark_canonical.py 315 --dry-run
        Show what would change, write nothing.

    python scripts/mark_canonical.py 315 --undo
        Clear strikethrough from every row of that company.

The keeper row is always un-struck, so re-running after a mistake fixes it.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_config
from src.logging_config import setup_logging, get_logger
from src.sheets import JobRow, get_sheets_client


def _same_company(jobs: list, company: str) -> list:
    """
    Every row for a company, by exact case-insensitive name.

    Deliberately stricter than the matcher's substring rule: striking is a write,
    and "DRW" matching into "DRW Holdings" would retire rows the user never looked
    at. Anything the exact rule misses is visible in the printed plan.
    """
    target = company.strip().lower()
    return [job for job in jobs if job.company.strip().lower() == target]


def _matching_position(jobs: list, position: str) -> list:
    """Rows whose position contains the given text, case-insensitively."""
    needle = position.strip().lower()
    return [job for job in jobs if needle in job.position.lower()]


def _describe(job: JobRow, struck_rows: set) -> str:
    mark = " [struck]" if job.row_number in struck_rows else ""
    return f"  row {job.row_number}: {job.company} — {job.position} ({job.status}){mark}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Keep one job row and strike through its duplicates.",
    )
    parser.add_argument("row", type=int, help="Row number to keep (1-indexed, as shown in Sheets)")
    parser.add_argument(
        "position",
        nargs="?",
        default=None,
        help="Optional. Strike only same-company rows whose position matches this text. "
             "Omit to strike every other row from the company.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show the plan, write nothing.")
    parser.add_argument(
        "--undo",
        action="store_true",
        help="Clear strikethrough from every row of the company instead of applying it.",
    )
    args = parser.parse_args()

    config = get_config()
    setup_logging("mark_canonical", config, console=False)
    logger = get_logger(__name__)

    sheets = get_sheets_client()
    jobs = sheets.get_all_jobs()
    by_row = {job.row_number: job for job in jobs}

    keeper = by_row.get(args.row)
    if keeper is None:
        print(f"ERROR: row {args.row} is not a job row. "
              f"Rows run 2..{max(by_row) if by_row else 1} (row 1 is the header).")
        return 1
    if not keeper.company.strip():
        print(f"ERROR: row {args.row} has no company name, so its siblings cannot be found.")
        return 1

    struck_rows = sheets.get_struck_rows()
    siblings = _same_company(jobs, keeper.company)

    print(f"Keeping:  row {keeper.row_number} — {keeper.company} — {keeper.position} ({keeper.status})")
    print(f"Company '{keeper.company}' has {len(siblings)} row(s) in the sheet.")

    if args.undo:
        targets = [job for job in siblings if job.row_number in struck_rows]
        if not targets:
            print("Nothing to undo — no rows for this company are struck through.")
            return 0
        print(f"\nClearing strikethrough from {len(targets)} row(s):")
        for job in targets:
            print(_describe(job, struck_rows))
        if args.dry_run:
            print("\n(dry run — nothing written)")
            return 0
        for job in targets:
            sheets.set_row_strikethrough(job.row_number, False)
            logger.info(f"Cleared strikethrough on row {job.row_number}")
        print(f"\nDone. Cleared {len(targets)} row(s).")
        return 0

    candidates = [job for job in siblings if job.row_number != keeper.row_number]
    if args.position:
        candidates = _matching_position(candidates, args.position)
        print(f"Filtered to positions matching '{args.position}'.")

    # Already-struck rows are left alone so the summary reflects real changes.
    to_strike = [job for job in candidates if job.row_number not in struck_rows]
    already = len(candidates) - len(to_strike)

    if not to_strike:
        if already:
            print(f"\nNothing to do — all {already} matching row(s) are already struck through.")
        elif args.position:
            print(f"\nNothing to do — no other '{keeper.company}' row matches "
                  f"'{args.position}'. Check the position text.")
        else:
            print(f"\nNothing to do — '{keeper.company}' has no other rows.")
    else:
        print(f"\nStriking through {len(to_strike)} row(s):")
        for job in to_strike:
            print(_describe(job, struck_rows))
        if already:
            print(f"  ({already} already struck, left alone)")

    # Re-running after keeping the wrong row must undo the earlier mistake.
    unstrike_keeper = keeper.row_number in struck_rows
    if unstrike_keeper:
        print(f"\nAlso clearing strikethrough from the keeper, row {keeper.row_number}.")

    if not to_strike and not unstrike_keeper:
        return 0

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    for job in to_strike:
        sheets.set_row_strikethrough(job.row_number, True)
        logger.info(f"Struck through row {job.row_number}: {job.company} — {job.position}")
    if unstrike_keeper:
        sheets.set_row_strikethrough(keeper.row_number, False)
        logger.info(f"Cleared strikethrough on keeper row {keeper.row_number}")

    print(f"\nDone. Struck {len(to_strike)} row(s); row {keeper.row_number} is the live one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

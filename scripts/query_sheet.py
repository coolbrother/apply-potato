"""
Read-only inspection of the job database (`GOOGLE_SHEET_ID`).

Exists so that looking at a row does not mean composing a fresh `python -c` every
time. Those one-liners are unreviewable and unauditable: they run once and leave no
artifact. This is the same queries as a committed file that can be read and diffed.

**This script never writes.** It calls only `values().get()` and `spreadsheets().get()`.
No `update`, no `append`, no `batchUpdate`. Keep it that way — it is allowlisted to run
without a prompt precisely because it cannot change anything.

Usage:
    python scripts/query_sheet.py --rows 261,317,478
    python scripts/query_sheet.py --range 515-525
    python scripts/query_sheet.py --company Susquehanna
    python scripts/query_sheet.py --range 515-525 --columns company,position,status
    python scripts/query_sheet.py --rows 518 --formulas
    python scripts/query_sheet.py --summary
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_config
from src.sheets import COLUMNS, SheetsClient, col_letter

COLUMN_NAMES = list(COLUMNS.keys())
LAST_COLUMN = col_letter(len(COLUMNS) - 1)


def parse_rows(args) -> list:
    """Turn --rows / --range into a sorted list of row numbers."""
    rows = set()
    if args.rows:
        for chunk in args.rows.split(","):
            chunk = chunk.strip()
            if chunk:
                rows.add(int(chunk))
    if args.range:
        start, _, end = args.range.partition("-")
        rows.update(range(int(start), int(end or start) + 1))
    return sorted(rows)


def fetch(client, service, first: int, last: int, formulas: bool) -> dict:
    """Read a contiguous block of rows, keyed by row number."""
    render = "FORMULA" if formulas else "FORMATTED_VALUE"
    result = service.spreadsheets().values().get(
        spreadsheetId=client.config.google_sheet_id,
        range=client._range(f"A{first}:{LAST_COLUMN}{last}"),
        valueRenderOption=render,
    ).execute()

    values = result.get("values", [])
    return {
        first + offset: row + [""] * (len(COLUMNS) - len(row))
        for offset, row in enumerate(values)
    }


def show_row(row_number: int, values: list, wanted: list, struck: set) -> None:
    mark = "  [STRUCK]" if row_number in struck else ""
    print(f"\n--- row {row_number}{mark} ---")
    for name in wanted:
        value = values[COLUMNS[name]]
        if value:
            print(f"  {col_letter(COLUMNS[name])} {name}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_argument_group("selection")
    selection.add_argument("--rows", help="Comma-separated row numbers, e.g. 261,317")
    selection.add_argument("--range", dest="range", help="Inclusive range, e.g. 515-525")
    selection.add_argument("--company", help="Every row matching this company name")
    selection.add_argument("--summary", action="store_true",
                           help="Row count and status breakdown")
    parser.add_argument("--columns",
                        help=f"Comma-separated subset of: {', '.join(COLUMN_NAMES)}")
    parser.add_argument("--formulas", action="store_true",
                        help="Show underlying formulas (e.g. =HYPERLINK) instead of text")
    args = parser.parse_args()

    if not any((args.rows, args.range, args.company, args.summary)):
        parser.error("pick one of --rows, --range, --company, --summary")

    wanted = COLUMN_NAMES
    if args.columns:
        wanted = [c.strip() for c in args.columns.split(",") if c.strip()]
        unknown = [c for c in wanted if c not in COLUMNS]
        if unknown:
            parser.error(f"unknown column(s): {', '.join(unknown)}")

    client = SheetsClient(get_config())
    service = client._get_service()

    if args.summary or args.company:
        jobs = [j for j in client.get_all_jobs() if (j.company or "").strip()]

    if args.summary:
        from collections import Counter
        statuses = Counter(j.status or "(blank)" for j in jobs)
        print(f"rows with a company: {len(jobs)}")
        print(f"last populated row:  {max(j.row_number for j in jobs)}")
        print("\nstatus breakdown:")
        for status, count in statuses.most_common():
            print(f"  {count:5d}  {status}")
        return 0

    if args.company:
        needle = args.company.strip().lower()
        rows = sorted(j.row_number for j in jobs
                      if needle in (j.company or "").lower())
        if not rows:
            print(f"No rows matching company '{args.company}'")
            return 0
    else:
        rows = parse_rows(args)

    struck = client.get_struck_rows()
    block = fetch(client, service, min(rows), max(rows), args.formulas)

    for row_number in rows:
        values = block.get(row_number)
        if values is None:
            print(f"\n--- row {row_number} --- (empty)")
            continue
        show_row(row_number, values, wanted, struck)

    return 0


if __name__ == "__main__":
    sys.exit(main())

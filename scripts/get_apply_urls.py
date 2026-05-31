#!/usr/bin/env python3
"""
Print apply URLs and doc paths for specified Google Sheets row numbers as JSON.

Usage:
    python scripts/get_apply_urls.py 3 5 7          # specific rows
    python scripts/get_apply_urls.py --auto 10       # last N rows with status "New"

Output (stdout):
    [
      {
        "row": 3,
        "company": "Google",
        "position": "SWE Intern",
        "url": "https://...",
        "folder": "/Path/To/Your/Resume/3_Google",
        "resume": "/Path/To/Your/Resume/3_Google/3_Google_Resume.docx",
        "cover_letter": "/Path/To/Your/Resume/3_Google/3_Google_Cover_Letter.docx"
      },
      ...
    ]

"folder", "resume", and "cover_letter" are empty strings when not found on disk.

Exit codes:
    0  — at least one row resolved successfully
    1  — bad args or all rows missing
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_config
from src.sheets import SheetsClient


def _parse_date(date_str) -> datetime:
    """Parse a Sheets date (serial number or MM/DD/YYYY string) to datetime."""
    try:
        serial = float(str(date_str).strip())
        return datetime(1899, 12, 30) + timedelta(days=serial)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.strptime(str(date_str).strip(), "%m/%d/%Y")
    except (ValueError, AttributeError):
        return datetime.min


def _find_job_folder(output_dir: Path, row: int) -> Path | None:
    """Return the first folder under output_dir whose name starts with '{row}_'."""
    prefix = f"{row}_"
    try:
        for p in output_dir.iterdir():
            if p.is_dir() and p.name.startswith(prefix):
                return p
    except OSError:
        pass
    return None


def _find_doc(folder: Path, suffix: str) -> str:
    """Return path of the first .docx whose name ends with suffix, or ''."""
    for p in folder.glob(f"*{suffix}"):
        return str(p)
    return ""


def _build_entry(job, output_dir: Path) -> dict:
    folder = _find_job_folder(output_dir, job.row_number)
    folder_str = str(folder) if folder else ""
    resume_str = _find_doc(folder, "_Resume.docx") if folder else ""
    cl_str = _find_doc(folder, "_Cover_Letter.docx") if folder else ""
    return {
        "row": job.row_number,
        "company": job.company or "",
        "position": job.position or "",
        "url": job.position_url or "",
        "folder": folder_str,
        "resume": resume_str,
        "cover_letter": cl_str,
    }


def main() -> None:
    args = sys.argv[1:]

    if not args:
        print("Usage: python scripts/get_apply_urls.py <row1> [row2] ...", file=sys.stderr)
        print("       python scripts/get_apply_urls.py --auto [N]", file=sys.stderr)
        sys.exit(1)

    cfg = get_config()
    client = SheetsClient(cfg)
    output_dir = cfg.job_desc_output_dir

    # --auto mode: last N rows with status "New" (case-insensitive) or empty status
    if args[0] == "--auto":
        limit = int(args[1]) if len(args) > 1 else 10
        all_jobs = client.get_all_jobs()
        cutoff = datetime.now() - timedelta(days=cfg.job_age_limit_days)
        new_jobs = [
            j for j in all_jobs
            if (j.status or "").strip().lower() in ("new", "")
            and _parse_date(j.added_date) >= cutoff
        ]
        # Take the last `limit` (most recently added)
        selected = new_jobs[-limit:]
        results = [_build_entry(j, output_dir) for j in selected]
        print(json.dumps(results, indent=2))
        sys.exit(0 if results else 1)

    # Specific row numbers mode
    try:
        target_rows = [int(r) for r in args]
    except ValueError:
        print("Error: all arguments must be integer row numbers (or --auto)", file=sys.stderr)
        sys.exit(1)

    all_jobs = client.get_all_jobs()
    row_map = {job.row_number: job for job in all_jobs}

    results = []
    found = 0
    for row in target_rows:
        if row in row_map:
            results.append(_build_entry(row_map[row], output_dir))
            found += 1
        else:
            results.append({
                "row": row,
                "company": "?",
                "position": "?",
                "url": "",
                "folder": "",
                "resume": "",
                "cover_letter": "",
                "error": "not found",
            })

    print(json.dumps(results, indent=2))
    sys.exit(0 if found > 0 else 1)


if __name__ == "__main__":
    main()

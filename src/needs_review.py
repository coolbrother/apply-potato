"""
Review queue for status emails that could not be attributed to a Sheet row.

`check_gmail.py` marks every email processed, including ones it could not match,
so an email that fails to match is gone: it is never re-fetched and nothing in the
Sheet records that it arrived. This module is the record — an append-only log in
`data/needs_review.json` that `scripts/daily_summary.py` reports on.

Deliberately a log, not a retry queue. The matcher does not guess between
candidate rows, and re-classifying the same email on every run would just burn AI
calls to reach the same ambiguous answer. One entry, one report, resolved by hand.

Entries are deduped on `message_id`, so `check_gmail.py --reprocess` re-walking
old mail does not multiply them.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


NEEDS_REVIEW_FILENAME = "needs_review.json"

# A company candidate matched more than one row — the email names no position, or
# names one that several rows share. Actionable: pick the right row by hand.
REASON_AMBIGUOUS = "ambiguous"

# Every company candidate matched zero rows. Either the job was never scraped, or
# it was hard-filtered out before reaching the Sheet.
REASON_UNTRACKED = "untracked"

# The classifier read the email as a status update but named no company at all.
REASON_NO_COMPANY = "no_company"

# The company has rows, but every one of them is struck through. Distinct from
# untracked: the user retired these rows by hand, so a status email arriving for
# them means either the wrong row was retired or a new posting was never scraped.
REASON_ALL_RETIRED = "all_retired"

REASON_LABELS = {
    REASON_AMBIGUOUS: "Ambiguous match",
    REASON_UNTRACKED: "Company not tracked",
    REASON_NO_COMPANY: "No company found",
    REASON_ALL_RETIRED: "All rows struck through",
}


def needs_review_path(data_dir: Path) -> Path:
    return Path(data_dir) / NEEDS_REVIEW_FILENAME


def load_needs_review(data_dir: Path) -> List[Dict[str, Any]]:
    """Read the review log. Returns [] if it is missing, empty, or corrupt."""
    path = needs_review_path(data_dir)
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return entries if isinstance(entries, list) else []


def record_needs_review(
    data_dir: Path,
    *,
    message_id: str,
    reason: str,
    account: str = "",
    sender: str = "",
    subject: str = "",
    category: str = "",
    company: str = "",
    candidates: List[Dict[str, Any]] | None = None,
) -> bool:
    """
    Append one unmatched email to the review log.

    Args:
        data_dir: The project's data directory.
        message_id: Gmail message id — the dedup key.
        reason: One of REASON_AMBIGUOUS / REASON_UNTRACKED / REASON_NO_COMPANY.
        account: Which mailbox the email came from.
        company: The company candidate the reason is about (blank for no_company).
        candidates: Rows that tied, as {"row", "position", "status"} dicts.

    Returns:
        True if an entry was written, False if this message was already logged.
    """
    entries = load_needs_review(data_dir)
    if any(entry.get("message_id") == message_id for entry in entries):
        return False

    entries.append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "account": account,
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "reason": reason,
        "category": category,
        "company": company,
        "candidates": candidates or [],
    })

    path = needs_review_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return True

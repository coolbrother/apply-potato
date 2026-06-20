#!/usr/bin/env python3
"""
Append a filled-form entry to data/filled_forms.json.
Called by the fill-form skill after successfully filling each form tab.

Usage: python scripts/update_filled_forms.py <row> <company> <position>
Exit 0 always — failure is non-fatal for the skill.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_config
from src.logging_config import setup_logging, get_logger


def main() -> None:
    cfg = get_config()
    setup_logging("update_filled_forms", cfg, console=False)
    logger = get_logger(__name__)

    if len(sys.argv) < 4:
        logger.error("Usage: update_filled_forms.py <row> <company> <position>")
        sys.exit(1)

    try:
        row = int(sys.argv[1])
    except ValueError:
        logger.error(f"Invalid row number: {sys.argv[1]}")
        sys.exit(0)

    company = sys.argv[2]
    position = sys.argv[3]
    now = datetime.now()
    now_str = now.strftime("%m/%d/%Y %H:%M:%S")
    today_str = now.strftime("%m/%d/%Y")

    data_path = Path(__file__).parent.parent / "data" / "filled_forms.json"

    entries = []
    if data_path.exists():
        try:
            entries = json.loads(data_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []

    # Avoid duplicate entries for the same row on the same day (compare on the date
    # portion, since filled_at now carries a time component).
    for entry in entries:
        if entry.get("row") == row and str(entry.get("filled_at", "")).split(" ")[0] == today_str:
            logger.info(f"Row {row} already recorded for {today_str} — skipping.")
            sys.exit(0)

    entries.append({
        "row": row,
        "company": company,
        "position": position,
        "filled_at": now_str,
    })

    data_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    logger.info(f"Recorded: row {row} ({company} / {position}) filled at {now_str}")


if __name__ == "__main__":
    main()

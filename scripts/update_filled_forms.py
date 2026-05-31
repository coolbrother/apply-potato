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


def main() -> None:
    if len(sys.argv) < 4:
        print("Usage: update_filled_forms.py <row> <company> <position>", file=sys.stderr)
        sys.exit(1)

    try:
        row = int(sys.argv[1])
    except ValueError:
        print(f"Invalid row number: {sys.argv[1]}", file=sys.stderr)
        sys.exit(0)

    company = sys.argv[2]
    position = sys.argv[3]
    today_str = datetime.now().strftime("%m/%d/%Y")

    data_path = Path(__file__).parent.parent / "data" / "filled_forms.json"

    entries = []
    if data_path.exists():
        try:
            entries = json.loads(data_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []

    # Avoid duplicate entries for the same row on the same day
    for entry in entries:
        if entry.get("row") == row and entry.get("filled_at") == today_str:
            print(f"Row {row} already recorded for {today_str} — skipping.")
            sys.exit(0)

    entries.append({
        "row": row,
        "company": company,
        "position": position,
        "filled_at": today_str,
    })

    data_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    print(f"Recorded: row {row} ({company} / {position}) filled on {today_str}")


if __name__ == "__main__":
    main()

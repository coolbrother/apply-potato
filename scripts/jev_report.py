"""
Summarize the Jev shadow trial from data/jev_shadow.json. Read-only.

The file holds only the interesting comparisons (disagreements, low confidence, possible
silent misses), so the overall agreement rate comes from the Gmail logs' per-run
"Jev shadow:" lines instead.

Usage:
    python scripts/jev_report.py           # summary and every recorded entry
    python scripts/jev_report.py --brief   # summary only
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_config
from src.email_categories import CATEGORIES
from src.jev_log import (
    REASON_DISAGREE,
    REASON_LOW_CONFIDENCE,
    REASON_POSSIBLE_MISS,
    load_entries,
)

RUN_LINE = re.compile(r"Jev shadow: agree (\d+), disagree (\d+), failed (\d+), recorded (\d+)")


def run_totals(logs_dir: Path) -> Counter:
    """Agree/disagree/failed/recorded summed over every Gmail run that had Jev on."""
    totals = Counter()
    for path in sorted(logs_dir.glob("gmail-*.log")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = RUN_LINE.search(line)
            if m:
                for key, value in zip(("agree", "disagree", "failed", "recorded"), m.groups()):
                    totals[key] += int(value)
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize the Jev shadow trial")
    parser.add_argument("--brief", action="store_true", help="summary only, no entry list")
    args = parser.parse_args()

    config = get_config()
    entries = load_entries(config.data_dir)
    totals = run_totals(config.logs_dir)

    compared = totals["agree"] + totals["disagree"]
    print("Jev shadow trial")
    print("=" * 60)
    if compared:
        print(f"Emails compared: {compared}  (Jev failed on {totals['failed']} more)")
        print(f"Agreement: {totals['agree']}/{compared} = {totals['agree'] / compared:.1%}")
    else:
        print("No Gmail run has logged a Jev comparison yet.")
    reasons = Counter(r for e in entries for r in e.get("reasons", []))
    print(f"Recorded entries: {len(entries)}")
    print(f"  disagree:        {reasons[REASON_DISAGREE]}")
    print(f"  low confidence:  {reasons[REASON_LOW_CONFIDENCE]}")
    print(f"  possible miss:   {reasons[REASON_POSSIBLE_MISS]}  (LLM said unknown, Jev saw a status)")

    disagreements = [e for e in entries if REASON_DISAGREE in e.get("reasons", [])]
    if disagreements:
        pairs = Counter((e["llm"]["category"], e["jev"]["category"]) for e in disagreements)
        labels = [c for c in CATEGORIES if any(c in p for p in pairs)]
        width = max(len(c) for c in labels) + 2
        print("\nDisagreements (rows: LLM, columns: Jev)")
        print(" " * width + "".join(f"{c[:6]:>8}" for c in labels))
        for llm in labels:
            row = "".join(f"{pairs.get((llm, jev), 0) or '.':>8}" for jev in labels)
            print(f"{llm:<{width}}{row}")

    if args.brief or not entries:
        return
    print("\nEntries (newest first)")
    print("-" * 60)
    for e in sorted(entries, key=lambda x: x.get("date", ""), reverse=True):
        top = ", ".join(f"{k} {v:.2f}" for k, v in list(e["jev"]["probabilities"].items())[:3])
        print(f"{e['date']}  {e['sender']}")
        print(f"  {e['subject']}")
        print(f"  LLM: {e['llm']['category']}   Jev: {e['jev']['category']} @ {e['jev']['confidence']:.2f}"
              f"   [{', '.join(e['reasons'])}]")
        print(f"  Jev top: {top}")


if __name__ == "__main__":
    main()

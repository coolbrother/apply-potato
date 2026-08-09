"""
Mark a stage finished on one row, or undo that.

The Gmail checker records a completion automatically only when exactly one row of that
company is sitting at the stage. Any company running parallel roles — SIG issues a
separate assessment per position — lands here instead, so this is the normal route for
those, not a fallback.

Writes only the Completed Stages column. Status is never touched: a row sits at OA both
before and after the assessment, and only an invitation to the next stage advances it.

Usage:
    python scripts/mark_stage_done.py 518 OA
    python scripts/mark_stage_done.py 518 OA --dry-run
    python scripts/mark_stage_done.py 518 OA --undo
    python scripts/mark_stage_done.py --list OA
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_config
from src.logging_config import setup_logging
from src.sheets import (
    COMPLETABLE_STAGES,
    SheetsClient,
    add_stage,
    remove_stage,
    split_stages,
)

logger = logging.getLogger(__name__)


def _stage_status(stage: str) -> str:
    """The status a row must hold to be sitting at this stage."""
    return stage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("row", nargs="?", type=int, help="1-indexed sheet row")
    parser.add_argument("stage", nargs="?", help=f"one of: {', '.join(COMPLETABLE_STAGES)}")
    parser.add_argument("--undo", action="store_true", help="remove the stage instead")
    parser.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
    parser.add_argument("--list", dest="list_stage", metavar="STAGE",
                        help="list rows at STAGE and whether each is done")
    args = parser.parse_args()

    config = get_config()
    setup_logging("mark_stage_done", config, console=True)
    client = SheetsClient(config)

    canonical = {s.lower(): s for s in COMPLETABLE_STAGES}

    if args.list_stage:
        stage = canonical.get(args.list_stage.strip().lower())
        if not stage:
            print(f"ERROR: unknown stage {args.list_stage!r}; "
                  f"expected one of {', '.join(COMPLETABLE_STAGES)}")
            return 2
        struck = client.get_struck_rows()
        rows = [j for j in client.get_all_jobs()
                if (j.company or "").strip()
                and j.status == _stage_status(stage)
                and j.row_number not in struck]
        done = [j for j in rows if stage in split_stages(j.completed_stages)]
        todo = [j for j in rows if stage not in split_stages(j.completed_stages)]
        print(f"{stage}: {len(done)}/{len(rows)} done\n")
        if todo:
            print("to do:")
            for j in todo:
                print(f"  row {j.row_number}: {j.company} - {j.position}")
        if done:
            print("\ndone:")
            for j in done:
                print(f"  row {j.row_number}: {j.company} - {j.position}")
        return 0

    if args.row is None or args.stage is None:
        parser.error("row and stage are required (or use --list STAGE)")

    stage = canonical.get(args.stage.strip().lower())
    if not stage:
        print(f"ERROR: unknown stage {args.stage!r}; "
              f"expected one of {', '.join(COMPLETABLE_STAGES)}")
        return 2

    jobs = {j.row_number: j for j in client.get_all_jobs()}
    job = jobs.get(args.row)
    if job is None or not (job.company or "").strip():
        print(f"ERROR: row {args.row} is not a job row")
        return 2

    before = job.completed_stages or ""
    after = remove_stage(before, stage) if args.undo else add_stage(before, stage)

    print(f"Row {args.row}: {job.company} - {job.position}")
    print(f"  status          : {job.status}")
    print(f"  completed before: {before or '(none)'}")
    print(f"  completed after : {after or '(none)'}")

    if after == before:
        print(f"\nNothing to do - {stage} is already "
              f"{'absent' if args.undo else 'recorded'}.")
        return 0

    if args.dry_run:
        print("\n(dry run - nothing written)")
        return 0

    client.update_job(args.row, {"completed_stages": after})
    verb = "Removed" if args.undo else "Marked"
    print(f"\n{verb} {stage} on row {args.row}.")
    logger.info(f"{verb} {stage} on row {args.row} ({job.company})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

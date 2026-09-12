#!/usr/bin/env python3
"""
Daily Promotions-tab check with a Discord digest.

The Gmail checker never reads the Promotions tab, on purpose. This script does, once a
day, and reports two things: mail that is about an application on the sheet, and mail
it could not decide about - subject and date, for the user to look at. General marketing
is dropped. Nothing here writes to the sheet; the user moves a reported email to
Primary and check_gmail.py takes it from there.

Usage:
    python scripts/check_promotions.py             # scan the last 25 hours, post to Discord
    python scripts/check_promotions.py --dry-run   # print instead of posting; record nothing
    python scripts/check_promotions.py --hours 72  # widen the window (e.g. after an outage)

Scheduled daily at 17:00 by install_service.py.
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from check_gmail import GmailChecker
from src.config import get_config
from src.gmail import get_gmail_clients
from src.logging_config import get_logger, setup_logging
from src.notifications import DiscordSender
from src.promotions_scan import SeenStore, build_message, load_prompt, scan_account


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the Promotions tab for mail about your applications.")
    parser.add_argument("--dry-run", action="store_true", help="Print the digest instead of posting it; record nothing.")
    parser.add_argument("--hours", type=int, default=25, help="How far back to look (default 25, so daily runs overlap).")
    parser.add_argument("--account", default=None, help="Only this configured account (default: all).")
    args = parser.parse_args()

    config = get_config()
    setup_logging("promotions", config, console=False)
    logger = get_logger(__name__)

    if args.dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    now = datetime.now()
    since = now - timedelta(hours=args.hours)
    logger.info(f"Promotions check: window [{since:%Y-%m-%d %H:%M} .. {now:%Y-%m-%d %H:%M}]"
                + (" (dry run)" if args.dry_run else ""))

    clients = get_gmail_clients(config)
    if args.account:
        clients = [c for c in clients if c.account.lower() == args.account.lower()]
        if not clients:
            logger.error(f"No configured account matches {args.account!r}")
            return 1

    # The checker is used only for its row matcher, so a Promotions email meets the
    # same requisition-id, subsidiary, tie and struck-row rules as Primary mail.
    checker = GmailChecker(config)
    try:
        checker._struck_rows = checker.sheets_client.get_struck_rows()
    except Exception as e:
        logger.warning(f"Could not read struck rows, treating none as struck: {e}")
        checker._struck_rows = set()

    template = load_prompt(config.prompts_dir)
    findings = []
    for client in clients:
        store = SeenStore(config.data_dir, client.account, now=now)
        try:
            findings.extend(scan_account(
                client, checker.classifier, template, checker._find_matching_job,
                since, store, dry_run=args.dry_run,
            ))
        except Exception as e:
            logger.error(f"[{client.label}] Promotions scan failed: {e}")

    message = build_message(findings, now)
    matched = sum(1 for f in findings if f.bucket == "matched")
    logger.info(f"Findings: {len(findings)} ({matched} matched, {len(findings) - matched} to review)")

    if args.dry_run:
        print(message or "(nothing to report)")
        return 0
    if not message:
        logger.info("Nothing to report; no Discord message sent.")
        return 0
    if DiscordSender(config).send_message(message):
        logger.info("Promotions digest sent.")
        return 0
    logger.error("Failed to send the Promotions digest.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

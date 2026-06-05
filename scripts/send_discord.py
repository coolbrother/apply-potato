#!/usr/bin/env python3
"""
Send a Discord notification via webhook.

Usage:
    python scripts/send_discord.py --message "..." [--webhook https://...]

If --webhook is omitted, reads DISCORD_WEBHOOK_URL from .env (via config).
Exit 0 on success, 1 on failure.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from src.config import get_config
from src.logging_config import setup_logging, get_logger


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", required=True, help="Message text to send")
    parser.add_argument("--webhook", default="", help="Discord webhook URL (overrides config)")
    args = parser.parse_args()

    cfg = get_config()
    setup_logging("send_discord", cfg, console=False)
    logger = get_logger(__name__)

    webhook_url = args.webhook or cfg.discord.form_fill_webhook_url

    if not webhook_url:
        logger.error("no webhook URL provided and DISCORD_WEBHOOK_URL not set in .env")
        sys.exit(1)

    try:
        resp = httpx.post(webhook_url, json={"content": args.message}, timeout=10.0)
        resp.raise_for_status()
        logger.info("Discord notification sent.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Error sending Discord notification: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

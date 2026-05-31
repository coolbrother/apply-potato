"""
Discord notification module for ApplyPotato.
Sends Discord alerts via webhook when dream company jobs are found or status changes.
"""

import logging
from typing import List, Optional

import httpx

from .config import get_config, Config


logger = logging.getLogger(__name__)

_LEGAL_SUFFIXES = {
    " llc", " inc", " inc.", " corp", " corp.", " co.", " ltd",
    " limited", " technologies", " technology", " corporation",
}


def _normalize_company(name: str) -> str:
    """Lowercase, strip commas, and remove common legal suffixes."""
    s = name.lower().strip().replace(",", "")
    for suffix in _LEGAL_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    return s


def is_dream_company(
    company: str,
    dream_companies: List[str],
    salary_min: Optional[float] = None,
    salary_max: Optional[float] = None,
    salary_period: Optional[str] = None,
    min_salary_annual: Optional[float] = None,
    min_salary_hourly: Optional[float] = None,
) -> bool:
    """
    Check if a job qualifies as a "Dream Company" job.

    A job is a Dream Company job if either:
    - The company is in the user's dream companies list (exact match), OR
    - The job's salary meets the configured threshold

    Salary params are optional — if omitted, only the name list is checked.
    """
    if not company:
        return False
    company_norm = _normalize_company(company)
    if dream_companies and any(_normalize_company(d) == company_norm for d in dream_companies):
        return True
    # Only check salary threshold if thresholds are actually configured
    if min_salary_annual or min_salary_hourly:
        return meets_salary_threshold(salary_min, salary_max, salary_period, min_salary_annual, min_salary_hourly)
    return False


def meets_salary_threshold(
    salary_min: Optional[float],
    salary_max: Optional[float],
    salary_period: Optional[str],
    config_annual: Optional[float],
    config_hourly: Optional[float],
) -> bool:
    """
    Returns True if the job salary can be confirmed to meet the configured threshold.

    No threshold configured → True (passes).
    Missing salary info → False (can't confirm, don't notify).
    Unknown period → assume yearly.
    """
    if not config_annual and not config_hourly:
        return False  # No threshold configured
    if salary_min is None and salary_max is None:
        return False  # No salary info — can't confirm threshold met

    salary = salary_max or salary_min
    if not salary:
        return False

    period = (salary_period or "yearly").lower()
    if period == "hourly":
        annual = salary * 2080
        hourly = salary
    elif period == "monthly":
        annual = salary * 12
        hourly = annual / 2080
    else:  # yearly or unknown
        annual = salary
        hourly = salary / 2080

    if config_annual and annual >= config_annual:
        return True
    if config_hourly and hourly >= config_hourly:
        return True
    return False


class DiscordSender:
    """Client for sending messages via Discord webhook."""

    def __init__(self, config: Optional[Config] = None):
        """
        Initialize Discord sender.

        Args:
            config: Optional config object. Uses global config if not provided.
        """
        self.config = config or get_config()

    def send_message(self, content: str) -> bool:
        """
        Send a message to Discord via webhook.

        Args:
            content: Message content

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.config.discord.enabled:
            logger.debug("Discord notifications disabled")
            return False

        webhook_url = self.config.discord.webhook_url
        if not webhook_url:
            logger.debug("No Discord webhook URL configured")
            return False

        try:
            response = httpx.post(
                webhook_url,
                json={"content": content},
                timeout=10.0
            )
            response.raise_for_status()
            logger.debug("Discord message sent")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Discord webhook error: {e.response.status_code}")
            return False
        except Exception as e:
            logger.error(f"Failed to send Discord message: {e}")
            return False


# Singleton instance
_sender: Optional[DiscordSender] = None


def get_discord_sender() -> DiscordSender:
    """Get the global DiscordSender instance."""
    global _sender
    if _sender is None:
        _sender = DiscordSender()
    return _sender


def notify_dream_company_job(company: str, position: str, url: str = "") -> bool:
    """
    Send Discord notification for a new dream company job.

    Returns:
        True if message was sent successfully
    """
    config = get_config()
    if not config.discord.enabled:
        return False

    message = f"───────────────\n\n🚀 **New Dream Company Job!**\n**{company}** - {position}"
    if url:
        message += f"\n{url}"

    sender = get_discord_sender()
    success = sender.send_message(message)

    if success:
        logger.info("Discord notification sent for new job")

    return success


def notify_status_change(company: str, position: str, new_status: str, url: str = "") -> bool:
    """
    Send Discord notification when a dream company job status changes.

    Returns:
        True if message was sent successfully
    """
    config = get_config()
    if not config.discord.enabled:
        return False

    # Choose emoji based on status
    emoji_map = {
        "Applied": "📝",
        "OA": "💻",
        "Phone": "📞",
        "Technical": "🔧",
        "Offer": "🎉",
        "Rejected": "❌",
    }
    emoji = emoji_map.get(new_status, "📋")

    # Format message
    # Separator at start creates space after previous message's preview card
    message = f"───────────────\n\n{emoji} **Status Update**\n**{company}** - {position} → **{new_status}**"
    if url:
        message += f"\n{url}"

    sender = get_discord_sender()
    success = sender.send_message(message)

    if success:
        logger.info(f"Discord notification sent for status change: {new_status}")

    return success

"""
Discord notification module for ApplyPotato.
Sends Discord alerts via webhook when dream company jobs are found or status changes.
"""

import logging
from typing import Optional

import httpx
from rapidfuzz import fuzz

from .config import get_config, Config


logger = logging.getLogger(__name__)


def is_dream_company(company: str, dream_companies: list, threshold: int) -> bool:
    """
    Check if a company matches any dream company using fuzzy matching.

    Args:
        company: Company name from job listing
        dream_companies: List of dream company names
        threshold: Fuzzy match threshold (0-100)

    Returns:
        True if company matches any dream company
    """
    if not company or not dream_companies:
        return False

    company_lower = company.lower().strip()
    for dream in dream_companies:
        dream_lower = dream.lower().strip()
        # Substring match (fast path) - handles "Google" matching "Google LLC"
        if dream_lower in company_lower or company_lower in dream_lower:
            return True
        # Fuzzy match (handles typos and variations like "Qualcomm" vs "Qualcomm Inc.")
        if fuzz.ratio(company_lower, dream_lower) >= threshold:
            return True
        # Token-based matching for word-level similarity
        # Avoids false positives like "Apple" matching "Applied Concepts"
        if fuzz.token_set_ratio(company_lower, dream_lower) >= threshold:
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

    Args:
        company: Company name
        position: Job position/title
        url: Job posting URL

    Returns:
        True if message was sent successfully
    """
    config = get_config()
    if not config.discord.enabled:
        return False

    # Format message with emoji for visibility
    # Separator at start creates space after previous message's preview card
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

    Args:
        company: Company name
        position: Job position/title
        new_status: New status (OA, Phone, Technical, Offer, Rejected)
        url: Job posting URL

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

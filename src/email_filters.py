"""
Email privacy filters for ApplyPotato.
Content-based filtering to protect sensitive information.
"""

import re
import logging
from typing import Tuple

from .gmail import EmailMessage


logger = logging.getLogger(__name__)


# =============================================================================
# Content Safety Scan
# =============================================================================

# Patterns for sensitive information
SENSITIVE_PATTERNS = [
    # Credit card numbers (various formats)
    (r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b",
     "credit card number"),
    # Credit card with spaces/dashes
    (r"\b(?:\d{4}[-\s]?){3}\d{4}\b", "credit card format"),
    # SSN (requires context words to avoid false positives on job IDs, etc.)
    (r"(?:ssn|social.?security).{0,20}\d{3}[-\s]?\d{2}[-\s]?\d{4}\b", "SSN format"),
    # OTP/verification codes with context
    (r"(?:verification|security|one.?time|otp|2fa|mfa).{0,20}(?:code|pin).{0,10}[:\s]+\d{4,8}\b",
     "OTP/verification code"),
    # Passcode/login code/access code
    (r"(?:passcode|login.?code|access.?code).{0,10}[:\s]+\d{4,8}\b", "passcode/login code"),
    # Password reset links (generic patterns)
    (r"(?:reset|change|update).{0,20}password", "password reset"),
    # Temporary password
    (r"(?:temporary|temp).{0,10}password", "temporary password"),
    # Bank account numbers (generic)
    (r"(?:account|routing).{0,10}(?:number|#).{0,10}\d{8,17}\b", "bank account number"),
]

# Compiled patterns for efficiency
_COMPILED_PATTERNS = [(re.compile(p, re.IGNORECASE), desc) for p, desc in SENSITIVE_PATTERNS]


def check_content_safety(email: EmailMessage) -> Tuple[bool, str]:
    """
    Scan email content for sensitive information.

    Returns:
        (is_safe, reason)
    """
    # Combine subject and body for scanning
    content = f"{email.subject}\n{email.body_text or ''}"

    for pattern, description in _COMPILED_PATTERNS:
        if pattern.search(content):
            return False, f"Sensitive content detected: {description}"

    return True, "Content passed safety scan"


def apply_privacy_filters(email: EmailMessage) -> Tuple[bool, str]:
    """
    Apply privacy filters to an email.

    Only checks content safety - no domain filtering.
    AI classifier decides if email is job-related.

    Returns:
        (should_process, reason)
    """
    safe, reason = check_content_safety(email)
    if not safe:
        logger.info(f"Email filtered (Content Safety): {reason}")
        return False, reason

    return True, "Passed privacy filters"

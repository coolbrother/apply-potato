"""
Email privacy filters for ApplyPotato.
Content-based filtering to protect sensitive information.
"""

import re
import logging
from typing import Tuple

from .gmail import EmailMessage, body_as_text


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
    # The same thing with the words the other way round: "the code for <product>
    # account login: 778899". The rule above needs "code" adjacent to "login", so a
    # sender that puts the product name between them was handing over a live code.
    # The gap is capped and may not cross a sentence or a line, which is what keeps
    # "log in to see the code of conduct" and "the dress code ... Login at reception"
    # out of it.
    (r"code[^.\n]{0,60}log\s?-?\s?in[^\d]{0,10}\d{4,8}\b", "login code"),
    # Temporary password
    (r"(?:temporary|temp).{0,10}password", "temporary password"),
    # Bank account numbers (generic)
    (r"(?:account|routing).{0,10}(?:number|#).{0,10}\d{8,17}\b", "bank account number"),
]

# Patterns matched against the subject only.
#
# A password reset is what an email is *about*, not something it happens to mention, and
# the difference only shows in the subject. An application confirmation that walks you
# through setting up a candidate account says "reset password" in passing, and scanning
# the body threw the whole email away — a row sat at New through an application that had
# really been made. Measured over 60 days of real inbox traffic, every genuine reset mail
# announced itself in its subject, so the subject alone loses none of them.
#
# Both word orders are needed: the verb form leads ("reset your password") and the noun
# form trails ("Password Reset"). Half the real ones used the noun form, and the old
# body-wide scan had been catching those by accident.
SUBJECT_PATTERNS = [
    (r"(?:reset|change|update).{0,20}password|password.{0,20}(?:reset|change)",
     "password reset"),
]

# Senders that never carry job mail: platforms the user's courses run on.
#
# This is not domain filtering in general — the AI decides what is job-related, and
# that stays. It is the one class of sender whose mail the classifier cannot be taught
# to ignore: a Gradescope receipt, "Successfully submitted to Homework 1", is a
# past-tense receipt for the user's own submission, which is exactly the stage_done
# signal, and the prompt deliberately does not require a recognised platform. Three
# homework receipts in two weeks were flagged as OA completions with no row to match,
# reached Discord as "Company not tracked", and — because an unmatched OA completion is
# left unprocessed for the invite to catch up — were re-classified on every run.
#
# Matched on the sender's domain, including subdomains, so a platform that mails from
# a regional host still counts.
COURSEWORK_SENDER_DOMAINS = (
    "gradescope.com",
)

# Compiled patterns for efficiency
_COMPILED_PATTERNS = [(re.compile(p, re.IGNORECASE), desc) for p, desc in SENSITIVE_PATTERNS]
_COMPILED_SUBJECT_PATTERNS = [
    (re.compile(p, re.IGNORECASE), desc) for p, desc in SUBJECT_PATTERNS
]


def check_content_safety(email: EmailMessage) -> Tuple[bool, str]:
    """
    Scan email content for sensitive information.

    Returns:
        (is_safe, reason)
    """
    subject = email.subject or ""

    for pattern, description in _COMPILED_SUBJECT_PATTERNS:
        if pattern.search(subject):
            return False, f"Sensitive content detected: {description}"

    # Combine subject and body for scanning. The body must be read through
    # body_as_text: an HTML-only message has an empty body_text, and half of real inbox
    # traffic is HTML-only, so scanning that field alone left the patterns below
    # inspecting nothing but the subject for every second email.
    content = f"{subject}\n{body_as_text(email)}"

    for pattern, description in _COMPILED_PATTERNS:
        if pattern.search(content):
            return False, f"Sensitive content detected: {description}"

    return True, "Content passed safety scan"


def is_coursework_sender(email: EmailMessage) -> Tuple[bool, str]:
    """
    Whether the email comes from a platform on COURSEWORK_SENDER_DOMAINS.

    Returns:
        (is_coursework, reason)
    """
    address = (email.sender_email or "").strip().lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    for platform in COURSEWORK_SENDER_DOMAINS:
        if domain == platform or domain.endswith("." + platform):
            return True, f"Coursework platform: {platform}"
    return False, ""


def apply_privacy_filters(email: EmailMessage) -> Tuple[bool, str]:
    """
    Apply privacy filters to an email.

    Checks content safety, and drops mail from the coursework platforms listed in
    COURSEWORK_SENDER_DOMAINS. No other domain filtering: the AI classifier decides
    whether an email is job-related.

    Returns:
        (should_process, reason)
    """
    coursework, reason = is_coursework_sender(email)
    if coursework:
        logger.info(f"Email filtered ({reason}): {email.subject}")
        return False, reason

    safe, reason = check_content_safety(email)
    if not safe:
        logger.info(f"Email filtered (Content Safety): {reason}")
        return False, reason

    return True, "Passed privacy filters"

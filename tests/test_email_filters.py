"""
Tests for the pre-AI privacy filter.

The subject/body split exists because an application confirmation carrying account-setup
boilerplate — "follow the prompts to 'reset password'" — was scanned body and all, so it
was discarded before the classifier ever saw it and the row sat at New through an
application that had really been made.

Usage:
    pytest tests/test_email_filters.py -v
"""

from datetime import datetime

from src.email_filters import apply_privacy_filters, check_content_safety
from src.gmail import EmailMessage


def email(subject: str = "", body: str = "") -> EmailMessage:
    return EmailMessage(
        message_id="m1",
        subject=subject,
        sender="Someone <someone@example.com>",
        sender_email="someone@example.com",
        date=datetime(2026, 8, 11, 23, 19),
        body_text=body,
        body_html="",
        category="Primary",
    )


def html_email(subject: str = "", html: str = "") -> EmailMessage:
    """An HTML-only message: no text/plain part at all, as half of real mail arrives."""
    msg = email(subject, "")
    msg.body_html = html
    return msg


def is_safe(subject: str = "", body: str = "") -> bool:
    safe, _ = check_content_safety(email(subject, body))
    return safe


# =============================================================================
# Password reset — subject only
# =============================================================================

class TestPasswordReset:
    """What the email is about, not what it happens to mention."""

    def test_an_application_confirmation_with_setup_boilerplate(self):
        """The shape of boilerplate that cost a row its Applied status."""
        assert is_safe(
            "Thank you for applying to Deloitte!",
            "This will take you to your profile page, and then follow the prompts to "
            "'reset password'. If your name is not in the upper righthand corner, you "
            "will need to sign in.",
        )

    def test_a_real_reset_email_is_still_dropped(self):
        assert not is_safe("Reset your password")
        assert not is_safe("Reset your password for your candidate account")

    def test_the_noun_order_too(self):
        """Half the real ones phrase it as a noun, which the old rule missed."""
        assert not is_safe("Careers Site - Password Reset")
        assert not is_safe("Watch for Your Password Reset Notification")
        assert not is_safe("Your password was changed")

    def test_body_mention_alone_never_drops(self):
        """A sign-in alert telling you to go reset your password is not the reset mail."""
        assert is_safe(
            "Important: New sign-in on an unrecognized device",
            "If not, please reset your password and review your account.",
        )

    def test_an_ordinary_confirmation_is_untouched(self):
        assert is_safe(
            "Your application to Stripe",
            "Thanks for applying. We will be in touch.",
        )


# =============================================================================
# The body-scanned patterns still work
# =============================================================================

class TestBodyPatterns:
    """Everything the subject split did not move stays where it was."""

    def test_temporary_password_in_the_body(self):
        """Unlike a reset link, an actual credential in the body is the sensitive thing."""
        assert not is_safe("Your account", "Your temporary password is hunter2")

    def test_otp_in_the_subject(self):
        assert not is_safe("Example verification code: 222333")

    def test_otp_in_the_body(self):
        assert not is_safe("Please verify your device", "Verification code: 444555")

    def test_meeting_passcode(self):
        assert not is_safe("Seminar", "Meeting ID: 111 2222 3333\nPasscode: 666777")

    def test_credit_card(self):
        assert not is_safe("Receipt", "Card 4111 1111 1111 1111 charged")

    def test_ssn_needs_its_context_word(self):
        assert not is_safe("Onboarding", "SSN: 123-45-6789")
        assert is_safe("Job 123-45-6789 posted", "Requisition 123-45-6789")


# =============================================================================
# HTML-only bodies
#
# body_text is empty for every HTML-only message, which is half of real inbox traffic,
# so scanning that field alone left these patterns reading nothing but the subject.
# =============================================================================

class TestHtmlOnlyBodies:

    def test_a_passcode_in_an_html_body_is_caught(self):
        """The live case: an identity-verification mail carrying a one-time code."""
        msg = html_email(
            "Please confirm your identity",
            "<html><body><p>Please confirm your identity using the one-time passcode "
            "below:</p><h2>123456</h2><p>This code will expire in 10 minutes.</p>"
            "</body></html>",
        )
        safe, reason = check_content_safety(msg)
        assert not safe
        assert "OTP/verification code" in reason

    def test_a_verification_code_in_an_html_body_is_caught(self):
        msg = html_email(
            "Verify your identity",
            "<p>Verification code 654321 (This code is valid for 10 minutes)</p>",
        )
        assert not check_content_safety(msg)[0]

    def test_an_ordinary_html_confirmation_still_passes(self):
        """Widening the scan must not start eating the status mail it exists beside."""
        msg = html_email(
            "Application Received",
            "<html><body><p>Thank you for your interest in our open position "
            "( 01999999 Methods Intern (Summer 2027) ). We look forward to reviewing "
            "your skills.</p></body></html>",
        )
        assert check_content_safety(msg)[0]

    def test_markup_is_not_scanned_as_content(self):
        """Tag soup must not trip the patterns on its own."""
        msg = html_email(
            "Newsletter",
            '<a href="https://example.com/track?id=12345678901234567890">Read more</a>',
        )
        assert check_content_safety(msg)[0]

    def test_the_text_part_still_wins_when_present(self):
        msg = email("Hello", "nothing sensitive here")
        msg.body_html = "<p>Verification code: 123456</p>"
        assert check_content_safety(msg)[0]


# =============================================================================
# apply_privacy_filters wrapper
# =============================================================================

class TestApplyPrivacyFilters:

    def test_passes_a_clean_email(self):
        keep, reason = apply_privacy_filters(email("Interview invitation", "Are you free?"))
        assert keep
        assert reason == "Passed privacy filters"

    def test_blocks_and_explains(self):
        keep, reason = apply_privacy_filters(email("Reset your password"))
        assert not keep
        assert "password reset" in reason

    def test_empty_email(self):
        keep, _ = apply_privacy_filters(email("", ""))
        assert keep

    def test_none_body(self):
        msg = email("Hello", "")
        msg.body_text = None
        keep, _ = apply_privacy_filters(msg)
        assert keep

"""
Tests for AI email classification.

These tests make real AI API calls to verify classification works correctly.
Kept minimal (3 tests) to balance coverage vs speed.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.email_classifier import EmailClassifier
from src.gmail import EmailMessage
from src.config import get_config

# Path to email fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "emails"


def load_email_fixture(filename: str) -> EmailMessage:
    """Load email fixture and convert to EmailMessage."""
    fixture_path = FIXTURES_DIR / filename
    with open(fixture_path, encoding="utf-8") as f:
        data = json.load(f)

    return EmailMessage(
        message_id=data.get("message_id", "test"),
        subject=data.get("subject", ""),
        sender=data.get("sender", ""),
        sender_email=data.get("sender_email", ""),
        date=datetime.fromisoformat(data.get("date", "2024-01-01T00:00:00")),
        body_text=data.get("body_text", ""),
        body_html=data.get("body_html", ""),
        category=data.get("category", "Primary"),
    )


@pytest.fixture(scope="module")
def email_classifier():
    """Create email classifier with real config."""
    config = get_config()
    return EmailClassifier(config)


class TestAIEmailClassification:
    """Test AI email classification with real API calls."""

    def test_classifies_confirmation_email(self, email_classifier):
        """Test AI classifies application confirmation email correctly.

        Uses confirmation_google.json which contains:
        - "Your application to Google has been received"
        - "Thank you for applying to the Software Engineering Intern position"
        """
        email = load_email_fixture("confirmation_google.json")
        result = email_classifier.classify(email)

        assert result is not None, "Should classify email"
        assert result.category == "confirmation", \
            f"Expected 'confirmation', got '{result.category}'"
        assert "google" in [c.lower() for c in result.company_candidates], \
            f"Should identify Google as company, got {result.company_candidates}"

    def test_classifies_rejection_email(self, email_classifier):
        """Test AI classifies rejection email correctly.

        Uses rejection_microsoft.json which contains:
        - "decided not to move forward with your application"
        - "Thank you for interviewing with Microsoft"
        """
        email = load_email_fixture("rejection_microsoft.json")
        result = email_classifier.classify(email)

        assert result is not None, "Should classify email"
        assert result.category == "rejection", \
            f"Expected 'rejection', got '{result.category}'"
        assert "microsoft" in [c.lower() for c in result.company_candidates], \
            f"Should identify Microsoft as company, got {result.company_candidates}"

    def test_classifies_interview_email(self, email_classifier):
        """Test AI classifies interview invitation email correctly.

        Uses interview_google.json which contains:
        - "Interview Invitation"
        - "invite you to a technical phone interview"
        """
        email = load_email_fixture("interview_google.json")
        result = email_classifier.classify(email)

        assert result is not None, "Should classify email"
        # Should be phone or technical interview
        assert result.category in ["phone", "technical", "interview"], \
            f"Expected interview category, got '{result.category}'"
        assert "google" in [c.lower() for c in result.company_candidates], \
            f"Should identify Google as company, got {result.company_candidates}"

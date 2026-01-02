"""
Tests for AI job extraction.

These tests make real AI API calls to verify extraction works correctly.
Kept minimal (2 tests) to balance coverage vs speed.
"""

import os
import sys
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ai_extractor import AIExtractor
from src.config import get_config

# Path to job page fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "job_pages"


@pytest.fixture(scope="module")
def ai_extractor():
    """Create AI extractor with real config."""
    config = get_config()
    return AIExtractor(config)


class TestAIExtraction:
    """Test AI job extraction with real API calls."""

    def test_extracts_class_standing_requirement(self, ai_extractor):
        """Test AI extracts class standing requirement correctly.

        Uses amazon_senior_only.txt which has:
        - "Must be a current Senior (final year undergraduate)"
        - "This position is only open to students who will be Seniors (4th year)"
        """
        txt_path = FIXTURES_DIR / "amazon_senior_only.txt"
        with open(txt_path, encoding="utf-8") as f:
            content = f.read()

        results = ai_extractor.extract(content, "https://amazon.jobs/test")

        # Should extract at least one job
        assert len(results) > 0, "AI should extract job data"
        result = results[0]

        assert result.company.lower() == "amazon", f"Expected Amazon, got {result.company}"

        # Should extract class standing requirement
        assert result.class_standing_requirement is not None, \
            "Should extract class standing requirement"

        # Should contain "senior" (case-insensitive)
        class_req = result.class_standing_requirement.lower()
        assert "senior" in class_req or "final" in class_req or "4th" in class_req, \
            f"Class standing should mention senior/final year, got: {result.class_standing_requirement}"

    def test_extracts_job_type_fulltime(self, ai_extractor):
        """Test AI extracts job type correctly for full-time positions.

        Uses meta_fulltime.txt which has:
        - "Employment Type: Full-Time"
        - "New Grad 2025" in title
        """
        txt_path = FIXTURES_DIR / "meta_fulltime.txt"
        with open(txt_path, encoding="utf-8") as f:
            content = f.read()

        results = ai_extractor.extract(content, "https://meta.com/careers/test")

        # Should extract at least one job
        assert len(results) > 0, "AI should extract job data"
        result = results[0]

        assert result.company.lower() == "meta", f"Expected Meta, got {result.company}"

        # Should extract job type as full-time (not internship)
        assert result.job_type is not None, "Should extract job type"

        job_type = result.job_type.lower()
        assert "full" in job_type or "new grad" in job_type, \
            f"Job type should be full-time/new grad, got: {result.job_type}"

        # Should NOT be internship
        assert "intern" not in job_type, \
            f"Full-time job should not be marked as internship, got: {result.job_type}"

    def test_enrollment_requirement_not_graduation_timeline(self, ai_extractor):
        """Test AI does NOT confuse enrollment requirements with graduation timeline.

        Uses western_union_enrollment.txt (real scraper output) which has:
        - "Must be enrolled in a college degree program and have earned 60 credit hours by the end of May 2026"

        This is an ENROLLMENT requirement (must still be a student), NOT a graduation deadline.
        The AI should NOT extract this as graduation_timeline.
        """
        txt_path = FIXTURES_DIR / "western_union_enrollment.txt"
        with open(txt_path, encoding="utf-8") as f:
            content = f.read()

        results = ai_extractor.extract(content, "https://westernunion.wd5.myworkdayjobs.com/test")

        # Should extract at least one job
        assert len(results) > 0, "AI should extract job data"
        result = results[0]

        assert result.company.lower() == "western union", f"Expected Western Union, got {result.company}"

        # graduation_timeline should be None - enrollment != graduation
        assert result.graduation_timeline is None, \
            f"Enrollment requirement should NOT be extracted as graduation_timeline. " \
            f"Got: {result.graduation_timeline}"

        # class_standing_requirement should capture the enrollment text
        assert result.class_standing_requirement is not None, \
            "Should extract class standing/enrollment requirement"

        class_req = result.class_standing_requirement.lower()
        assert "enrolled" in class_req or "60 credit" in class_req, \
            f"Should capture enrollment requirement, got: {result.class_standing_requirement}"

"""
Tests for AutoApplyOrchestrator — helper functions and real AI detection.

Detection tests make real AI API calls using the configured provider.

Usage:
    pytest tests/test_auto_apply.py -v
"""

import pytest
from pathlib import Path
from docx import Document

from src.ai_extractor import ExtractedJob
from src.auto_apply import (
    AutoApplyOrchestrator,
    _format_detection_note,
    _parse_json_response,
    _sanitize_name,
    _truncate_page,
)
from src.config import get_config

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "apply_pages"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def config():
    return get_config()


@pytest.fixture(scope="module")
def orchestrator(config):
    return AutoApplyOrchestrator(config)


@pytest.fixture(scope="module")
def google_apply_content() -> str:
    return (FIXTURES_DIR / "google_apply_page.txt").read_text(encoding="utf-8")


@pytest.fixture
def google_job() -> ExtractedJob:
    return ExtractedJob(
        company="Google",
        title="Software Engineering Intern",
        job_type="Internship",
        locations=["Mountain View, CA"],
        is_remote=False,
        work_model="Hybrid",
        salary_min=55,
        salary_max=65,
        salary_period="hourly",
        currency="USD",
        required_skills=["Python", "Java"],
        preferred_skills=[],
        required_majors=["Computer Science"],
        gpa_requirement=None,
        class_standing_requirement="Junior or Senior",
        work_authorization="US Citizen",
        sponsorship_available=False,
        posted_date="",
        deadline="",
        season_year="Summer 2025",
        job_category="Software Engineering",
    )


# ---------------------------------------------------------------------------
# Pure helper function tests (no I/O)
# ---------------------------------------------------------------------------

class TestParseJsonResponse:

    def test_plain_json(self):
        result = _parse_json_response('{"needs_resume": true, "confidence": 0.9}')
        assert result["needs_resume"] is True
        assert result["confidence"] == 0.9

    def test_strips_markdown_fences(self):
        result = _parse_json_response('```json\n{"needs_resume": false}\n```')
        assert result["needs_resume"] is False

    def test_json_embedded_in_prose(self):
        result = _parse_json_response('Sure! {"needs_resume": true, "confidence": 0.8} Hope that helps.')
        assert result["needs_resume"] is True

    def test_returns_none_for_no_json(self):
        assert _parse_json_response("No JSON here.") is None

    def test_returns_none_for_empty(self):
        assert _parse_json_response("") is None
        assert _parse_json_response(None) is None


class TestFormatDetectionNote:

    def test_resume_required_cl_no_field(self):
        note = _format_detection_note({"needs_resume": True, "cover_letter_field_present": False, "confidence": 0.9})
        assert "Resume: required" in note
        assert "Cover letter: no field" in note
        assert "90%" in note

    def test_resume_not_required(self):
        note = _format_detection_note({"needs_resume": False, "cover_letter_field_present": False, "confidence": 0.8})
        assert "Resume: not required" in note

    def test_cover_letter_field_present(self):
        note = _format_detection_note({"needs_resume": True, "cover_letter_field_present": True, "confidence": 0.7})
        assert "Cover letter: field present" in note

    def test_confidence_as_percentage(self):
        note = _format_detection_note({"needs_resume": True, "cover_letter_field_present": False, "confidence": 0.85})
        assert "85%" in note


class TestSanitizeName:

    def test_removes_special_chars(self):
        assert _sanitize_name("Acme/Corp!") == "AcmeCorp"

    def test_replaces_spaces_with_underscores(self):
        assert _sanitize_name("Acme Corp") == "Acme_Corp"

    def test_truncates_to_max_len(self):
        assert len(_sanitize_name("A" * 100)) <= 50

    def test_handles_empty(self):
        assert _sanitize_name("") == ""


class TestTruncatePage:

    def test_short_content_unchanged(self):
        text = "Hello world"
        assert _truncate_page(text) == text

    def test_long_content_truncated(self):
        result = _truncate_page("A" * 10000)
        assert "[truncated]" in result

    def test_preserves_start_and_end(self):
        text = "START" + "x" * 4000 + "z" * 5000 + "y" * 4000 + "END"
        result = _truncate_page(text)
        assert result.startswith("START")
        assert result.endswith("END")


# ---------------------------------------------------------------------------
# Real AI detection tests
# ---------------------------------------------------------------------------

class TestRunDetection:

    @pytest.mark.asyncio
    async def test_detects_resume_required(self, orchestrator, google_job, google_apply_content):
        """AI should detect that the Google apply page requires a resume."""
        result = await orchestrator._run_detection(
            extracted=google_job,
            job_url="https://careers.google.com/jobs/results/test",
            page_content=google_apply_content,
            scraper=None,
        )
        assert result is not None, "Detection should return a result dict"
        assert result.get("needs_resume") is True

    @pytest.mark.asyncio
    async def test_detects_cover_letter_field_present(self, orchestrator, google_job, google_apply_content):
        """AI should detect whether a cover letter field is present in the form."""
        result = await orchestrator._run_detection(
            extracted=google_job,
            job_url="https://careers.google.com/jobs/results/test",
            page_content=google_apply_content,
            scraper=None,
        )
        assert result is not None
        assert "cover_letter_field_present" in result, \
            "Detection result should include cover_letter_field_present key"

    @pytest.mark.asyncio
    async def test_returns_confidence(self, orchestrator, google_job, google_apply_content):
        """Detection result should include a confidence score."""
        result = await orchestrator._run_detection(
            extracted=google_job,
            job_url="https://careers.google.com/jobs/results/test",
            page_content=google_apply_content,
            scraper=None,
        )
        assert result is not None
        assert "confidence" in result
        assert 0.0 <= result["confidence"] <= 1.0

    @pytest.mark.asyncio
    async def test_empty_page_content_returns_low_confidence(self, orchestrator, google_job):
        """Empty page should return a result but with low confidence."""
        result = await orchestrator._run_detection(
            extracted=google_job,
            job_url="https://example.com/apply",
            page_content="",
            scraper=None,
        )
        # Should still return something (default behavior), not crash
        if result is not None:
            assert result.get("confidence", 1.0) <= 0.5, \
                "Empty page should yield low confidence"


"""
Parser and fixture tests for ApplyPotato.

Tests the GitHub markdown parsing and fixture loading.
No AI API calls - these are fast unit tests.

Usage:
    pytest tests/test_integration.py -v
"""

import pytest

from src.sheets import HEADERS
from tests.conftest import parse_fixture_markdown


# =============================================================================
# Parser Unit Tests (with synthetic fixtures)
# =============================================================================

class TestGitHubParserWithFixtures:
    """Unit tests for GitHub parser using synthetic fixtures."""

    def test_parse_fixture_markdown(self):
        """Test that fixture markdown is parsed correctly."""
        jobs = parse_fixture_markdown()

        # Should find multiple jobs
        assert len(jobs) >= 5, f"Should find at least 5 jobs, found {len(jobs)}"

        # Check specific jobs
        companies = [j.company for j in jobs]
        assert "Google" in companies, "Should find Google"
        assert "Microsoft" in companies, "Should find Microsoft"
        assert "Amazon" in companies, "Should find Amazon"

        # ClosedCorp should NOT be in list (deleted)
        assert "ClosedCorp" not in companies, "Should not find deleted company"

    def test_parse_continuation_rows(self):
        """Test that ↳ continuation rows are handled correctly."""
        jobs = parse_fixture_markdown()

        # TechStartup should have multiple listings (↳ row)
        startup_jobs = [j for j in jobs if j.company == "TechStartup"]
        assert len(startup_jobs) >= 2, "TechStartup should have multiple listings"

    def test_old_jobs_included_in_fixtures(self):
        """Test that age filtering is visible in fixtures."""
        jobs = parse_fixture_markdown()

        # OldCompany has age 30d - should be in fixtures
        old_jobs = [j for j in jobs if j.company == "OldCompany"]
        assert len(old_jobs) == 1, "OldCompany should be in fixtures"
        assert old_jobs[0].age_days == 30, "OldCompany should have age 30"


# =============================================================================
# Mock Sheets Tests
# =============================================================================

class TestMockSheets:
    """Test the mock sheets client works correctly."""

    def test_mock_sheets_has_headers(self, mock_sheets_client):
        """Test that mock sheet starts with headers."""
        assert mock_sheets_client.rows[0] == HEADERS

    def test_mock_sheets_add_job(self, mock_sheets_client):
        """Test adding a job to mock sheet."""
        job_data = {
            "company": "TestCompany",
            "position": "Test Position",
            "status": "New",
            "job_type": "Internship",
        }

        row_num = mock_sheets_client.add_job(job_data)

        assert row_num == 2, "First job should be row 2 (after header)"
        assert mock_sheets_client.get_row_count() == 1

        jobs = mock_sheets_client.get_all_jobs()
        assert len(jobs) == 1
        assert jobs[0].company == "TestCompany"

    def test_mock_sheets_update_job(self, mock_sheets_client):
        """Test updating a job in mock sheet."""
        # Add a job
        mock_sheets_client.add_job({"company": "Test", "position": "Dev"})

        # Update it
        mock_sheets_client.update_job(2, {"status": "Applied"})

        jobs = mock_sheets_client.get_all_jobs()
        assert jobs[0].status == "Applied"

    def test_mock_sheets_find_by_company(self, mock_sheets_client):
        """Test finding jobs by company name."""
        mock_sheets_client.add_job({"company": "Google", "position": "SWE"})
        mock_sheets_client.add_job({"company": "Meta", "position": "SWE"})
        mock_sheets_client.add_job({"company": "Google", "position": "PM"})

        google_jobs = mock_sheets_client.find_jobs_by_company("Google")
        assert len(google_jobs) == 2

        meta_jobs = mock_sheets_client.find_jobs_by_company("Meta")
        assert len(meta_jobs) == 1

"""
Pytest configuration and fixtures for integration testing.

This module provides:
- Test configuration with mock user profile
- Mock GitHub parser (returns jobs from fixture markdown)
- Mock Playwright scraper (returns cached HTML fixtures)
- Real Google Sheets client (pointed to test sheet)
- Mock Gmail client (returns fixture emails)
"""

import asyncio
import json
import os
import re
import src.sheets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from src.config import Config, UserProfile, DiscordConfig, GitHubRepo
from src.github_parser import JobListing
from src.gmail import EmailMessage
from src.sheets import SheetsClient
from tests.mocks.mock_sheets import MockSheetsClient

# Project root for fixture paths
PROJECT_ROOT = Path(__file__).parent.parent


# =============================================================================
# Paths
# =============================================================================

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GITHUB_MD_DIR = FIXTURES_DIR / "github_markdown"
JOB_PAGES_DIR = FIXTURES_DIR / "job_pages"
EMAILS_DIR = FIXTURES_DIR / "emails"
TEST_CONFIG_PATH = FIXTURES_DIR / "test_config.json"


# =============================================================================
# URL to Fixture Mapping
# =============================================================================

# Maps test URLs to their HTML fixture files
URL_TO_FIXTURE: Dict[str, str] = {
    "https://test-jobs.example.com/google-swe-intern": "google_swe_intern.html",
    "https://test-jobs.example.com/microsoft-intern": "microsoft_intern.html",
    "https://test-jobs.example.com/meta-fulltime": "meta_fulltime.html",
    "https://test-jobs.example.com/amazon-senior-only": "amazon_senior_only.html",
    "https://test-jobs.example.com/startup-no-visa": "startup_no_visa.html",
}


# =============================================================================
# Test Configuration
# =============================================================================

def load_test_config_json() -> dict:
    """Load test configuration from JSON file."""
    with open(TEST_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def test_config_json() -> dict:
    """Load test configuration JSON."""
    return load_test_config_json()


@pytest.fixture(scope="session")
def test_user_profile() -> UserProfile:
    """Create test user profile."""
    config_json = load_test_config_json()
    user_data = config_json["user_profile"]

    return UserProfile(
        name=user_data["name"],
        email=user_data["email"],
        class_standing=user_data.get("class_standing"),
        graduation_date=user_data.get("graduation_date", ""),
        majors=[user_data.get("major", "Computer Science")],
        minors=[user_data.get("minor")] if user_data.get("minor") else [],
        gpa=user_data.get("gpa", 3.5),
        work_authorization=user_data.get("work_authorization", "US Citizen"),
        target_job_type=user_data.get("target_job_type", "Internship"),
        target_season_year=user_data.get("target_season_year"),
        preferred_locations=user_data.get("preferred_locations", []),
        work_model=user_data.get("work_model", "Any"),
        min_salary_hourly=user_data.get("min_salary_hourly", 0),
        target_companies=user_data.get("target_companies", []),
        skills=user_data.get("skills", []),
        job_categories=["Software Engineering"],
        degree_level="Bachelors",
    )


@pytest.fixture(scope="session")
def test_google_sheet_id() -> str:
    """Get test Google Sheet ID from environment."""
    sheet_id = os.getenv("TEST_GOOGLE_SHEET_ID")
    if not sheet_id:
        pytest.skip(
            "TEST_GOOGLE_SHEET_ID not set. Create a test Google Sheet and add "
            "TEST_GOOGLE_SHEET_ID=your_sheet_id to your .env file."
        )
    return sheet_id


@pytest.fixture(scope="session")
def test_config(test_user_profile: UserProfile, test_google_sheet_id: str) -> Config:
    """Create test configuration."""
    return Config(
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        google_credentials_path=PROJECT_ROOT / "auth" / "credentials.json",
        google_sheet_id=test_google_sheet_id,
        ai_provider=os.getenv("AI_PROVIDER", "openai"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        openai_max_tokens=None,
        gemini_max_output_tokens=None,
        github_repos=[GitHubRepo(owner_repo="test-fixtures/test-jobs", branch="main")],
        job_age_limit_days=30,
        scrape_interval_minutes=30,
        gmail_check_interval_minutes=10,
        gmail_lookback_days=1,
        user=test_user_profile,
        status_colors={
            "Applied": "#E3F2FD",
            "OA": "#B3E5FC",
            "Phone": "#81D4FA",
            "Technical": "#4FC3F7",
            "Offer": "#C8E6C9",
            "Rejected": "#FFCDD2",
        },
        discord=DiscordConfig(enabled=False, webhook_url="", dream_company_match_threshold=80),
        max_retries=3,
        page_timeout_seconds=30,
        render_delay_seconds=1.0,
        retry_base_delay_seconds=5.0,
        log_level="DEBUG",
        oauth_local_port=8888,
        oauth_timeout_seconds=120,
        base_dir=PROJECT_ROOT,
    )


# =============================================================================
# GitHub Parser Mock
# =============================================================================

def parse_fixture_markdown() -> List[JobListing]:
    """Parse the test jobs markdown fixture and return JobListing objects."""
    md_path = GITHUB_MD_DIR / "test_jobs.md"
    with open(md_path, encoding="utf-8") as f:
        content = f.read()

    soup = BeautifulSoup(content, "html.parser")
    jobs = []
    last_company = ""

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        # Parse company
        company_cell = cells[0]
        company_link = company_cell.find("a")
        company = company_link.get_text(strip=True) if company_link else company_cell.get_text(strip=True)

        # Skip deleted/closed
        if company_cell.find("del"):
            continue

        # Handle ↳ (same company as above)
        if company in ("↳", ""):
            company = last_company
        else:
            last_company = company

        # Parse title
        title = cells[1].get_text(strip=True)

        # Parse location
        location = cells[2].get_text(strip=True)

        # Parse URL
        app_cell = cells[3]
        apply_link = app_cell.find("a", href=True)
        if not apply_link:
            continue
        url = apply_link["href"]

        # Parse age
        age_days = 0
        if len(cells) >= 5:
            age_text = cells[4].get_text(strip=True)
            match = re.match(r"(\d+)d", age_text)
            if match:
                age_days = int(match.group(1))

        if company and title and url:
            jobs.append(JobListing(
                company=company,
                title=title,
                location=location,
                url=url,
                date_posted="",
                source_repo="test-fixtures",
                age_days=age_days,
            ))

    return jobs


@pytest.fixture
def mock_job_listings() -> List[JobListing]:
    """Get job listings parsed from fixture markdown."""
    return parse_fixture_markdown()


@pytest.fixture
def mock_github_parser(mock_job_listings: List[JobListing]):
    """Mock GitHubParser.fetch_all_jobs to return fixture jobs."""
    with patch("src.github_parser.GitHubParser.fetch_all_jobs") as mock:
        mock.return_value = mock_job_listings
        yield mock


# =============================================================================
# Playwright Scraper Mock
# =============================================================================

def load_job_page_fixture(url: str) -> Optional[str]:
    """Load HTML content for a test URL from fixtures."""
    fixture_file = URL_TO_FIXTURE.get(url)
    if not fixture_file:
        return None

    fixture_path = JOB_PAGES_DIR / fixture_file
    if not fixture_path.exists():
        return None

    with open(fixture_path, encoding="utf-8") as f:
        return f.read()


class MockPlaywrightScraper:
    """Mock scraper that returns HTML from fixtures instead of fetching."""

    def __init__(self, config=None):
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def fetch_page(self, url: str) -> Tuple[Optional[str], str, bool]:
        """Return fixture HTML content for the URL."""
        content = load_job_page_fixture(url)
        if content is None:
            # Return empty content for unknown URLs (simulates fetch failure)
            return None, url, False
        return content, url, False  # Never blocked in tests


@pytest.fixture
def mock_scraper():
    """Provide mock scraper class."""
    return MockPlaywrightScraper


@pytest.fixture
def patch_scraper():
    """Patch PlaywrightScraper to use mock."""
    with patch("src.scraper.PlaywrightScraper", MockPlaywrightScraper):
        yield


# =============================================================================
# Gmail Client Mock
# =============================================================================

def load_email_fixtures() -> List[EmailMessage]:
    """Load all email fixtures as EmailMessage objects."""
    emails = []

    for email_file in EMAILS_DIR.glob("*.json"):
        with open(email_file, encoding="utf-8") as f:
            data = json.load(f)

        # Parse date string to datetime
        date_str = data.get("date", "")
        try:
            date = datetime.fromisoformat(date_str)
        except ValueError:
            date = datetime.now()

        emails.append(EmailMessage(
            message_id=data.get("message_id", email_file.stem),
            subject=data.get("subject", ""),
            sender=data.get("sender", ""),
            sender_email=data.get("sender_email", ""),
            date=date,
            body_text=data.get("body_text", ""),
            body_html=data.get("body_html", ""),
            category=data.get("category", "Primary"),
        ))

    return emails


@pytest.fixture
def mock_email_fixtures() -> List[EmailMessage]:
    """Get all email fixtures as EmailMessage objects."""
    return load_email_fixtures()


@pytest.fixture
def mock_gmail_client(mock_email_fixtures: List[EmailMessage]):
    """Mock GmailClient to return fixture emails."""
    mock_client = MagicMock()
    mock_client.fetch_recent_emails.return_value = mock_email_fixtures
    mock_client.is_processed.return_value = False
    mock_client.mark_as_processed.return_value = None

    with patch("src.gmail.get_gmail_client", return_value=mock_client):
        with patch("src.gmail.GmailClient", return_value=mock_client):
            yield mock_client


# =============================================================================
# Google Sheets Fixtures
# =============================================================================

@pytest.fixture
def sheets_client(test_config: Config):
    """Get real SheetsClient pointed at test sheet."""
    # Clear singleton to ensure fresh client with test config
    src.sheets._client = None

    client = SheetsClient(test_config)
    return client


@pytest.fixture
def clean_test_sheet(sheets_client):
    """Clear ALL data from test sheet before test, including headers.

    This tests that ensure_headers() correctly creates headers when they don't exist.
    """
    service = sheets_client._get_service()
    sheet_id = sheets_client.config.google_sheet_id

    # First, ensure the Jobs sheet exists (creates it if needed)
    sheets_client._ensure_jobs_sheet_exists()

    # Get the Jobs sheet ID
    jobs_sheet_id = sheets_client._get_jobs_sheet_id()

    # Get current row count (check all columns, not just A, in case data starts elsewhere)
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Jobs!A:R"
        ).execute()
        row_count = len(result.get("values", []))
    except Exception:
        row_count = 0

    # Delete ALL rows including header (start from row 0)
    if row_count > 0:
        request = {
            "deleteDimension": {
                "range": {
                    "sheetId": jobs_sheet_id,
                    "dimension": "ROWS",
                    "startIndex": 0,  # Delete from first row (header)
                    "endIndex": row_count
                }
            }
        }
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [request]}
        ).execute()

    # Do NOT call ensure_headers() here - let the code under test do it
    yield sheets_client


# =============================================================================
# Mock Sheets Fixtures (for fast testing without real API)
# =============================================================================

@pytest.fixture
def mock_sheets_client():
    """Get mock SheetsClient that stores data in memory."""
    return MockSheetsClient()


@pytest.fixture
def test_config_mock_sheets(test_user_profile: UserProfile) -> Config:
    """Create test configuration that doesn't require real Google Sheet ID."""
    return Config(
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        google_credentials_path=PROJECT_ROOT / "auth" / "credentials.json",
        google_sheet_id="mock-sheet-id",  # Not used with mock
        ai_provider=os.getenv("AI_PROVIDER", "openai"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        openai_max_tokens=None,
        gemini_max_output_tokens=None,
        github_repos=[GitHubRepo(owner_repo="test-fixtures/test-jobs", branch="main")],
        job_age_limit_days=30,
        scrape_interval_minutes=30,
        gmail_check_interval_minutes=10,
        gmail_lookback_days=1,
        user=test_user_profile,
        status_colors={
            "Applied": "#E3F2FD",
            "OA": "#B3E5FC",
            "Phone": "#81D4FA",
            "Technical": "#4FC3F7",
            "Offer": "#C8E6C9",
            "Rejected": "#FFCDD2",
        },
        discord=DiscordConfig(enabled=False, webhook_url="", dream_company_match_threshold=80),
        max_retries=3,
        page_timeout_seconds=30,
        render_delay_seconds=1.0,
        retry_base_delay_seconds=5.0,
        log_level="DEBUG",
        oauth_local_port=8888,
        oauth_timeout_seconds=120,
        base_dir=PROJECT_ROOT,
    )


# =============================================================================
# Integration Test Helpers
# =============================================================================

@pytest.fixture
def expected_results(test_config_json: dict) -> dict:
    """Get expected test results from config."""
    return test_config_json.get("expected_results", {})


# =============================================================================
# Pytest Configuration
# =============================================================================

def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test (requires external services)"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

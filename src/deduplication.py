"""
Deduplication logic for job postings.
Uses job apply URL as the unique identifier.

Also tracks filtered-out jobs to avoid re-processing them.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from .config import Config, get_config
from .sheets import SheetsClient, get_sheets_client


logger = logging.getLogger(__name__)

# File to track filtered-out jobs (stored in data/ directory)
FILTERED_JOBS_FILENAME = "filtered_jobs.json"

# File to track seen source URLs (stored in data/ directory)
SEEN_SOURCES_FILENAME = "seen_sources.json"


# Tracking parameters to remove from URLs
TRACKING_PARAMS = {
    # UTM tracking
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    # Referral tracking
    "ref",
    "source",
    "src",  # Common source tracking
    "gh_src",  # Greenhouse source tracking (NOT gh_jid - that's the job ID!)
    # Common tracking params
    "fbclid",
    "gclid",
    "mc_eid",
    "mc_cid",
    "_ga",
    "_gl",
}


def normalize_url(url: str) -> str:
    """
    Normalize a URL for deduplication.

    Removes tracking parameters and normalizes format so the same
    job posting URL always produces the same key.

    Args:
        url: Raw URL from job posting

    Returns:
        Normalized URL string
    """
    if not url or not url.strip():
        return ""

    # Parse the URL
    parsed = urlparse(url.strip())

    # Normalize scheme to https (treat http and https as same)
    scheme = "https"
    netloc = parsed.netloc.lower()

    # Remove trailing slashes from path
    path = parsed.path.rstrip("/")

    # Strip /apply from Lever and Workable URLs (job description page has better content than apply page)
    if ("lever.co" in netloc or "workable.com" in netloc) and path.endswith("/apply"):
        path = path[:-6]  # Remove "/apply"

    # Parse query parameters and filter out tracking params
    query_params = parse_qs(parsed.query, keep_blank_values=False)
    filtered_params = {
        key: values
        for key, values in query_params.items()
        if key.lower() not in TRACKING_PARAMS
        and not key.lower().startswith("utm_")
        and not key.lower().startswith("rx_")  # Indeed/Radancy tracking
        and not key.lower().startswith("_")
    }

    # Rebuild query string (sorted for consistency)
    query = urlencode(filtered_params, doseq=True) if filtered_params else ""

    # Rebuild URL without fragment
    normalized = urlunparse((scheme, netloc, path, "", query, ""))

    return normalized


class DeduplicationChecker:
    """
    Checks for duplicate job postings using Google Sheets.

    Uses normalized job URLs to detect duplicates.
    """

    def __init__(self, config: Optional[Config] = None, sheets_client: Optional[SheetsClient] = None):
        """
        Initialize the deduplication checker.

        Args:
            config: Optional config object. Uses global config if not provided.
            sheets_client: Optional SheetsClient. Uses global client if not provided.
        """
        self.config = config or get_config()
        self._sheets_client = sheets_client
        self._cached_urls: Optional[set] = None
        self._filtered_urls: set = set()
        self._seen_source_urls: dict = {}  # {normalized_url: timestamp_str}
        self._load_filtered_jobs()
        self._load_seen_sources()

    @property
    def sheets_client(self) -> SheetsClient:
        """Get the sheets client, creating if needed."""
        if self._sheets_client is None:
            self._sheets_client = get_sheets_client()
        return self._sheets_client

    def refresh_cache(self) -> None:
        """
        Refresh the cached URL set from Google Sheets.

        Call this at the start of a scraping session to get current jobs.
        """
        jobs = self.sheets_client.get_all_jobs()

        # Build set of normalized URLs
        self._cached_urls = set()
        for job in jobs:
            if job.position_url:
                normalized = normalize_url(job.position_url)
                self._cached_urls.add(normalized)

        logger.info(f"Refreshed job cache: {len(self._cached_urls)} existing job URLs")

    def job_exists(self, url: str) -> bool:
        """
        Check if a job URL already exists in Google Sheets.

        Uses normalized URL for matching.
        Will refresh cache if not already loaded.

        Args:
            url: Job apply URL

        Returns:
            True if job already exists, False otherwise
        """
        if self._cached_urls is None:
            self.refresh_cache()

        normalized = normalize_url(url)
        exists = normalized in self._cached_urls

        if exists:
            logger.debug(f"Duplicate found: {url}")
        else:
            logger.debug(f"New job URL: {url}")

        return exists

    def add_to_cache(self, url: str) -> None:
        """
        Add a URL to the cache after it's been added to Sheets.

        Call this after successfully adding a job to prevent
        duplicates within the same scraping session.

        Args:
            url: Job apply URL
        """
        if self._cached_urls is None:
            self._cached_urls = set()

        normalized = normalize_url(url)
        self._cached_urls.add(normalized)

        logger.debug(f"Added to cache: {url}")

    # =========================================================================
    # Filtered Jobs Tracking
    # =========================================================================

    def _load_filtered_jobs(self) -> None:
        """Load filtered job URLs from file."""
        filtered_file = self.config.data_dir / FILTERED_JOBS_FILENAME
        if filtered_file.exists():
            try:
                with open(filtered_file, "r") as f:
                    data = json.load(f)
                    self._filtered_urls = set(data.get("filtered_urls", []))
                    logger.debug(f"Loaded {len(self._filtered_urls)} filtered job URLs")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load filtered jobs file: {e}")
                self._filtered_urls = set()
        else:
            self._filtered_urls = set()

    def _save_filtered_jobs(self) -> None:
        """Save filtered job URLs to file."""
        filtered_file = self.config.data_dir / FILTERED_JOBS_FILENAME

        data = {
            "filtered_urls": list(self._filtered_urls),
            "last_updated": datetime.now().isoformat()
        }

        try:
            with open(filtered_file, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save filtered jobs file: {e}")

    def is_filtered(self, url: str) -> bool:
        """
        Check if a job URL was previously filtered out.

        Args:
            url: Job apply URL (will be normalized)

        Returns:
            True if job was previously filtered, False otherwise
        """
        normalized = normalize_url(url)
        return normalized in self._filtered_urls

    def mark_as_filtered(self, url: str) -> None:
        """
        Mark a job URL as filtered (failed hard filters).

        Args:
            url: Job apply URL (will be normalized)
        """
        normalized = normalize_url(url)
        self._filtered_urls.add(normalized)
        self._save_filtered_jobs()
        logger.debug(f"Marked as filtered: {url}")

    def clear_filtered_jobs(self) -> None:
        """
        Clear all filtered jobs.

        Call this when user profile changes (graduation date, class standing, etc.)
        """
        self._filtered_urls = set()
        self._save_filtered_jobs()
        logger.info("Cleared filtered jobs cache")

    # =========================================================================
    # Seen Source URLs Tracking
    # =========================================================================

    def _load_seen_sources(self) -> None:
        """Load seen source URLs from file and prune expired entries."""
        seen_file = self.config.data_dir / SEEN_SOURCES_FILENAME
        if seen_file.exists():
            try:
                with open(seen_file, "r") as f:
                    data = json.load(f)
                    self._seen_source_urls = data.get("seen_urls", {})
                    logger.debug(f"Loaded {len(self._seen_source_urls)} seen source URLs")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load seen sources file: {e}")
                self._seen_source_urls = {}
        else:
            self._seen_source_urls = {}

        # Prune expired entries
        self._prune_seen_sources()

    def _prune_seen_sources(self) -> None:
        """Remove seen source URLs older than TTL."""
        if not self._seen_source_urls:
            return

        ttl_days = self.config.seen_sources_ttl_days
        cutoff = datetime.now() - timedelta(days=ttl_days)
        original_count = len(self._seen_source_urls)

        # Filter out expired entries
        self._seen_source_urls = {
            url: ts for url, ts in self._seen_source_urls.items()
            if datetime.fromisoformat(ts) > cutoff
        }

        pruned_count = original_count - len(self._seen_source_urls)
        if pruned_count > 0:
            logger.info(f"Pruned {pruned_count} seen source URLs older than {ttl_days} days")
            self._save_seen_sources()

    def _save_seen_sources(self) -> None:
        """Save seen source URLs to file."""
        seen_file = self.config.data_dir / SEEN_SOURCES_FILENAME

        data = {
            "seen_urls": self._seen_source_urls,
            "last_updated": datetime.now().isoformat()
        }

        try:
            with open(seen_file, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save seen sources file: {e}")

    def is_seen_source(self, url: str) -> bool:
        """
        Check if a source URL was previously processed.

        Args:
            url: Source URL (will be normalized)

        Returns:
            True if URL was previously seen, False otherwise
        """
        normalized = normalize_url(url)
        return normalized in self._seen_source_urls

    def mark_source_seen(self, url: str) -> None:
        """
        Mark a source URL as seen/processed.

        Args:
            url: Source URL (will be normalized)
        """
        normalized = normalize_url(url)
        self._seen_source_urls[normalized] = datetime.now().isoformat()
        self._save_seen_sources()
        logger.debug(f"Marked source as seen: {url}")

    def clear_seen_sources(self) -> None:
        """Clear all seen source URLs."""
        self._seen_source_urls = {}
        self._save_seen_sources()
        logger.info("Cleared seen sources cache")


# Singleton instance
_checker: Optional[DeduplicationChecker] = None


def get_dedup_checker(config: Optional[Config] = None) -> DeduplicationChecker:
    """
    Get the global DeduplicationChecker instance.

    Args:
        config: Optional config to use on first initialization.

    Returns:
        DeduplicationChecker singleton instance
    """
    global _checker
    if _checker is None:
        _checker = DeduplicationChecker(config)
    return _checker


def reset_dedup_checker() -> None:
    """Reset the singleton checker (useful for testing)."""
    global _checker
    _checker = None

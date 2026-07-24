"""
Newsletter email parser for ApplyPotato.
Extracts job listing URLs from newsletter HTML using link extraction (no AI).
"""

import logging
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

from .config import Config, get_config, NewsletterSource
from .gmail import GmailClient, EmailMessage, get_gmail_clients
from .github_parser import JobListing


logger = logging.getLogger(__name__)

# Known job posting domains/patterns to accept
JOB_URL_PATTERNS = [
    "simplify.jobs/p/",
    "lever.co", "greenhouse.io", "workday.com", "myworkdayjobs.com",
    "ashbyhq.com", "smartrecruiters.com", "icims.com", "jobvite.com",
    "/careers/", "/jobs/", "/job/",
]

# URL patterns to skip (non-job links)
SKIP_URL_PATTERNS = [
    "unsubscribe", "mailto:", "tel:", "#",
    "email.mg.", "mailchimp", "sendgrid",
    "simplify.jobs/copilot",
    "github.com/",
]


class NewsletterParser:
    """
    Parser for job listing newsletters.

    Fetches newsletter emails, extracts job URLs from HTML using
    BeautifulSoup, and returns JobListing objects compatible with
    the existing pipeline.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        gmail_client: Optional[GmailClient] = None,
        gmail_clients: Optional[List[GmailClient]] = None,
    ):
        """
        Args:
            config: Optional config object.
            gmail_client: Single client override (kept for callers/tests passing one client).
            gmail_clients: Explicit list of clients. Defaults to every configured account.
        """
        self.config = config or get_config()
        if gmail_clients:
            self.gmail_clients = gmail_clients
        elif gmail_client:
            self.gmail_clients = [gmail_client]
        else:
            self.gmail_clients = get_gmail_clients()

    def _is_job_url(self, url: str) -> bool:
        """Check if a URL looks like a job posting."""
        url_lower = url.lower()

        if any(p in url_lower for p in SKIP_URL_PATTERNS):
            return False

        return any(p in url_lower for p in JOB_URL_PATTERNS)

    def _extract_from_internship_elements(self, soup: BeautifulSoup, source_name: str) -> List[JobListing]:
        """
        Extract jobs from SWElist-style HTML: <p class="internship"><strong>Company:</strong> <a>Title</a></p>
        """
        listings = []
        seen_urls = set()
        source_repo = f"newsletter:{source_name}"

        for p in soup.find_all("p", class_="internship"):
            strong = p.find("strong")
            company = strong.get_text(strip=True).rstrip(":").strip() if strong else ""

            link = p.find("a", href=True)
            if not link:
                continue

            url = link["href"].strip()
            title = link.get_text(strip=True)

            if not self._is_job_url(url):
                continue

            if url in seen_urls:
                continue
            seen_urls.add(url)

            listings.append(JobListing(
                company=company or "Unknown",
                title=title or "Job Posting",
                location="",
                url=url,
                date_posted="",
                source_repo=source_repo,
                age_days=0,
            ))

        return listings

    def _extract_generic_job_links(self, soup: BeautifulSoup, source_name: str) -> List[JobListing]:
        """Fallback: extract any job-like links from HTML."""
        listings = []
        seen_urls = set()
        source_repo = f"newsletter:{source_name}"

        for link in soup.find_all("a", href=True):
            url = link["href"].strip()

            if not self._is_job_url(url) or url in seen_urls:
                continue
            seen_urls.add(url)

            # Try to get company from nearby <strong> tag
            company = ""
            parent = link.find_parent(["p", "div", "li", "td"])
            if parent:
                strong = parent.find("strong")
                if strong:
                    company = strong.get_text(strip=True).rstrip(":").strip()

            listings.append(JobListing(
                company=company or "Unknown",
                title=link.get_text(strip=True) or "Job Posting",
                location="",
                url=url,
                date_posted="",
                source_repo=source_repo,
                age_days=0,
            ))

        return listings

    def extract_jobs_from_email(self, email: EmailMessage, source_name: str) -> List[JobListing]:
        """Extract job listings from a single newsletter email."""
        html = email.body_html
        if not html or not html.strip():
            logger.warning(f"Empty newsletter content from {email.subject}")
            return []

        soup = BeautifulSoup(html, "html.parser")

        # Try structured extraction first (SWElist format)
        listings = self._extract_from_internship_elements(soup, source_name)

        # Fallback to generic link extraction
        if not listings:
            listings = self._extract_generic_job_links(soup, source_name)

        logger.info(f"Extracted {len(listings)} jobs from newsletter: {email.subject}")
        return listings

    def fetch_newsletter_emails(self, source: NewsletterSource) -> List[Tuple[GmailClient, EmailMessage]]:
        """
        Fetch newsletter emails for a specific source across every configured account.

        Returns (client, email) pairs so each email is marked processed on the
        account it came from.
        """
        hours = self.config.newsletter_lookback_days * 24
        results = []

        for client in self.gmail_clients:
            try:
                emails = client.fetch_emails_by_senders(
                    senders=source.sender_emails,
                    hours=hours,
                    skip_processed=True,
                )
            except Exception as e:
                # One broken account must not stop the others
                logger.error(f"Failed to fetch newsletters for {client.label}: {e}")
                continue
            results.extend((client, email) for email in emails)

        return results

    def fetch_all_jobs(self) -> List[JobListing]:
        """
        Fetch and extract jobs from all configured newsletter sources.
        Main entry point for the newsletter parser.
        """
        if not self.config.newsletter_sources:
            logger.debug("No newsletter sources configured")
            return []

        all_listings = []
        seen_urls = set()

        for source in self.config.newsletter_sources:
            logger.info("")
            logger.info(f"Fetching newsletters from: {source.name} ({source.sender_emails})")

            fetched = self.fetch_newsletter_emails(source)
            logger.info(f"Found {len(fetched)} unprocessed newsletters from {source.name}")

            for client, email in fetched:
                logger.info(f"Processing newsletter [{client.label}]: {email.subject} ({email.date})")

                listings = self.extract_jobs_from_email(email, source.name)

                for listing in listings:
                    if listing.url not in seen_urls:
                        seen_urls.add(listing.url)
                        all_listings.append(listing)

                client.mark_newsletter_as_processed(email.message_id)

        logger.info(f"Total newsletter jobs extracted: {len(all_listings)}")
        return all_listings

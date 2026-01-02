"""
GitHub job list parser for ApplyPotato.
Fetches and parses markdown job tables from GitHub repositories.
"""

import re
import logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import List, Optional, Tuple
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .config import get_config, Config, GitHubRepo


logger = logging.getLogger(__name__)


@dataclass
class JobListing:
    """Represents a job listing parsed from GitHub markdown."""
    company: str
    title: str
    location: str
    url: str
    date_posted: str
    source_repo: str
    age_days: int = 0  # Parsed from "3d" → 3

    def __hash__(self):
        return hash((self.company.lower(), self.title.lower(), self.url))

    def __eq__(self, other):
        if not isinstance(other, JobListing):
            return False
        return (self.company.lower() == other.company.lower() and
                self.title.lower() == other.title.lower() and
                self.url == other.url)


class GitHubParser:
    """Parser for GitHub job list repositories."""

    def __init__(self, config: Optional[Config] = None):
        """
        Initialize GitHub parser.

        Args:
            config: Optional config object. Uses global config if not provided.
        """
        self.config = config or get_config()
        self.client = httpx.Client(timeout=float(self.config.page_timeout_seconds), follow_redirects=True)

    def _get_repo_config(self, repo: GitHubRepo) -> dict:
        """
        Get branch/file config for a repository.

        Args:
            repo: GitHubRepo object with owner_repo and branch

        Returns:
            Dict with 'branch' and 'file' keys
        """
        return {"branch": repo.branch, "file": "README.md"}

    def _get_raw_url(self, repo: str, branch: str = "main", file: str = "README.md") -> str:
        """
        Get raw GitHub URL for a file.

        Args:
            repo: Repository in format "owner/repo"
            branch: Branch name
            file: File path

        Returns:
            Raw GitHub URL
        """
        return f"https://raw.githubusercontent.com/{repo}/{branch}/{file}"

    def _fetch_markdown(self, repo: GitHubRepo) -> Optional[str]:
        """
        Fetch markdown content from a GitHub repository.

        Args:
            repo: GitHubRepo object with owner_repo and branch

        Returns:
            Markdown content or None if fetch failed.
        """
        repo_config = self._get_repo_config(repo)
        branch = repo_config["branch"]
        file = repo_config["file"]

        url = self._get_raw_url(repo.owner_repo, branch, file)

        try:
            response = self.client.get(url)
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as e:
            # Try 'main' branch as fallback if configured branch failed
            if branch != "main":
                logger.debug(f"Branch '{branch}' failed, trying 'main'...")
                url = self._get_raw_url(repo.owner_repo, "main", file)
                try:
                    response = self.client.get(url)
                    response.raise_for_status()
                    return response.text
                except httpx.HTTPStatusError:
                    pass
            logger.error(f"Failed to fetch {repo.owner_repo}: {e}")
            return None
        except httpx.RequestError as e:
            logger.error(f"Request error for {repo.owner_repo}: {e}")
            return None

    def _parse_age(self, age_str: str) -> int:
        """
        Parse age string to integer days. Supports multiple formats:
        - Relative: "3d", "14d"
        - Calendar: "Jan 01", "Dec 12", "Jan 1"

        Args:
            age_str: Age string in any supported format

        Returns:
            Number of days, or 999 if cannot parse (treated as old)
        """
        if not age_str:
            return 999

        age_str = age_str.strip()

        # Try relative format first: "3d", "14d"
        match = re.match(r'(\d+)d', age_str)
        if match:
            return int(match.group(1))

        # Try calendar date format: "Jan 01", "Dec 12", "Jan 1"
        # Assume current year, handle year boundary
        try:
            # Parse month and day
            parsed = datetime.strptime(age_str, "%b %d")
            today = date.today()

            # Use current year by default
            post_date = parsed.replace(year=today.year).date()

            # If date is in the future, it was probably last year
            if post_date > today:
                post_date = post_date.replace(year=today.year - 1)

            age_days = (today - post_date).days
            return max(0, age_days)
        except ValueError:
            pass

        # Try single digit day: "Jan 1" -> "Jan 01"
        try:
            parsed = datetime.strptime(age_str, "%b %d")
            today = date.today()
            post_date = parsed.replace(year=today.year).date()
            if post_date > today:
                post_date = post_date.replace(year=today.year - 1)
            return max(0, (today - post_date).days)
        except ValueError:
            pass

        return 999

    def _parse_table_row(self, row: str) -> List[str]:
        """
        Parse a markdown table row into cells.

        Args:
            row: Markdown table row (e.g., "| Cell 1 | Cell 2 |")

        Returns:
            List of cell contents
        """
        # Remove leading/trailing pipes and split
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]

        cells = [cell.strip() for cell in row.split("|")]
        return cells

    def _extract_url_from_cell(self, cell: str) -> Tuple[str, Optional[str]]:
        """
        Extract text and URL from a markdown cell.

        Args:
            cell: Cell content that may contain markdown links

        Returns:
            Tuple of (text, url) where url may be None
        """
        # Match [text](url) pattern
        link_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
        match = re.search(link_pattern, cell)

        if match:
            text = match.group(1)
            url = match.group(2)
            return text, url

        # No link found
        return cell, None

    def _extract_all_urls(self, cell: str) -> List[str]:
        """
        Extract all URLs from a markdown cell.

        Args:
            cell: Cell content that may contain markdown links or HTML anchor tags

        Returns:
            List of URLs found
        """
        urls = []

        # Match markdown links: [text](url)
        md_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
        md_matches = re.findall(md_pattern, cell)
        urls.extend(url for _, url in md_matches)

        # Match HTML anchor tags: <a href="url"> or <a href='url'>
        html_pattern = r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>'
        html_matches = re.findall(html_pattern, cell, re.IGNORECASE)
        urls.extend(html_matches)

        return urls

    def _is_valid_job_url(self, url: str) -> bool:
        """
        Check if URL is a valid job posting URL.

        Args:
            url: URL to check

        Returns:
            True if URL appears to be a job posting
        """
        if not url:
            return False

        # Filter out common non-job URLs
        invalid_patterns = [
            "simplify.jobs",  # Simplify redirect links (we want actual job URLs)
            "github.com",
            "linkedin.com/company",
            "twitter.com",
            "youtube.com",
            "#",  # Anchor links
        ]

        url_lower = url.lower()
        for pattern in invalid_patterns:
            if pattern in url_lower:
                return False

        # Must be http/https
        if not url.startswith(("http://", "https://")):
            return False

        return True

    def _parse_jobs_table(self, markdown: str, source_repo: str) -> List[JobListing]:
        """
        Parse job listings from markdown table.

        Args:
            markdown: Full markdown content
            source_repo: Source repository name

        Returns:
            List of JobListing objects
        """
        jobs = []
        lines = markdown.split("\n")

        # Find table header and determine column indices
        header_idx = -1
        columns = {}

        for i, line in enumerate(lines):
            if "|" in line and "Company" in line:
                header_idx = i
                cells = self._parse_table_row(line)

                # Map column names to indices
                for idx, cell in enumerate(cells):
                    cell_lower = cell.lower().strip()
                    if "company" in cell_lower:
                        columns["company"] = idx
                    elif "role" in cell_lower or "position" in cell_lower or "title" in cell_lower:
                        columns["title"] = idx
                    elif "location" in cell_lower:
                        columns["location"] = idx
                    elif "link" in cell_lower or "application" in cell_lower or "apply" in cell_lower:
                        columns["link"] = idx
                    elif "date" in cell_lower or "posted" in cell_lower:
                        columns["date"] = idx

                break

        if header_idx == -1:
            logger.warning(f"No table header found in {source_repo}")
            return jobs

        # Skip header and separator row
        data_start = header_idx + 2

        for line in lines[data_start:]:
            line = line.strip()

            # Stop at empty line or non-table content
            if not line or not line.startswith("|"):
                continue

            # Skip separator rows
            if re.match(r'^\|[\s\-:|]+\|$', line):
                continue

            cells = self._parse_table_row(line)

            try:
                # Extract company
                company_cell = cells[columns.get("company", 0)]
                company, company_url = self._extract_url_from_cell(company_cell)

                # Skip if company is a strike-through (closed position)
                if company.startswith("~~") or "<del>" in company_cell:
                    continue

                # Extract title/role
                title_cell = cells[columns.get("title", 1)] if "title" in columns else ""
                title, title_url = self._extract_url_from_cell(title_cell)

                # Extract location
                location = cells[columns.get("location", 2)] if "location" in columns else ""

                # Extract application URL
                link_cell = cells[columns.get("link", 3)] if "link" in columns else ""
                urls = self._extract_all_urls(link_cell)

                # Find valid job URL
                job_url = None
                for url in urls:
                    if self._is_valid_job_url(url):
                        job_url = url
                        break

                # Fall back to title URL if no link column URL
                if not job_url and title_url and self._is_valid_job_url(title_url):
                    job_url = title_url

                # Skip if no valid URL found
                if not job_url:
                    continue

                # Extract date
                date_posted = cells[columns.get("date", -1)] if "date" in columns and columns["date"] < len(cells) else ""

                # Clean up extracted values
                company = re.sub(r'[*_~`]', '', company).strip()
                title = re.sub(r'[*_~`]', '', title).strip()
                location = re.sub(r'[*_~`]', '', location).strip()
                date_posted = re.sub(r'[*_~`]', '', date_posted).strip()

                # Parse age and filter old jobs
                age_days = self._parse_age(date_posted)
                if age_days > self.config.job_age_limit_days:
                    continue

                if company and title:
                    jobs.append(JobListing(
                        company=company,
                        title=title,
                        location=location,
                        url=job_url,
                        date_posted=date_posted,
                        source_repo=source_repo,
                        age_days=age_days,
                    ))

            except (IndexError, KeyError) as e:
                logger.debug(f"Failed to parse row: {line} - {e}")
                continue

        return jobs

    def _parse_html_table(self, content: str, source_repo: str) -> List[JobListing]:
        """
        Parse job listings from HTML tables.

        Args:
            content: HTML/markdown content containing tables
            source_repo: Source repository name

        Returns:
            List of JobListing objects (filtered by age)
        """
        jobs = []
        soup = BeautifulSoup(content, 'html.parser')
        last_company = ""  # Track previous company for ↳ rows

        # Find all table rows
        for row in soup.find_all('tr'):
            cells = row.find_all('td')

            # Skip header rows (they use <th>) or rows without enough cells
            if len(cells) < 4:
                continue

            try:
                # Column 0: Company
                company_cell = cells[0]
                company_link = company_cell.find('a')
                company = company_link.get_text(strip=True) if company_link else company_cell.get_text(strip=True)

                # Skip strikethrough/closed companies
                if company_cell.find('del') or company_cell.find('s'):
                    continue

                # Column 1: Role/Title
                title_cell = cells[1]
                title = title_cell.get_text(strip=True)

                # Column 2: Location
                location_cell = cells[2]
                location = location_cell.get_text(strip=True)

                # Column 3: Application URL
                app_cell = cells[3]
                apply_link = app_cell.find('a', href=True)

                # Column 4: Age (if present)
                age_days = 0
                if len(cells) >= 5:
                    age_cell = cells[4]
                    age_text = age_cell.get_text(strip=True)
                    age_days = self._parse_age(age_text)

                # Filter 1: Skip old jobs
                if age_days > self.config.job_age_limit_days:
                    continue

                # Filter 2: Skip jobs with no valid URL (closed/inactive)
                if not apply_link or not apply_link.get('href'):
                    continue

                job_url = apply_link['href']

                # Skip simplify.jobs redirect links, we want actual job URLs
                if 'simplify.jobs' in job_url.lower():
                    # Try to find another link in the cell
                    for link in app_cell.find_all('a', href=True):
                        href = link.get('href', '')
                        if href and 'simplify.jobs' not in href.lower():
                            job_url = href
                            break
                    else:
                        # Only simplify links found, skip for now
                        # (we might want to keep these in the future)
                        continue

                # Validate URL
                if not self._is_valid_job_url(job_url):
                    continue

                # Clean up company name (remove extra whitespace, asterisks, etc.)
                company = re.sub(r'[*_~`]', '', company).strip()
                title = re.sub(r'[*_~`]', '', title).strip()

                # Handle ↳ symbol (means "same company as above")
                if company in ('↳', '⎯', '') or not company:
                    company = last_company
                else:
                    last_company = company

                if company and title:
                    jobs.append(JobListing(
                        company=company,
                        title=title,
                        location=location,
                        url=job_url,
                        date_posted="",
                        source_repo=source_repo,
                        age_days=age_days,
                    ))

            except (IndexError, KeyError, AttributeError) as e:
                logger.debug(f"Failed to parse HTML row: {e}")
                continue

        return jobs

    def fetch_all_jobs(self) -> List[JobListing]:
        """
        Fetch all jobs from configured GitHub repositories.

        Returns:
            List of JobListing objects from all sources (filtered by age).
        """
        all_jobs = []
        seen = set()

        for repo in self.config.github_repos:
            logger.info("")  # Visual separator between repos
            logger.info(f"Fetching jobs from {repo.owner_repo} (branch: {repo.branch})...")
            content = self._fetch_markdown(repo)

            if not content:
                logger.warning(f"Failed to fetch content from {repo.owner_repo}")
                continue

            # Try HTML parsing first (some repos use HTML tables)
            jobs = self._parse_html_table(content, repo.owner_repo)

            # Fall back to markdown parsing if no HTML jobs found
            if not jobs:
                logger.debug(f"No HTML tables found in {repo.owner_repo}, trying markdown parsing")
                jobs = self._parse_jobs_table(content, repo.owner_repo)

            logger.info(f"Found {len(jobs)} recent jobs in {repo.owner_repo} (within {self.config.job_age_limit_days} days)")

            # Deduplicate
            for job in jobs:
                if job not in seen:
                    seen.add(job)
                    all_jobs.append(job)

        logger.info(f"Total unique recent jobs found: {len(all_jobs)}")
        return all_jobs

    def close(self):
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

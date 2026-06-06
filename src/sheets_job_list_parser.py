"""
Google Sheets Job List parser for ApplyPotato.

Reads job URLs from a user-managed Google Sheet with "Job List" and "Result" columns,
and writes the processing result back after each URL is handled.
"""

import logging
from typing import Dict, List, Optional

from .config import Config, get_config
from .github_parser import JobListing
from .sheets import SheetsClient

logger = logging.getLogger(__name__)

SOURCE_REPO = "sheets-list"


class SheetsJobListParser:
    """
    Reads job URLs from a Google Sheet and writes results back after processing.

    Expected sheet format (case-insensitive header row):
        | Job List            | Result |
        | https://example.com |        |  <- processed on next run
        | https://other.com   | Done   |  <- skipped (already has result)
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self._sheets_client = SheetsClient(self.config)
        self._url_to_row: Dict[str, int] = {}
        self._job_list_col: int = 0
        self._result_col: int = 1

    def _get_service(self):
        return self._sheets_client._get_service()

    def _col_letter(self, col_index: int) -> str:
        """Convert 0-indexed column number to A1 letter (supports A-Z only)."""
        return chr(ord("A") + col_index)

    def fetch_all_jobs(self) -> List[JobListing]:
        """
        Read all unprocessed rows from the job list sheet.

        Returns JobListing objects with source_repo="sheets-list" and age_days=0
        so they always pass the age filter.
        """
        sheet_id = self.config.job_list_sheet_id
        tab = self.config.job_list_sheet_tab
        if not sheet_id:
            return []

        service = self._get_service()
        range_name = f"'{tab}'!A1:Z"

        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=range_name,
            ).execute()
        except Exception as e:
            logger.error(f"Failed to read job list sheet: {e}")
            return []

        rows = result.get("values", [])
        if not rows:
            logger.info("Job list sheet is empty")
            return []

        # Find header row (first row)
        header = [h.strip().lower() for h in rows[0]]
        try:
            self._job_list_col = header.index("job list")
        except ValueError:
            logger.error("Job list sheet missing 'Job List' header column")
            return []
        try:
            self._result_col = header.index("result")
        except ValueError:
            logger.error("Job list sheet missing 'Result' header column")
            return []

        listings: List[JobListing] = []
        self._url_to_row = {}

        for row_idx, row in enumerate(rows[1:], start=2):  # 1-indexed, row 1 is header
            # Pad short rows
            while len(row) <= max(self._job_list_col, self._result_col):
                row.append("")

            url = row[self._job_list_col].strip()
            result_val = row[self._result_col].strip()

            if not url:
                continue
            if result_val:
                # Already processed — skip
                continue

            self._url_to_row[url] = row_idx
            listings.append(JobListing(
                company="",
                title="",
                location="",
                url=url,
                date_posted="",
                source_repo=SOURCE_REPO,
                age_days=0,
            ))

        logger.info(f"Job list sheet: found {len(listings)} unprocessed URL(s)")
        return listings

    def mark_row(self, url: str, status: str) -> None:
        """Write a result string to the Result column for the given URL's row."""
        row_num = self._url_to_row.get(url)
        if row_num is None:
            logger.warning(f"mark_row: URL not found in job list index: {url}")
            return

        sheet_id = self.config.job_list_sheet_id
        tab = self.config.job_list_sheet_tab
        col_letter = self._col_letter(self._result_col)
        cell = f"'{tab}'!{col_letter}{row_num}"

        try:
            service = self._get_service()
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=cell,
                valueInputOption="RAW",
                body={"values": [[status]]},
            ).execute()
            logger.info(f"  Job list sheet: row {row_num} marked '{status}'")
        except Exception as e:
            logger.warning(f"  Failed to write result to job list sheet row {row_num}: {e}")

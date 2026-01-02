"""
Google Sheets integration for ApplyPotato.
Handles all CRUD operations for the Jobs tab.
"""

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import get_config, Config


def normalize_date(date_str: str) -> str:
    """
    Normalize date string to MM/DD/YYYY format.

    Handles formats like:
    - "2025-12-27" -> "12/27/2025"
    - "Dec 27, 2025" -> "12/27/2025"
    - Already MM/DD/YYYY -> unchanged
    """
    if not date_str:
        return ""

    date_str = date_str.strip()

    # Try ISO format (YYYY-MM-DD)
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%m/%d/%Y")
    except ValueError:
        pass

    # Try "Dec 27, 2025" format
    try:
        dt = datetime.strptime(date_str, "%b %d, %Y")
        return dt.strftime("%m/%d/%Y")
    except ValueError:
        pass

    # Try "December 27, 2025" format
    try:
        dt = datetime.strptime(date_str, "%B %d, %Y")
        return dt.strftime("%m/%d/%Y")
    except ValueError:
        pass

    # Try "Dec 23" format (no year - assume current year)
    try:
        dt = datetime.strptime(date_str, "%b %d")
        dt = dt.replace(year=datetime.now().year)
        return dt.strftime("%m/%d/%Y")
    except ValueError:
        pass

    # Try "December 23" format (no year - assume current year)
    try:
        dt = datetime.strptime(date_str, "%B %d")
        dt = dt.replace(year=datetime.now().year)
        return dt.strftime("%m/%d/%Y")
    except ValueError:
        pass

    # Return as-is if no format matched
    return date_str


# Google Sheets API scopes
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Column mapping (0-indexed)
COLUMNS = {
    "company": 0,           # A
    "position": 1,          # B
    "status": 2,            # C
    "job_posting_date": 3,  # D
    "application_date": 4,  # E
    "oa_date": 5,           # F
    "phone_date": 6,        # G
    "tech_date": 7,         # H
    "fit_score": 8,         # I
    "salary": 9,            # J
    "job_type": 10,         # K
    "work_model": 11,       # L
    "location": 12,         # M
    "season_year": 13,      # N
    "deadline": 14,         # O
    "source": 15,           # P
    "added_date": 16,       # Q
    "notes": 17,            # R
}

# Header row (must match column order)
HEADERS = [
    "Company", "Position", "Status", "Job Posting Date", "Application Date",
    "OA Date", "Phone Interview Date", "Tech Interview Date", "Fit Score",
    "Salary", "Job Type", "Work Model", "Location", "Season/Year",
    "Deadline", "Source", "Added Date", "Notes"
]

# Status values
STATUS_NEW = "New"
STATUS_APPLIED = "Applied"
STATUS_OA = "OA"
STATUS_PHONE = "Phone"
STATUS_TECHNICAL = "Technical"
STATUS_OFFER = "Offer"
STATUS_REJECTED = "Rejected"
STATUS_GHOSTED = "Ghosted"


@dataclass
class JobRow:
    """Represents a job row in the spreadsheet."""
    row_number: int  # 1-indexed row number in sheet
    company: str
    position: str
    position_url: Optional[str]
    status: str
    job_posting_date: str
    application_date: str
    oa_date: str
    phone_date: str
    tech_date: str
    fit_score: int
    salary: str
    job_type: str
    work_model: str
    location: str
    season_year: str
    deadline: str
    source: str
    added_date: str
    notes: str

    @classmethod
    def from_row(cls, row_number: int, values: List[str]) -> "JobRow":
        """Create JobRow from spreadsheet row values."""
        # Pad row to ensure all columns exist
        while len(values) < len(COLUMNS):
            values.append("")

        # Parse position URL from hyperlink formula if present
        position = values[COLUMNS["position"]]
        position_url = None
        if position.startswith('=HYPERLINK('):
            # Parse =HYPERLINK("url", "text")
            try:
                parts = position[11:-1].split('", "')
                if len(parts) == 2:
                    position_url = parts[0].strip('"')
                    position = parts[1].strip('"')
            except (IndexError, ValueError):
                pass

        # Parse fit score
        try:
            fit_score = int(values[COLUMNS["fit_score"]]) if values[COLUMNS["fit_score"]] else 0
        except ValueError:
            fit_score = 0

        return cls(
            row_number=row_number,
            company=values[COLUMNS["company"]],
            position=position,
            position_url=position_url,
            status=values[COLUMNS["status"]],
            job_posting_date=values[COLUMNS["job_posting_date"]],
            application_date=values[COLUMNS["application_date"]],
            oa_date=values[COLUMNS["oa_date"]],
            phone_date=values[COLUMNS["phone_date"]],
            tech_date=values[COLUMNS["tech_date"]],
            fit_score=fit_score,
            salary=values[COLUMNS["salary"]],
            job_type=values[COLUMNS["job_type"]],
            work_model=values[COLUMNS["work_model"]],
            location=values[COLUMNS["location"]],
            season_year=values[COLUMNS["season_year"]],
            deadline=values[COLUMNS["deadline"]],
            source=values[COLUMNS["source"]],
            added_date=values[COLUMNS["added_date"]],
            notes=values[COLUMNS["notes"]],
        )


class SheetsClient:
    """Client for Google Sheets operations."""

    def __init__(self, config: Optional[Config] = None):
        """
        Initialize Sheets client.

        Args:
            config: Optional config object. Uses global config if not provided.
        """
        self.config = config or get_config()
        self._service = None
        self._creds = None

    def _get_credentials(self) -> Credentials:
        """Get or refresh Google API credentials."""
        if self._creds and self._creds.valid:
            return self._creds

        token_path = self.config.auth_dir / "sheets_token.json"

        # Try to load existing credentials
        if token_path.exists():
            self._creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        # If no valid credentials, run OAuth flow
        if not self._creds or not self._creds.valid:
            if self._creds and self._creds.expired and self._creds.refresh_token:
                self._creds.refresh(Request())
            else:
                if not self.config.google_credentials_path.exists():
                    raise FileNotFoundError(
                        f"Google credentials file not found: {self.config.google_credentials_path}\n"
                        "Please download credentials.json from Google Cloud Console."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.config.google_credentials_path), SCOPES
                )
                self._creds = flow.run_local_server(
                    port=self.config.oauth_local_port,
                    open_browser=True,
                    timeout_seconds=self.config.oauth_timeout_seconds
                )

            # Save credentials for next run
            with open(token_path, "w") as token:
                token.write(self._creds.to_json())

        return self._creds

    def _get_service(self):
        """Get Google Sheets service instance."""
        if self._service is None:
            creds = self._get_credentials()
            self._service = build("sheets", "v4", credentials=creds)
        return self._service

    def _retry_with_backoff(self, func, max_retries: int = 3):
        """Execute function with exponential backoff on rate limit errors."""
        for attempt in range(max_retries):
            try:
                return func()
            except HttpError as e:
                if e.resp.status == 429:  # Rate limit
                    wait_time = (2 ** attempt) * 10  # 10, 20, 40 seconds
                    print(f"Rate limited, waiting {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    raise
        # Final attempt
        return func()

    def _ensure_jobs_sheet_exists(self) -> None:
        """Create or rename a sheet to 'Jobs' if it doesn't exist."""
        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        # Get existing sheets with their IDs
        def get_sheets():
            result = service.spreadsheets().get(
                spreadsheetId=sheet_id,
                fields="sheets.properties"
            ).execute()
            return result.get("sheets", [])

        sheets = self._retry_with_backoff(get_sheets)
        sheet_titles = [s["properties"]["title"] for s in sheets]

        if "Jobs" in sheet_titles:
            return  # Already exists

        # Try to rename Sheet1 to Jobs if it exists
        for sheet in sheets:
            if sheet["properties"]["title"] == "Sheet1":
                def rename_sheet():
                    service.spreadsheets().batchUpdate(
                        spreadsheetId=sheet_id,
                        body={
                            "requests": [{
                                "updateSheetProperties": {
                                    "properties": {
                                        "sheetId": sheet["properties"]["sheetId"],
                                        "title": "Jobs"
                                    },
                                    "fields": "title"
                                }
                            }]
                        }
                    ).execute()

                self._retry_with_backoff(rename_sheet)
                print("Renamed 'Sheet1' to 'Jobs'")
                return

        # No Sheet1 found, create new Jobs sheet
        def create_sheet():
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "requests": [{
                        "addSheet": {
                            "properties": {"title": "Jobs"}
                        }
                    }]
                }
            ).execute()

        self._retry_with_backoff(create_sheet)
        print("Created 'Jobs' sheet")

    def _ensure_date_formatting(self) -> None:
        """Apply MM/DD/YYYY date format to date columns."""
        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        # First, get the sheet's internal ID (sheetId, not spreadsheetId)
        def get_sheet_id():
            result = service.spreadsheets().get(
                spreadsheetId=sheet_id,
                fields="sheets.properties"
            ).execute()
            for sheet in result.get("sheets", []):
                if sheet["properties"]["title"] == "Jobs":
                    return sheet["properties"]["sheetId"]
            return 0  # Fallback to first sheet

        jobs_sheet_id = self._retry_with_backoff(get_sheet_id)

        # Date columns (0-indexed):
        # D=3 (job_posting_date), E=4 (application_date), F=5 (oa_date),
        # G=6 (phone_date), H=7 (tech_date), O=14 (deadline), Q=16 (added_date)
        date_column_indices = [3, 4, 5, 6, 7, 14, 16]

        requests = []
        for col_idx in date_column_indices:
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": jobs_sheet_id,
                        "startColumnIndex": col_idx,
                        "endColumnIndex": col_idx + 1
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "DATE",
                                "pattern": "M/d/yyyy"
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat"
                }
            })

        # Freeze header row
        requests.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": jobs_sheet_id,
                    "gridProperties": {"frozenRowCount": 1}
                },
                "fields": "gridProperties.frozenRowCount"
            }
        })

        # Add filter to header row
        requests.append({
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": jobs_sheet_id,
                        "startRowIndex": 0,
                        "startColumnIndex": 0,
                        "endColumnIndex": 18
                    }
                }
            }
        })

        def apply_formatting():
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": requests}
            ).execute()

        self._retry_with_backoff(apply_formatting)

    def ensure_headers(self) -> None:
        """Ensure the Jobs sheet has correct headers."""
        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        # First ensure the Jobs sheet exists
        self._ensure_jobs_sheet_exists()

        def check_headers():
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range="Jobs!A1:R1"
            ).execute()
            return result.get("values", [[]])[0]

        try:
            existing = self._retry_with_backoff(check_headers)
        except HttpError as e:
            if e.resp.status == 400:  # Range issue
                existing = []
            else:
                raise

        if existing != HEADERS:
            def set_headers():
                service.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range="Jobs!A1:R1",
                    valueInputOption="RAW",
                    body={"values": [HEADERS]}
                ).execute()

            self._retry_with_backoff(set_headers)

        # Ensure date columns have proper formatting
        self._ensure_date_formatting()

    def get_all_jobs(self) -> List[JobRow]:
        """
        Get all jobs from the spreadsheet.

        Returns:
            List of JobRow objects.
        """
        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        def fetch():
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range="Jobs!A2:R",  # Skip header row
                valueRenderOption="FORMULA"  # Get formulas to parse hyperlinks
            ).execute()
            return result.get("values", [])

        rows = self._retry_with_backoff(fetch)

        jobs = []
        for i, row in enumerate(rows):
            row_number = i + 2  # 1-indexed, skip header
            jobs.append(JobRow.from_row(row_number, row))

        return jobs

    def add_job(self, job_data: Dict[str, Any]) -> int:
        """
        Add a new job to the spreadsheet.

        Args:
            job_data: Dictionary with job fields. Keys should match COLUMNS keys.

        Returns:
            Row number of the added job.
        """
        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        # Build row values
        row = [""] * len(COLUMNS)

        for key, idx in COLUMNS.items():
            if key in job_data:
                value = job_data[key]
                if value is not None:
                    row[idx] = str(value)

        # Normalize date columns to MM/DD/YYYY format
        date_columns = ["job_posting_date", "application_date", "deadline"]
        for col in date_columns:
            if col in COLUMNS and row[COLUMNS[col]]:
                row[COLUMNS[col]] = normalize_date(row[COLUMNS[col]])

        # Handle position with URL as hyperlink
        if "position_url" in job_data and job_data["position_url"]:
            position = job_data.get("position", "Link")
            url = job_data["position_url"]
            row[COLUMNS["position"]] = f'=HYPERLINK("{url}", "{position}")'

        # Set defaults
        if not row[COLUMNS["status"]]:
            row[COLUMNS["status"]] = STATUS_NEW
        if not row[COLUMNS["added_date"]]:
            row[COLUMNS["added_date"]] = datetime.now().strftime("%m/%d/%Y")

        def append():
            result = service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range="Jobs!A:R",
                valueInputOption="USER_ENTERED",  # Parse formulas
                insertDataOption="INSERT_ROWS",
                body={"values": [row]}
            ).execute()
            return result

        result = self._retry_with_backoff(append)

        # Parse the updated range to get row number
        updated_range = result.get("updates", {}).get("updatedRange", "")
        # Format: Jobs!A123:R123
        try:
            row_num = int(updated_range.split("!")[1].split(":")[0][1:])
        except (IndexError, ValueError):
            row_num = -1

        return row_num

    def update_job(self, row_number: int, updates: Dict[str, Any]) -> None:
        """
        Update an existing job row.

        Args:
            row_number: 1-indexed row number in the sheet.
            updates: Dictionary of column names to new values.
        """
        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        # Build list of updates
        data = []
        for key, value in updates.items():
            if key in COLUMNS:
                col_idx = COLUMNS[key]
                col_letter = chr(ord("A") + col_idx)
                range_str = f"Jobs!{col_letter}{row_number}"
                data.append({
                    "range": range_str,
                    "values": [[str(value) if value is not None else ""]]
                })

        if not data:
            return

        def batch_update():
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": data
                }
            ).execute()

        self._retry_with_backoff(batch_update)

    def find_jobs_by_company(self, company_name: str) -> List[JobRow]:
        """
        Find jobs by company name (case-insensitive partial match).

        Args:
            company_name: Company name to search for.

        Returns:
            List of matching JobRow objects, sorted by added_date descending.
        """
        all_jobs = self.get_all_jobs()
        company_lower = company_name.lower()

        matches = [
            job for job in all_jobs
            if company_lower in job.company.lower()
        ]

        # Sort by added_date descending (most recent first)
        matches.sort(key=lambda j: j.added_date, reverse=True)

        return matches

    def find_jobs_by_company_and_position(self, company_name: str, position: str) -> List[JobRow]:
        """
        Find jobs by company name AND position (case-insensitive partial match).

        Args:
            company_name: Company name to search for.
            position: Position/title to search for.

        Returns:
            List of matching JobRow objects, sorted by added_date descending.
        """
        all_jobs = self.get_all_jobs()
        company_lower = company_name.lower()
        position_lower = position.lower()

        matches = [
            job for job in all_jobs
            if company_lower in job.company.lower()
            and position_lower in job.position.lower()
        ]

        # Sort by added_date descending (most recent first)
        matches.sort(key=lambda j: j.added_date, reverse=True)

        return matches

    def job_exists(self, company: str, position: str) -> bool:
        """
        Check if a job already exists in the spreadsheet.

        Args:
            company: Company name.
            position: Job position/title.

        Returns:
            True if job exists, False otherwise.
        """
        all_jobs = self.get_all_jobs()
        company_lower = company.lower()
        position_lower = position.lower()

        for job in all_jobs:
            if (job.company.lower() == company_lower and
                job.position.lower() == position_lower):
                return True

        return False

    def append_to_notes(self, row_number: int, note: str) -> None:
        """
        Append text to the notes column of a job.

        Args:
            row_number: 1-indexed row number.
            note: Text to append.
        """
        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        # First get existing notes
        col_letter = chr(ord("A") + COLUMNS["notes"])
        range_str = f"Jobs!{col_letter}{row_number}"

        def get_notes():
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=range_str
            ).execute()
            values = result.get("values", [[]])
            return values[0][0] if values and values[0] else ""

        existing = self._retry_with_backoff(get_notes)

        # Append new note with timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_note = f"[{timestamp}] {note}"
        if existing:
            combined = f"{existing}\n{new_note}"
        else:
            combined = new_note

        self.update_job(row_number, {"notes": combined})

    def add_date_to_column(self, row_number: int, column: str, date_str: str) -> None:
        """
        Add a date to a date column (semicolon-separated if multiple).

        Args:
            row_number: 1-indexed row number.
            column: Column name (oa_date, phone_date, tech_date).
            date_str: Date string to add.
        """
        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        if column not in COLUMNS:
            return

        col_letter = chr(ord("A") + COLUMNS[column])
        range_str = f"Jobs!{col_letter}{row_number}"

        def get_existing():
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=range_str
            ).execute()
            values = result.get("values", [[]])
            return values[0][0] if values and values[0] else ""

        existing = self._retry_with_backoff(get_existing)

        # Check if date already exists (prevent duplicates)
        if existing:
            existing_dates = [d.strip() for d in existing.split(";")]
            if date_str in existing_dates:
                return  # Date already exists, skip
            combined = f"{existing}; {date_str}"
        else:
            combined = date_str

        self.update_job(row_number, {column: combined})

    def _get_jobs_sheet_id(self) -> int:
        """Get the internal sheet ID for the Jobs tab."""
        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        def get_sheet_id():
            result = service.spreadsheets().get(
                spreadsheetId=sheet_id,
                fields="sheets.properties"
            ).execute()
            for sheet in result.get("sheets", []):
                if sheet["properties"]["title"] == "Jobs":
                    return sheet["properties"]["sheetId"]
            return 0  # Fallback to first sheet

        return self._retry_with_backoff(get_sheet_id)

    def _hex_to_rgb(self, hex_color: str) -> dict:
        """
        Convert hex color to Google Sheets RGB format (0-1 floats).

        Args:
            hex_color: Hex color string like "#E3F2FD"

        Returns:
            Dict with red, green, blue keys (0-1 float values)
        """
        hex_color = hex_color.lstrip("#")
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
        return {"red": r, "green": g, "blue": b}

    def set_row_color(self, row_number: int, hex_color: str) -> None:
        """
        Set background color for an entire row.

        Args:
            row_number: 1-indexed row number.
            hex_color: Hex color string like "#E3F2FD"
        """
        service = self._get_service()
        sheet_id = self.config.google_sheet_id
        jobs_sheet_id = self._get_jobs_sheet_id()

        rgb = self._hex_to_rgb(hex_color)

        request = {
            "repeatCell": {
                "range": {
                    "sheetId": jobs_sheet_id,
                    "startRowIndex": row_number - 1,  # 0-indexed
                    "endRowIndex": row_number,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(COLUMNS)
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": rgb
                    }
                },
                "fields": "userEnteredFormat.backgroundColor"
            }
        }

        def apply_color():
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [request]}
            ).execute()

        self._retry_with_backoff(apply_color)

    def apply_status_color(self, row_number: int, status: str) -> None:
        """
        Apply the configured color for a status to a row.

        Args:
            row_number: 1-indexed row number.
            status: Status value (Applied, OA, Phone, Technical, Offer, Rejected)
        """
        color = self.config.status_colors.get(status)
        if color:
            self.set_row_color(row_number, color)


# Singleton client instance
_client: Optional[SheetsClient] = None


def get_sheets_client() -> SheetsClient:
    """Get the global SheetsClient instance."""
    global _client
    if _client is None:
        _client = SheetsClient()
    return _client

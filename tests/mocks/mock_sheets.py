"""
Mock Google Sheets client for testing.
Stores data in memory instead of making API calls.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from src.sheets import JobRow, COLUMNS, HEADERS, STATUS_NEW


class MockSheetsClient:
    """In-memory mock of SheetsClient for testing."""

    def __init__(self):
        # Store rows as list of lists (like spreadsheet)
        # Row 0 = headers, Row 1+ = data
        self.rows: List[List[str]] = [HEADERS.copy()]
        self.row_colors: Dict[int, str] = {}  # row_number -> hex_color

    def ensure_headers(self) -> None:
        """Headers are always present in mock."""
        pass

    def get_all_jobs(self) -> List[JobRow]:
        """Get all jobs from mock storage."""
        jobs = []
        for i, row in enumerate(self.rows[1:], start=2):  # Skip header, 1-indexed
            # Pad row to full width
            padded = row + [""] * (len(COLUMNS) - len(row))
            jobs.append(JobRow.from_row(i, padded))
        return jobs

    def add_job(self, job_data: Dict[str, Any]) -> int:
        """Add a new job to mock storage."""
        # Build row values
        row = [""] * len(COLUMNS)

        for key, idx in COLUMNS.items():
            if key in job_data:
                value = job_data[key]
                if value is not None:
                    row[idx] = str(value)

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

        self.rows.append(row)
        return len(self.rows)  # Row number (1-indexed, includes header)

    def update_job(self, row_number: int, updates: Dict[str, Any]) -> None:
        """Update an existing job row."""
        if row_number < 2 or row_number > len(self.rows):
            return  # Invalid row

        row_idx = row_number - 1  # Convert to 0-indexed
        row = self.rows[row_idx]

        # Ensure row is padded
        while len(row) < len(COLUMNS):
            row.append("")

        for key, value in updates.items():
            if key in COLUMNS:
                col_idx = COLUMNS[key]
                row[col_idx] = str(value) if value is not None else ""

    def find_jobs_by_company(self, company_name: str) -> List[JobRow]:
        """Find jobs by company name."""
        all_jobs = self.get_all_jobs()
        company_lower = company_name.lower()

        matches = [
            job for job in all_jobs
            if company_lower in job.company.lower()
        ]

        matches.sort(key=lambda j: j.added_date, reverse=True)
        return matches

    def find_jobs_by_company_and_position(self, company_name: str, position: str) -> List[JobRow]:
        """Find jobs by company and position."""
        all_jobs = self.get_all_jobs()
        company_lower = company_name.lower()
        position_lower = position.lower()

        matches = [
            job for job in all_jobs
            if company_lower in job.company.lower()
            and position_lower in job.position.lower()
        ]

        matches.sort(key=lambda j: j.added_date, reverse=True)
        return matches

    def job_exists(self, company: str, position: str) -> bool:
        """Check if job already exists."""
        all_jobs = self.get_all_jobs()
        company_lower = company.lower()
        position_lower = position.lower()

        for job in all_jobs:
            if (job.company.lower() == company_lower and
                job.position.lower() == position_lower):
                return True
        return False

    def append_to_notes(self, row_number: int, note: str) -> None:
        """Append to notes column."""
        if row_number < 2 or row_number > len(self.rows):
            return

        row_idx = row_number - 1
        row = self.rows[row_idx]

        while len(row) < len(COLUMNS):
            row.append("")

        notes_idx = COLUMNS["notes"]
        existing = row[notes_idx]

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_note = f"[{timestamp}] {note}"

        if existing:
            row[notes_idx] = f"{existing}\n{new_note}"
        else:
            row[notes_idx] = new_note

    def add_date_to_column(self, row_number: int, column: str, date_str: str) -> None:
        """Add date to a date column (semicolon-separated)."""
        if row_number < 2 or row_number > len(self.rows):
            return

        if column not in COLUMNS:
            return

        row_idx = row_number - 1
        row = self.rows[row_idx]

        while len(row) < len(COLUMNS):
            row.append("")

        col_idx = COLUMNS[column]
        existing = row[col_idx]

        if existing:
            existing_dates = [d.strip() for d in existing.split(";")]
            if date_str in existing_dates:
                return  # Already exists
            row[col_idx] = f"{existing}; {date_str}"
        else:
            row[col_idx] = date_str

    def set_row_color(self, row_number: int, hex_color: str) -> None:
        """Set row color (stored but no visual effect in tests)."""
        self.row_colors[row_number] = hex_color

    def apply_status_color(self, row_number: int, status: str) -> None:
        """Apply status color (no-op in mock, just tracks it)."""
        # In real client this would apply color based on config
        # For mock, we just record that it was called
        pass

    def clear(self) -> None:
        """Clear all data (for test cleanup)."""
        self.rows = [HEADERS.copy()]
        self.row_colors = {}

    def get_row_count(self) -> int:
        """Get number of data rows (excluding header)."""
        return len(self.rows) - 1

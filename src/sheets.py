"""
Google Sheets integration for ApplyPotato.
Handles all CRUD operations for the Jobs tab.
"""

import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import get_config, Config


logger = logging.getLogger(__name__)


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


# Google Sheets serial-date epoch: serial 0 is 1899-12-30.
SHEETS_EPOCH = datetime(1899, 12, 30)

# Formats a date cell can arrive in. %m and %d accept both "07" and "7", so each
# entry covers the zero-padded form Python writes and the unpadded form Sheets displays.
_CELL_DATE_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y")


def parse_sheet_datetime(value: Any) -> Optional[datetime]:
    """
    Parse a single date cell value into a datetime.

    Handles every shape a date column can come back as:
    - Sheets serial numbers (what valueRenderOption=FORMULA returns; the fractional
      part carries the time of day)
    - ISO 8601 strings (used by the JSON state files)
    - "M/D/YYYY H:MM:SS" / "M/D/YYYY", zero-padded or not

    Returns None if the value is blank or unparseable. Pass one value, not a
    semicolon-joined cell — use split_date_cell() for that.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text:
        return None

    # Sheets serial number (days since 1899-12-30). "inf" raises OverflowError.
    try:
        return SHEETS_EPOCH + timedelta(days=float(text))
    except (ValueError, TypeError, OverflowError):
        pass

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass

    for fmt in _CELL_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None


def split_date_cell(value: Any) -> List[str]:
    """Split a possibly semicolon-joined date cell into trimmed, non-empty segments."""
    if value is None:
        return []
    return [segment.strip() for segment in str(value).split(";") if segment.strip()]


def split_stages(value: Any) -> List[str]:
    """
    Read the Completed Stages cell into a list of canonical stage names.

    Matching is case-insensitive so a hand-typed "oa" counts, and unknown text is kept
    verbatim rather than dropped — the cell is user-editable and silently discarding
    something they typed would be worse than carrying it along.
    """
    canonical = {s.lower(): s for s in COMPLETABLE_STAGES}
    out: List[str] = []
    for segment in split_date_cell(value):
        name = canonical.get(segment.lower(), segment)
        if name not in out:
            out.append(name)
    return out


def add_stage(value: Any, stage: str) -> str:
    """
    Return the cell value with `stage` recorded, preserving order and deduping.

    Returns the cell unchanged when the stage is already present, so a re-processed
    email cannot append a second copy.
    """
    stages = split_stages(value)
    canonical = {s.lower(): s for s in COMPLETABLE_STAGES}
    name = canonical.get((stage or "").strip().lower())
    if not name:
        raise ValueError(f"unknown stage {stage!r}; expected one of {COMPLETABLE_STAGES}")
    if name not in stages:
        stages.append(name)
    # Keep pipeline order rather than arrival order, so the cell reads the way the
    # process runs regardless of which email landed first.
    order = {s: i for i, s in enumerate(COMPLETABLE_STAGES)}
    stages.sort(key=lambda s: order.get(s, len(order)))
    return "; ".join(stages)


def remove_stage(value: Any, stage: str) -> str:
    """Return the cell value with `stage` removed; used to undo a wrong mark."""
    name = (stage or "").strip().lower()
    return "; ".join(s for s in split_stages(value) if s.lower() != name)


def date_already_recorded(existing: Any, date_str: str) -> bool:
    """
    Check whether date_str's calendar date is already present in a date cell.

    Compares parsed dates rather than raw text. The cell is read back as
    FORMATTED_VALUE, so an existing real datetime arrives unpadded and with a time
    ("7/23/2026 20:28:52") while the incoming value is zero-padded and may be
    date-only ("07/23/2026"). A raw string compare never matches those, which is
    what caused the same date to be appended to a cell over and over.

    Args:
        existing: Current cell value (formatted text, a serial number, or a
            semicolon-joined mix of both).
        date_str: The value about to be written.

    Returns:
        True if the same calendar date is already in the cell.
    """
    new_dt = parse_sheet_datetime(date_str)
    target = str(date_str).strip()

    for segment in split_date_cell(existing):
        # Exact match first, so unparseable-but-identical text still dedupes.
        if segment == target:
            return True
        if new_dt is None:
            continue
        existing_dt = parse_sheet_datetime(segment)
        if existing_dt and existing_dt.date() == new_dt.date():
            return True

    return False


# Google Sheets API scopes
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Column mapping (0-indexed)
COLUMNS = {
    "company": 0,               # A
    "position": 1,              # B
    "status": 2,                # C
    "job_posting_date": 3,      # D
    "dream": 4,                 # E
    "application_date": 5,      # F
    "oa_date": 6,               # G
    "phone_date": 7,            # H
    "tech_date": 8,             # I
    "fit_score": 9,             # J
    "salary": 10,               # K
    "job_type": 11,             # L
    "work_model": 12,           # M
    "location": 13,             # N
    "season_year": 14,          # O
    "deadline": 15,             # P
    "source": 16,               # Q
    "added_date": 17,           # R
    "resume_needed": 18,        # S
    "cover_letter_needed": 19,  # T
    "notes": 20,                # U
    "last_email_time": 21,      # V
    "completed_stages": 22,     # W
    "last_event": 23,          # X
}

# Header row (must match column order)
HEADERS = [
    "Company", "Position", "Status", "Job Posting Date", "Dream",
    "Application Date", "OA Date", "Phone Interview Date", "Tech Interview Date",
    "Fit Score", "Salary", "Job Type", "Work Model", "Location", "Season/Year",
    "Deadline", "Source", "Added Date", "Resume", "Cover Letter", "Notes",
    "Last Email Time", "Completed Stages", "Last Event"
]

# Stages that can be finished, in pipeline order. Offer is absent on purpose: it is an
# outcome, not something the applicant completes.
COMPLETABLE_STAGES = ("OA", "Phone", "Technical")

# The date column that proves a row reached each stage. Status alone is not enough: a
# rejected row shows "Rejected", but its OA Date still records that the assessment
# happened, which is how a completion can be attributed after the outcome arrives.
STAGE_DATE_COLUMN = {
    "OA": "oa_date",
    "Phone": "phone_date",
    "Technical": "tech_date",
}


# What the Last Event cell says, per classifier category.
#
# Named for the event rather than the stage, because the obvious spellings collide with
# columns that mean something else. "Stage Done" reads as a restatement of Completed
# Stages, and "Applied" duplicates Status while meaning a different thing — the stage a
# row sits at versus the email that just arrived.
EVENT_LABELS = {
    "confirmation": "Application Received",
    "oa": "OA Invite",
    "phone": "Phone Invite",
    "technical": "Technical Invite",
    "stage_done": "Assessment Submitted",
    "offer": "Offer",
    "rejection": "Rejected",
}

# Recovering the key from the label. Includes the spellings this column used earlier
# today, so rows written before the rename still resolve rather than silently ceasing to
# match.
_EVENT_KEYS = {label.lower(): key for key, label in EVENT_LABELS.items()}
_EVENT_KEYS.update({
    "oa": "oa", "phone": "phone", "technical": "technical",
    "stage done": "stage_done", "stage_done": "stage_done",
    "confirmation": "confirmation", "rejection": "rejection",
})


def event_label(category: str) -> str:
    """The sheet-facing spelling of a classifier category."""
    key = (category or "").strip().lower()
    return EVENT_LABELS.get(key, key.replace("_", " ").title())


def event_key(cell: str) -> str:
    """The classifier key behind a Last Event cell, tolerating older spellings."""
    text = (cell or "").strip().lower()
    return _EVENT_KEYS.get(text, text.replace(" ", "_"))


def reached_stage(job, stage: str) -> bool:
    """
    Whether this row got as far as `stage`, whatever its status is now.

    True when the stage's date cell is filled, or the row is sitting at the stage with
    the date not yet written. Deliberately independent of the outcome: rows 261, 262, 315
    and 317 are all Rejected with an OA Date, and their assessments were still taken.
    """
    if stage not in STAGE_DATE_COLUMN:
        return False
    if (getattr(job, "status", "") or "") == stage:
        return True
    return bool(str(getattr(job, STAGE_DATE_COLUMN[stage], "") or "").strip())


# Words a company carries at the end of its legal name and drops from its own
# abbreviation: nobody writes IBM Corporation's acronym as "IBMC".
LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "lllp", "llp", "lp", "corp", "corporation",
    "co", "company", "ltd", "limited", "plc", "sa", "ag", "nv", "bv", "gmbh",
    "holdings", "holding",
}

# Below three characters an acronym stops identifying anyone: on the sheet as it stands
# "MS" is Maven Securities, Morgan Stanley and Motorola Solutions at once, and "CS" is
# Citadel Securities, which is not who the world means by CS.
MIN_ACRONYM_LEN = 3


def company_acronyms(name: str) -> Set[str]:
    """
    The acronyms `name` could reasonably be written as, upper-cased.

    Both the whole name's initials and the initials left after peeling off trailing legal
    suffixes, since a company may or may not be quoted with its "Inc." — "Tyson Foods,
    Inc." yields TFI and TF. "&" is dropped rather than counted, so Stanley Black &
    Decker is SBD and not SB&D. Suffix peeling stops at the first word that is not a
    suffix, which is what keeps "Group" in Jump Trading Group (JTG) — the word is part of
    the name traders use, not boilerplate.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9&]+", name or "") if w and w != "&"]
    found = set()
    while len(words) >= 2:
        found.add("".join(word[0] for word in words).upper())
        if words[-1].lower() not in LEGAL_SUFFIXES:
            break
        words = words[:-1]
    return {acronym for acronym in found if len(acronym) >= MIN_ACRONYM_LEN}


def as_acronym(text: str) -> Optional[str]:
    """
    `text` as an acronym token, or None when it is not one.

    Requires all caps: a company written out in title case is a name, and reading "Sun"
    as an acronym would hand ordinary English words the power to claim a row. Trailing
    legal suffixes come off first, because a classifier reading a signature block reports
    "IBM Corporation" as readily as "IBM" and both are the same three letters.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9&]+", text or "") if w and w != "&"]
    while len(words) > 1 and words[-1].lower() in LEGAL_SUFFIXES:
        words = words[:-1]
    # Punctuation goes last, so "I.B.M." and "SB&D" arrive as the letters they stand for.
    bare = re.sub(r"[^A-Za-z0-9]", "", "".join(words))
    if len(bare) < MIN_ACRONYM_LEN or not bare.isalpha() or not bare.isupper():
        return None
    return bare


def shows_application(job) -> bool:
    """
    Whether this row carries evidence that the application was actually made.

    Status past New, or an Application Date, either alone. Deliberately weak evidence:
    the sheet is full of rows that were really applied to and still read New because
    their confirmation was misfiled, dropped by a filter, or named a company the row did
    not. So this can never be read as "New means not applied" — only as "this one row,
    unlike its siblings, has something on it".

    Used solely to break a tie the matcher could not otherwise resolve. It never
    overrides a match; it only chooses among rows that were about to be abandoned.
    """
    status = (getattr(job, "status", "") or "").strip()
    if status and status != STATUS_NEW:
        return True
    return bool(str(getattr(job, "application_date", "") or "").strip())


def has_live_application(job) -> bool:
    """
    Whether this row is an application still in play.

    `shows_application` asks whether the application was ever made; this asks whether
    it is still going. The difference is the whole point: a company whose only
    application ended in a rejection is open again, and a second role there is worth
    seeing, while a company with an application in flight only produces noise.

    Terminal is Rejected or Ghosted, the same pair check_gmail.py treats as ends. Note
    the asymmetry with `shows_application`, which counts *any* status past New: there a
    rejection is still evidence the user applied, which is exactly what that tie-break
    needs. Here it is evidence they are finished.
    """
    if not shows_application(job):
        return False
    return (getattr(job, "status", "") or "").strip() not in (
        STATUS_REJECTED,
        STATUS_GHOSTED,
    )


def company_matches(needle: str, company: str) -> bool:
    """
    Whether `needle` names `company`, matching only on whole alphanumeric runs.

    Plain containment is what let a Maven Securities assessment invite land on Akuna
    Capital row 317: the classifier had picked "UNA" out of the vendor's host name
    `webassessment.una-arcticshores.com`, and "una" is inside "akuna capital". Since row
    317 was the only un-struck Akuna row, the collision looked like a unique match and
    was written without hesitation. Run over the sheet as it stood — every company name in
    it plus every bare first word, 403 needles over 4694 rows — this rule changed 19 of
    them, and every single change was a false positive going away: "GE" no longer claiming
    Generac and Ivy Tech ColleGE, "ING" no longer claiming NightwING and Jump TradING,
    "SK" no longer claiming The Trade DeSK. Nothing gained a row.

    The test is "no alphanumeric on either side" rather than `\\b`, because `\\b` is
    defined against the neighbouring character and so misbehaves whenever the needle
    itself ends in punctuation — "Yahoo!" would stop matching "Yahoo! Inc". Punctuation
    and spaces are boundaries, so "Packard" matches "Hewlett-Packard" and "Amazon"
    matches "Amazon.com".

    A lower-to-upper transition is a boundary too, but only on the trailing side: the
    sheet spells row 453 "JPMorganChase", and a bare "JPMorgan" has to keep reaching it.
    Leading camel case is deliberately not a boundary, since brand names lead — honouring
    it would put "Scale" back on TribalScale. This cannot reopen the original bug either
    way: it reads the case of `company`, and "Akuna" has no transition at "una".

    Substring matching within a name is kept on purpose: an email says "Goldman" where
    the sheet says "Goldman Sachs". Only the ragged edge is removed. An empty needle
    matches nothing, where containment matched every row in the sheet.

    Failing that, one side may be the other's acronym, in either direction: IBM writes to
    students as IBM while row 631 was scraped as "International Business Machines
    Corporation", so its offer of an OA sat unmatched and the row stayed New. Substring
    matching cannot reach across an abbreviation, since none of the letters are adjacent.
    Swept over the sheet — 296 companies, 510 needles being every company name, every bare
    first word and every acronym any of those names yields — this reached 80 needles that
    previously matched nothing at all, each landing on exactly one company (IBM, HRT,
    CTC, BOA, TTD, PSU, NFCU…). No needle that already identified a single row was turned
    into a tie, because `MIN_ACRONYM_LEN` holds the collision-dense two-letter forms out:
    at two characters "Western Digital" would start claiming the six rows filed under
    "WD" alongside the row it correctly matches today.
    """
    needle = (needle or "").strip()
    company = (company or "").strip()
    if not needle:
        return False

    for hit in re.finditer(re.escape(needle), company, re.IGNORECASE):
        start, end = hit.span()
        if start > 0 and company[start - 1].isalnum():
            continue
        if end < len(company) and company[end].isalnum():
            # Except where the name is concatenated: "JPMorgan" + "Chase".
            if not (company[end].isupper() and company[end - 1].islower()):
                continue
        return True

    # The email abbreviates what the sheet spells out, or the reverse.
    needle_acronym = as_acronym(needle)
    if needle_acronym and needle_acronym in company_acronyms(company):
        return True
    company_acronym = as_acronym(company)
    if company_acronym and company_acronym in company_acronyms(needle):
        return True

    return False


# A requisition number is the one token in an application email that names the posting
# rather than the employer. Six digits is where they start being identifiers instead of
# quantities: it clears years, street numbers, ZIPs and dollar amounts, and every real id
# seen on this sheet is seven or eight.
MIN_REQUISITION_DIGITS = 6

_DIGIT_RUN = re.compile(rf"\d{{{MIN_REQUISITION_DIGITS},}}")


def requisition_ids(text: str) -> Set[str]:
    """
    Every long digit run in `text`, any of which may be a requisition number.

    No attempt is made to tell an id from a tracking number here. A candidate only
    matters if some row's URL contains it, and that intersection is what does the work —
    guessing which number "looks like" a requisition id would only add a way to be wrong.
    """
    return set(_DIGIT_RUN.findall(text or ""))


def url_contains_requisition(url: str, requisition_id: str) -> bool:
    """
    Whether `url` carries this exact requisition id, bounded by non-digits.

    Workday appends it to the slug ("...--Summer-2027---Onsite-_01999999"), so the
    boundary matters in one direction only, but both are checked: without it "1999999"
    would match inside "01999999" and a shorter id could claim a longer one's posting.
    """
    if not url or not requisition_id:
        return False

    for hit in re.finditer(re.escape(requisition_id), url):
        start, end = hit.span()
        if start > 0 and url[start - 1].isdigit():
            continue
        if end < len(url) and url[end].isdigit():
            continue
        return True

    return False


def col_letter(col_index: int) -> str:
    """
    Convert a 0-indexed column number to A1 letters: 0 -> A, 25 -> Z, 26 -> AA.

    The obvious chr(ord("A") + idx) is correct only through Z and then silently
    emits "[", so it would break the first time the schema crossed 26 columns.
    """
    letters = ""
    while True:
        col_index, remainder = divmod(col_index, 26)
        letters = chr(ord("A") + remainder) + letters
        if col_index == 0:
            return letters
        col_index -= 1


# Rightmost column of the schema, so every A1 range is derived rather than retyped.
LAST_COL = col_letter(len(COLUMNS) - 1)

# Number formats, declared by column *name* so adding a column never means
# recounting indices.
DATE_COLUMNS = ("job_posting_date", "oa_date", "phone_date", "tech_date", "deadline")
DATETIME_COLUMNS = ("application_date", "added_date", "last_email_time")

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
    dream: str           # "Yes" | "No" | ""
    fit_score: int
    salary: str
    job_type: str
    work_model: str
    location: str
    season_year: str
    deadline: str
    source: str
    added_date: str
    resume_needed: str      # "Yes" | "No" | ""
    cover_letter_needed: str  # "Yes" | "No" | ""
    notes: str
    # Arrival time of the most recent email that updated this row. Defaulted because
    # every other field is required and pre-existing rows have a blank cell.
    last_email_time: str = ""
    # Semicolon-joined stages the applicant has finished, e.g. "OA; Phone". A permanent
    # record, never cleared: it says which stages were completed, not what is outstanding.
    # Each stage appears at most once however many assessments a company sent, so counting
    # completions is counting rows whose cell contains the stage.
    completed_stages: str = ""
    # The most recent thing that happened on this row — "OA Invite", "Assessment
    # Submitted", "Rejected". Written by an arriving email and by a manual mark, which is
    # why it is an event rather than an email category. Paired with completed_stages it
    # distinguishes states the record alone cannot: W="OA" with X="OA Invite" means one
    # assessment is done and a newer one is waiting.
    last_event: str = ""

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
            dream=values[COLUMNS["dream"]],
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
            resume_needed=values[COLUMNS["resume_needed"]],
            cover_letter_needed=values[COLUMNS["cover_letter_needed"]],
            notes=values[COLUMNS["notes"]],
            last_email_time=values[COLUMNS["last_email_time"]],
            completed_stages=values[COLUMNS["completed_stages"]],
            last_event=values[COLUMNS["last_event"]],
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
        # Resolved lazily; the tab's numeric id can't change under a live client.
        self._jobs_sheet_id: Optional[int] = None
        # Headers + number formats are a per-process concern; see ensure_headers().
        self._schema_ensured: bool = False

    @property
    def _tab(self) -> str:
        """The Jobs tab name, always quoted so A1 notation is safe for any name."""
        return "'" + self.config.jobs_sheet_tab.replace("'", "''") + "'"

    def _range(self, a1: str) -> str:
        """Build an A1 range against the Jobs tab: "A2:V" -> "'Jobs'!A2:V"."""
        return f"{self._tab}!{a1}"

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
        """Create or rename a sheet to the configured Jobs tab if it doesn't exist."""
        service = self._get_service()
        sheet_id = self.config.google_sheet_id
        tab = self.config.jobs_sheet_tab

        # Get existing sheets with their IDs
        def get_sheets():
            result = service.spreadsheets().get(
                spreadsheetId=sheet_id,
                fields="sheets.properties"
            ).execute()
            return result.get("sheets", [])

        sheets = self._retry_with_backoff(get_sheets)
        sheet_titles = [s["properties"]["title"] for s in sheets]

        if tab in sheet_titles:
            return  # Already exists

        # Try to rename Sheet1 to the Jobs tab if it exists
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
                                        "title": tab
                                    },
                                    "fields": "title"
                                }
                            }]
                        }
                    ).execute()

                self._retry_with_backoff(rename_sheet)
                self._jobs_sheet_id = None  # Title moved; any resolved id is stale
                print(f"Renamed 'Sheet1' to '{tab}'")
                return

        # No Sheet1 found, create a new Jobs tab
        def create_sheet():
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "requests": [{
                        "addSheet": {
                            "properties": {"title": tab}
                        }
                    }]
                }
            ).execute()

        self._retry_with_backoff(create_sheet)
        self._jobs_sheet_id = None  # A brand-new tab has an id we haven't seen
        print(f"Created '{tab}' sheet")

    def _ensure_date_formatting(self) -> None:
        """Apply MM/DD/YYYY date format to date columns."""
        service = self._get_service()
        sheet_id = self.config.google_sheet_id
        jobs_sheet_id = self._get_jobs_sheet_id()

        # Date-only vs date+time columns, resolved from their names. The datetime ones
        # store a real timestamp so the daily summary can window activity within the day.
        date_column_indices = [COLUMNS[name] for name in DATE_COLUMNS]
        datetime_column_indices = [COLUMNS[name] for name in DATETIME_COLUMNS]

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

        for col_idx in datetime_column_indices:
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
                                "type": "DATE_TIME",
                                "pattern": "M/d/yyyy H:mm:ss"
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
                        "endColumnIndex": len(COLUMNS)
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
        """
        Ensure the Jobs sheet has correct headers.

        Costs 3 API calls even when nothing needs fixing, and the last of them is a
        *write* — a full-column repeatCell across every date column plus a filter reset.
        The layout cannot change under a live process, so do it once per client and let
        both schedulers call it every cycle for free.
        """
        if self._schema_ensured:
            return

        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        # First ensure the Jobs sheet exists
        self._ensure_jobs_sheet_exists()

        def check_headers():
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=self._range(f"A1:{LAST_COL}1")
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
                    range=self._range(f"A1:{LAST_COL}1"),
                    valueInputOption="RAW",
                    body={"values": [HEADERS]}
                ).execute()

            self._retry_with_backoff(set_headers)

        # Ensure date columns have proper formatting
        self._ensure_date_formatting()

        # Only after everything succeeded — a transient failure must retry next run
        self._schema_ensured = True

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
                range=self._range(f"A2:{LAST_COL}"),  # Skip header row
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
            row[COLUMNS["added_date"]] = datetime.now().strftime("%m/%d/%Y %H:%M:%S")

        def append():
            result = service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=self._range(f"A:{LAST_COL}"),
                valueInputOption="USER_ENTERED",  # Parse formulas
                insertDataOption="INSERT_ROWS",
                body={"values": [row]}
            ).execute()
            return result

        result = self._retry_with_backoff(append)

        # Parse the updated range to get row number
        updated_range = result.get("updates", {}).get("updatedRange", "")
        # Format: Jobs!A123:U123. Split from the right — a quoted tab name may
        # itself contain "!" — then drop the column letters off the first cell.
        try:
            first_cell = updated_range.rsplit("!", 1)[-1].split(":")[0]
            row_num = int(first_cell.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        except (IndexError, ValueError):
            row_num = -1

        # INSERT_ROWS inherits the row above's formatting — that is Sheets' behaviour for
        # an appended row, not a choice this code makes, and values().append() offers no
        # way to opt out. So the row is normalized right after it lands. The row is
        # written either way: a formatting failure must not read as a failed append.
        if row_num > 0:
            try:
                self._normalize_appended_row(row_num, row[COLUMNS["status"]])
            except Exception as e:
                # Must reach the log file, not stdout: the append already succeeded so
                # nothing retries, and the row keeps the inherited formatting forever.
                # Under a background service a print() goes nowhere, making this
                # indistinguishable from running stale pre-fix code.
                logger.warning(f"Could not normalize row {row_num} formatting: {e}")
        else:
            logger.warning(
                f"Could not parse a row number from {updated_range!r}; "
                f"row not painted and may keep the row above's color"
            )

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
                range_str = self._range(f"{col_letter(col_idx)}{row_number}")
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
        Find jobs by company name (case-insensitive, on alphanumeric boundaries).

        Args:
            company_name: Company name to search for.

        Returns:
            List of matching JobRow objects, sorted by added_date descending.
        """
        all_jobs = self.get_all_jobs()

        matches = [
            job for job in all_jobs
            if company_matches(company_name, job.company)
        ]

        # Sort by added_date descending (most recent first)
        matches.sort(key=lambda j: j.added_date, reverse=True)

        return matches

    def find_jobs_by_requisition_id(self, ids) -> List[JobRow]:
        """
        Find jobs whose posting URL carries any of these requisition ids.

        The id identifies the posting, where the company name identifies at best the
        employer and at worst its parent: Workday signs Pratt & Whitney's receipts
        "RTX Corporation", which reaches none of that subsidiary's rows and five of the
        parent's. The id in the same email reaches exactly one.

        Args:
            ids: Candidate requisition ids, as returned by requisition_ids().

        Returns:
            Matching JobRow objects, sorted by added_date descending.
        """
        ids = {str(i) for i in (ids or set()) if str(i)}
        if not ids:
            return []

        matches = [
            job for job in self.get_all_jobs()
            if any(url_contains_requisition(job.position_url or "", i) for i in ids)
        ]

        matches.sort(key=lambda j: j.added_date, reverse=True)

        return matches

    def find_jobs_by_company_and_position(self, company_name: str, position: str) -> List[JobRow]:
        """
        Find jobs by company name AND position.

        The company is matched on alphanumeric boundaries, the position by plain
        containment. The asymmetry is deliberate: the company is the row's identity and a
        ragged-edge hit there picks the wrong employer outright, while the position only
        narrows within one employer's rows and routinely differs by a suffix — an email
        saying "Software Engineer Intern" has to keep matching a row spelled
        "Software Engineer Internship", which a boundary would forbid.

        Args:
            company_name: Company name to search for.
            position: Position/title to search for.

        Returns:
            List of matching JobRow objects, sorted by added_date descending.
        """
        all_jobs = self.get_all_jobs()
        position_lower = position.lower()

        matches = [
            job for job in all_jobs
            if company_matches(company_name, job.company)
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
        range_str = self._range(f"{col_letter(COLUMNS['notes'])}{row_number}")

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

        For event columns that can legitimately hold several dates — a rescheduled
        assessment, a second interview round. NOT for application_date, which is
        single-valued; write that one through update_job.

        Args:
            row_number: 1-indexed row number.
            column: Column name (oa_date, phone_date, tech_date).
            date_str: Date string to add.
        """
        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        if column not in COLUMNS:
            return

        range_str = self._range(f"{col_letter(COLUMNS[column])}{row_number}")

        def get_existing():
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=range_str
            ).execute()
            values = result.get("values", [[]])
            return values[0][0] if values and values[0] else ""

        existing = self._retry_with_backoff(get_existing)

        # Skip if this calendar date is already recorded. Compares parsed dates, not
        # raw text: the read above returns FORMATTED_VALUE, so an existing datetime
        # comes back unpadded while date_str is zero-padded.
        if existing:
            if date_already_recorded(existing, date_str):
                return
            combined = f"{existing}; {date_str}"
        else:
            combined = date_str

        self.update_job(row_number, {column: combined})

    def _get_jobs_sheet_id(self) -> int:
        """
        Get the internal sheet ID (gid) for the Jobs tab.

        batchUpdate addresses cells by gid rather than by tab name, so every color
        or format write needs this number. It costs an API call to look up and
        can't change while the client is alive, so it's resolved once and kept.
        """
        if self._jobs_sheet_id is not None:
            return self._jobs_sheet_id

        service = self._get_service()
        sheet_id = self.config.google_sheet_id
        tab = self.config.jobs_sheet_tab

        def get_sheet_id():
            result = service.spreadsheets().get(
                spreadsheetId=sheet_id,
                fields="sheets.properties"
            ).execute()
            for sheet in result.get("sheets", []):
                if sheet["properties"]["title"] == tab:
                    return sheet["properties"]["sheetId"]
            return 0  # Fallback to first sheet

        self._jobs_sheet_id = self._retry_with_backoff(get_sheet_id)
        return self._jobs_sheet_id

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
            status: Status value (New, Applied, OA, Phone, Technical, Offer, Rejected)
        """
        color = self.config.status_colors.get(status)
        if color:
            self.set_row_color(row_number, color)

    def _normalize_appended_row(self, row_number: int, status: str) -> None:
        """
        Undo the formatting an appended row inherited from the row above it.

        Sheets copies the preceding row's format onto a row inserted by
        values().append(insertDataOption="INSERT_ROWS"), and that call takes no
        formatting argument, so the only options are to fix the row afterwards or to
        abandon append for insertDimension(inheritFromBefore=False) plus a separate
        write. This is the former.

        Resetting only the properties known to be set elsewhere would repeat the bug
        each time a new one is introduced: the colour was reset here, strikethrough was
        not, so striking the bottom row of the sheet silently struck every job appended
        after it — rows 420-462 — and the Gmail matcher skips struck rows, so those jobs
        became invisible to status updates. So strikethrough, bold, italic and underline
        are all cleared together.

        They are named individually, though, and the mask must not be collapsed to the
        textFormat subtree: that also deletes textFormat.link, which is the hyperlink on
        the Position cell rather than inherited formatting, and cost rows 518-573 theirs.
        A property added to the payload has to be added to the mask by hand.

        numberFormat is deliberately left alone: ensure_headers() sets date formats on
        the event-date columns, and resetting those would break date parsing.

        Both requests go in one batchUpdate — one round trip, and one failure mode for
        the caller to log.
        """
        service = self._get_service()
        sheet_id = self.config.google_sheet_id
        jobs_sheet_id = self._get_jobs_sheet_id()

        row_range = {
            "sheetId": jobs_sheet_id,
            "startRowIndex": row_number - 1,  # 0-indexed
            "endRowIndex": row_number,
            "startColumnIndex": 0,
            "endColumnIndex": len(COLUMNS),
        }

        requests = [{
            "repeatCell": {
                "range": row_range,
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {
                            "strikethrough": False,
                            "bold": False,
                            "italic": False,
                            "underline": False,
                        }
                    }
                },
                # Named one by one rather than as the textFormat subtree: a mask naming a
                # subtree replaces all of it, which deleted textFormat.link on every row
                # appended between 2026-08-04 and 2026-08-08 (rows 518-573). append()
                # runs USER_ENTERED, so Sheets evaluates the =HYPERLINK() and materializes
                # the link a moment before this request would have wiped it. Adding a new
                # inherited property here means adding it to this mask too.
                "fields": (
                    "userEnteredFormat.textFormat.strikethrough,"
                    "userEnteredFormat.textFormat.bold,"
                    "userEnteredFormat.textFormat.italic,"
                    "userEnteredFormat.textFormat.underline"
                ),
            }
        }]

        color = self.config.status_colors.get(status)
        if color:
            requests.append({
                "repeatCell": {
                    "range": row_range,
                    "cell": {
                        "userEnteredFormat": {"backgroundColor": self._hex_to_rgb(color)}
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

        def apply():
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": requests},
            ).execute()

        self._retry_with_backoff(apply)

    def set_row_strikethrough(self, row_number: int, struck: bool = True) -> None:
        """
        Strike through (or un-strike) an entire row.

        Strikethrough marks a row as retired: a duplicate of another row, or a
        posting the user did not actually apply to. The Gmail matcher skips struck
        rows, so this is how an ambiguous company is narrowed to one live row.

        Args:
            row_number: 1-indexed row number.
            struck: True to strike through, False to clear it.
        """
        service = self._get_service()
        sheet_id = self.config.google_sheet_id
        jobs_sheet_id = self._get_jobs_sheet_id()

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
                        "textFormat": {"strikethrough": bool(struck)}
                    }
                },
                "fields": "userEnteredFormat.textFormat.strikethrough"
            }
        }

        def apply_strike():
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [request]}
            ).execute()

        self._retry_with_backoff(apply_strike)

    def get_struck_rows(self) -> set:
        """
        Row numbers whose text is struck through.

        Reads formatting, which values().get() does not return, so this is a
        separate API call from get_all_jobs(). Only columns A:B are inspected — a
        row counts as retired if either Company or Position is struck. That covers
        both natural gestures: striking the whole row, and striking just the
        position cell.

        Uses effectiveFormat so a strikethrough inherited from row- or
        column-level formatting counts the same as one applied to the cell.

        Returns:
            Set of 1-indexed row numbers. Empty if the sheet has no data.
        """
        service = self._get_service()
        sheet_id = self.config.google_sheet_id

        def fetch():
            return service.spreadsheets().get(
                spreadsheetId=sheet_id,
                ranges=[self._range("A2:B")],  # Skip header row
                includeGridData=True,
                fields="sheets.data.rowData.values.effectiveFormat.textFormat.strikethrough",
            ).execute()

        result = self._retry_with_backoff(fetch)

        struck = set()
        sheets = result.get("sheets", [])
        if not sheets:
            return struck

        data = sheets[0].get("data", [])
        if not data:
            return struck

        for i, row in enumerate(data[0].get("rowData", [])):
            row_number = i + 2  # 1-indexed, skip header
            for cell in row.get("values", []):
                fmt = cell.get("effectiveFormat", {}).get("textFormat", {})
                if fmt.get("strikethrough"):
                    struck.add(row_number)
                    break

        return struck


# Singleton client instance
_client: Optional[SheetsClient] = None


def get_sheets_client() -> SheetsClient:
    """Get the global SheetsClient instance."""
    global _client
    if _client is None:
        _client = SheetsClient()
    return _client

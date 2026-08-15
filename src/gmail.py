"""
Gmail integration for ApplyPotato.
Handles OAuth authentication and email fetching for status tracking.
"""

import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional

from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import get_config, Config


logger = logging.getLogger(__name__)

# Gmail API scope (read-only)
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# File to track processed emails (stored in data/ directory)
PROCESSED_EMAILS_FILENAME = "processed_emails.json"
PROCESSED_NEWSLETTER_EMAILS_FILENAME = "processed_newsletter_emails.json"

# OAuth token file (stored in auth/ directory)
TOKEN_FILENAME = "gmail_token.json"

# Maximum number of processed IDs to keep (prevents file from growing forever)
MAX_PROCESSED_IDS = 1000


# Gmail category label mapping
CATEGORY_LABELS = {
    "CATEGORY_PERSONAL": "Primary",
    "CATEGORY_SOCIAL": "Social",
    "CATEGORY_PROMOTIONS": "Promotions",
    "CATEGORY_UPDATES": "Updates",
    "CATEGORY_FORUMS": "Forums",
}


def account_slug(email: str) -> str:
    """Turn an email address into a filename-safe slug."""
    return re.sub(r"[^a-z0-9]+", "_", email.strip().lower()).strip("_")


def normalize_gmail_address(email: str) -> str:
    """
    Canonicalize an address for identity comparison.

    Gmail treats dots as insignificant and ignores everything after a '+' in the
    local part, and googlemail.com is an alias for gmail.com. So
    'johndoe@gmail.com' and 'john.doe@gmail.com' are the same mailbox and
    must compare equal. Non-Gmail domains (e.g. school .edu accounts) are only
    lowercased.
    """
    email = email.strip().lower()
    if "@" not in email:
        return email

    local, domain = email.rsplit("@", 1)
    if domain == "googlemail.com":
        domain = "gmail.com"
    if domain == "gmail.com":
        local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}"


@dataclass
class EmailMessage:
    """Represents an email message from Gmail."""
    message_id: str
    subject: str
    sender: str
    sender_email: str
    date: datetime
    body_text: str
    body_html: str
    category: str  # Primary, Social, Promotions, Updates, Forums, or Unknown
    account: str = ""  # Which configured Gmail account this came from ("" = single-account mode)


def body_as_text(email: EmailMessage) -> str:
    """
    The email's body as plain text, converting the HTML part when there is no text one.

    Half of real inbox traffic is HTML-only — 370 of 744 messages over a 30-day sample,
    Workday's application receipts among them — so reading `body_text` alone sees an
    empty string for every second email.
    """
    body = email.body_text or ""
    if body.strip():
        return body

    if not (email.body_html or "").strip():
        return ""

    soup = BeautifulSoup(email.body_html, "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()
    return soup.get_text(separator="\n", strip=True)


def message_text(email: EmailMessage) -> str:
    """Subject and body together, for anything scanning the whole message."""
    return f"{email.subject or ''}\n{body_as_text(email)}"


class GmailClient:
    """
    Client for Gmail API operations.

    One instance per Gmail account. Each account keeps its own OAuth token and
    its own processed-email caches, suffixed with the account slug. When no
    accounts are configured (GMAIL_ACCOUNTS blank), the legacy unsuffixed
    filenames are used so existing single-account installs keep working.
    """

    def __init__(self, config: Optional[Config] = None, account: Optional[str] = None):
        """
        Initialize Gmail client.

        Args:
            config: Optional config object. Uses global config if not provided.
            account: Email address of the account this client reads from.
                     None/"" selects legacy single-account mode.
        """
        self.config = config or get_config()
        self.account = (account or "").strip()
        self.label = self.account or "default"
        self._slug = account_slug(self.account) if self.account else ""

        self.token_path = self.config.auth_dir / self._state_filename(TOKEN_FILENAME)
        self.processed_path = self.config.data_dir / self._state_filename(PROCESSED_EMAILS_FILENAME)
        self.newsletter_processed_path = (
            self.config.data_dir / self._state_filename(PROCESSED_NEWSLETTER_EMAILS_FILENAME)
        )

        self._service = None
        self._creds = None
        self._verified = False
        self._processed_ids: set = set()
        self._migrate_legacy_state()
        self._load_processed_ids()

    def _state_filename(self, filename: str) -> str:
        """Insert the account slug before the file extension (no-op in legacy mode)."""
        if not self._slug:
            return filename
        stem, ext = filename.rsplit(".", 1)
        return f"{stem}_{self._slug}.{ext}"

    def _migrate_legacy_state(self) -> None:
        """
        Hand the legacy unsuffixed token/caches to the first configured account.

        Without this, turning on GMAIL_ACCOUNTS would force a re-auth of the
        primary mailbox and re-process every email in the lookback window.
        """
        accounts = self.config.gmail_accounts
        if not self._slug or not accounts or self.account != accounts[0]:
            return

        pairs = [
            (self.config.auth_dir / TOKEN_FILENAME, self.token_path),
            (self.config.data_dir / PROCESSED_EMAILS_FILENAME, self.processed_path),
            (self.config.data_dir / PROCESSED_NEWSLETTER_EMAILS_FILENAME, self.newsletter_processed_path),
        ]
        for legacy, per_account in pairs:
            if legacy.exists() and not per_account.exists():
                try:
                    legacy.rename(per_account)
                    logger.info(f"Migrated {legacy.name} -> {per_account.name} for account {self.account}")
                except OSError as e:
                    logger.warning(f"Failed to migrate {legacy.name}: {e}")

    def _get_credentials(self) -> Credentials:
        """Get or refresh Google API credentials for Gmail."""
        if self._creds and self._creds.valid:
            return self._creds

        token_path = self.token_path

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
                # Pre-select the right Google account on the consent screen
                extra = {"login_hint": self.account} if self.account else {}
                self._creds = flow.run_local_server(
                    port=self.config.oauth_local_port,
                    open_browser=True,
                    timeout_seconds=self.config.oauth_timeout_seconds,
                    **extra
                )

            # Save credentials for next run
            with open(token_path, "w") as token:
                token.write(self._creds.to_json())

        return self._creds

    def _get_service(self):
        """Get Gmail API service instance."""
        if self._service is None:
            creds = self._get_credentials()
            self._service = build("gmail", "v1", credentials=creds)
            self._verify_account(self._service)
        return self._service

    def authorized_email(self) -> str:
        """Return the email address this client's token is actually authorized for."""
        profile = self._get_service().users().getProfile(userId="me").execute()
        return profile.get("emailAddress", "")

    def _verify_account(self, service) -> None:
        """
        Guard against a token authorized for the wrong mailbox.

        It is easy to pick the wrong account on Google's consent screen; without
        this check the wrong inbox gets scanned silently under the right label.
        """
        if not self.account or self._verified:
            return

        try:
            profile = service.users().getProfile(userId="me").execute()
        except HttpError as e:
            logger.warning(f"Could not verify Gmail account identity for {self.account}: {e}")
            return

        authorized = (profile.get("emailAddress") or "").strip()
        if authorized and normalize_gmail_address(authorized) != normalize_gmail_address(self.account):
            raise RuntimeError(
                f"Gmail token for '{self.account}' is actually authorized as '{authorized}'.\n"
                f"Delete {self.token_path} and re-run: python check_gmail.py --auth"
            )

        self._verified = True

    def _load_processed_ids(self) -> None:
        """Load processed email IDs from file."""
        processed_file = self.processed_path
        if processed_file.exists():
            try:
                with open(processed_file, "r") as f:
                    data = json.load(f)
                    self._processed_ids = set(data.get("processed_ids", []))
                    logger.debug(f"Loaded {len(self._processed_ids)} processed email IDs")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load processed emails file: {e}")
                self._processed_ids = set()
        else:
            self._processed_ids = set()

    def _save_processed_ids(self) -> None:
        """Save processed email IDs to file."""
        processed_file = self.processed_path

        # Prune to max size if needed
        if len(self._processed_ids) > MAX_PROCESSED_IDS:
            # Keep only the most recent IDs (arbitrary since we don't track order)
            # In practice, just keep MAX_PROCESSED_IDS items
            self._processed_ids = set(list(self._processed_ids)[-MAX_PROCESSED_IDS:])

        data = {
            "processed_ids": list(self._processed_ids),
            "last_check": datetime.now().isoformat()
        }

        try:
            with open(processed_file, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save processed emails file: {e}")

    def is_processed(self, message_id: str) -> bool:
        """Check if an email has already been processed."""
        return message_id in self._processed_ids

    def mark_as_processed(self, message_id: str) -> None:
        """Mark an email as processed and save to file."""
        self._processed_ids.add(message_id)
        self._save_processed_ids()

    def _build_query(self, hours: int) -> str:
        """
        Build Gmail search query for recent emails.

        Args:
            hours: How many hours back to search.

        Returns:
            Gmail search query string.
        """
        # Calculate date threshold
        after_date = datetime.now() - timedelta(hours=hours)
        after_str = after_date.strftime("%Y/%m/%d")

        # Scope to the inbox and drop the noisy category tabs (Promotions,
        # Social, Forums) so that mail never reaches the AI classifier.
        # We use negative category filters rather than `category:primary`
        # because Workspace accounts (e.g. a university .edu) don't populate
        # the Primary category — `category:primary` matches nothing there and
        # would silently drop the entire inbox. `in:inbox` keeps this from
        # searching All Mail (archived/sent) on accounts without category tabs.
        query = f"after:{after_str} in:inbox -category:promotions -category:social -category:forums"

        logger.debug(f"[{self.label}] Gmail query: {query}")
        return query

    def _parse_email_address(self, header_value: str) -> tuple:
        """
        Parse email header to extract name and email address.

        Args:
            header_value: e.g., "John Doe <john@example.com>" or "john@example.com"

        Returns:
            Tuple of (display_name, email_address)
        """
        if "<" in header_value and ">" in header_value:
            name = header_value.split("<")[0].strip().strip('"')
            email = header_value.split("<")[1].split(">")[0].strip()
            return name, email
        else:
            return header_value.strip(), header_value.strip()

    def _get_email_body(self, payload: dict) -> tuple:
        """
        Extract email body text and HTML from message payload.

        Args:
            payload: Gmail message payload dict.

        Returns:
            Tuple of (plain_text, html_text)
        """
        plain_text = ""
        html_text = ""

        def extract_parts(part):
            nonlocal plain_text, html_text

            mime_type = part.get("mimeType", "")
            body = part.get("body", {})
            data = body.get("data", "")

            if data:
                decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                if mime_type == "text/plain":
                    plain_text += decoded
                elif mime_type == "text/html":
                    html_text += decoded

            # Recursively handle multipart messages
            for subpart in part.get("parts", []):
                extract_parts(subpart)

        extract_parts(payload)

        return plain_text, html_text

    def _message_to_email(self, msg_id: str, message: dict) -> EmailMessage:
        """Convert a raw Gmail API message dict into an EmailMessage."""
        headers = {h["name"].lower(): h["value"]
                   for h in message.get("payload", {}).get("headers", [])}

        subject = headers.get("subject", "(No Subject)")
        sender_raw = headers.get("from", "")
        date_str = headers.get("date", "")

        sender_name, sender_email = self._parse_email_address(sender_raw)

        # Parse date
        try:
            msg_date = parsedate_to_datetime(date_str)
        except (ValueError, TypeError):
            msg_date = datetime.now()

        # Get body
        plain_text, html_text = self._get_email_body(message.get("payload", {}))

        # Get Gmail category from labels
        category = "Unknown"
        for label_id in message.get("labelIds", []):
            if label_id in CATEGORY_LABELS:
                category = CATEGORY_LABELS[label_id]
                break

        return EmailMessage(
            message_id=msg_id,
            subject=subject,
            sender=sender_name or sender_email,
            sender_email=sender_email,
            date=msg_date,
            body_text=plain_text,
            body_html=html_text,
            category=category,
            account=self.account,
        )

    def _fetch_messages(self, query: str, processed_ids: set, skip_processed: bool) -> List[EmailMessage]:
        """
        Page through a Gmail search query and return the matching messages.

        Args:
            query: Gmail search query string.
            processed_ids: IDs to skip when skip_processed is True.
            skip_processed: Whether to honor processed_ids.

        Returns:
            List of EmailMessage objects.
        """
        service = self._get_service()

        emails = []
        page_token = None

        while True:
            try:
                results = service.users().messages().list(
                    userId="me",
                    q=query,
                    pageToken=page_token,
                    maxResults=50
                ).execute()

                for msg_info in results.get("messages", []):
                    msg_id = msg_info["id"]

                    # Skip already processed (unless skip_processed is False)
                    if skip_processed and msg_id in processed_ids:
                        logger.debug(f"[{self.label}] Skipping already processed: {msg_id}")
                        continue

                    # Fetch full message
                    try:
                        message = service.users().messages().get(
                            userId="me",
                            id=msg_id,
                            format="full"
                        ).execute()
                    except HttpError as e:
                        logger.warning(f"[{self.label}] Failed to fetch message {msg_id}: {e}")
                        continue

                    emails.append(self._message_to_email(msg_id, message))

                # Check for more pages
                page_token = results.get("nextPageToken")
                if not page_token:
                    break

            except HttpError as e:
                logger.error(f"[{self.label}] Gmail API error: {e}")
                break

        return emails

    def fetch_recent_emails(self, hours: Optional[int] = None, skip_processed: bool = True) -> List[EmailMessage]:
        """
        Fetch recent job-related emails.

        Args:
            hours: How many hours back to search. Defaults to config value.
            skip_processed: If True, skip emails already in processed_emails.json

        Returns:
            List of EmailMessage objects.
        """
        if hours is None:
            hours = self.config.gmail_lookback_days * 24

        query = self._build_query(hours)
        emails = self._fetch_messages(query, self._processed_ids, skip_processed)

        logger.info(f"[{self.label}] Fetched {len(emails)} new emails (after filtering processed)")
        return emails

    def _load_processed_ids_from_file(self, processed_file: Path) -> set:
        """Load processed email IDs from a specific file."""
        if processed_file.exists():
            try:
                with open(processed_file, "r") as f:
                    data = json.load(f)
                    return set(data.get("processed_ids", []))
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load {processed_file.name}: {e}")
        return set()

    def _save_processed_ids_to_file(self, processed_file: Path, ids: set) -> None:
        """Save processed email IDs to a specific file."""
        # Prune to max size if needed
        if len(ids) > MAX_PROCESSED_IDS:
            ids = set(list(ids)[-MAX_PROCESSED_IDS:])

        data = {
            "processed_ids": list(ids),
            "last_check": datetime.now().isoformat()
        }

        try:
            with open(processed_file, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save {processed_file.name}: {e}")

    def fetch_emails_by_senders(
        self,
        senders: List[str],
        hours: int,
        skip_processed: bool = True,
    ) -> List[EmailMessage]:
        """
        Fetch emails from specific senders within a time window.

        Args:
            senders: List of sender email addresses to filter by.
            hours: How many hours back to search.
            skip_processed: If True, skip emails already processed (tracked separately from status emails).

        Returns:
            List of EmailMessage objects from the specified senders.
        """
        if not senders:
            return []

        # Build query: after:{date} from:({sender1} OR {sender2})
        after_date = datetime.now() - timedelta(hours=hours)
        after_str = after_date.strftime("%Y/%m/%d")
        senders_query = " OR ".join(f"from:{s}" for s in senders)
        query = f"after:{after_str} ({senders_query})"

        logger.debug(f"[{self.label}] Newsletter Gmail query: {query}")

        # Newsletter emails are tracked separately from status emails
        newsletter_processed = self._load_processed_ids_from_file(self.newsletter_processed_path)
        emails = self._fetch_messages(query, newsletter_processed, skip_processed)

        logger.info(f"[{self.label}] Fetched {len(emails)} newsletter emails from senders: {senders}")
        return emails

    def mark_newsletter_as_processed(self, message_id: str) -> None:
        """Mark a newsletter email as processed (separate tracking from status emails)."""
        newsletter_processed = self._load_processed_ids_from_file(self.newsletter_processed_path)
        newsletter_processed.add(message_id)
        self._save_processed_ids_to_file(self.newsletter_processed_path, newsletter_processed)


# Cached client instances, one per configured account
_clients: Optional[List[GmailClient]] = None


def get_gmail_clients(config: Optional[Config] = None) -> List[GmailClient]:
    """
    Get one GmailClient per configured account.

    Falls back to a single legacy client when GMAIL_ACCOUNTS is not set.
    """
    global _clients
    if _clients is None:
        cfg = config or get_config()
        accounts = cfg.gmail_accounts
        if accounts:
            _clients = [GmailClient(cfg, account=a) for a in accounts]
        else:
            _clients = [GmailClient(cfg)]
    return _clients


def get_gmail_client() -> GmailClient:
    """Get the primary GmailClient instance (first configured account)."""
    return get_gmail_clients()[0]

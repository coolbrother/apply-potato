"""
Gmail integration for ApplyPotato.
Handles OAuth authentication and email fetching for status tracking.
"""

import base64
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass

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


class GmailClient:
    """Client for Gmail API operations."""

    def __init__(self, config: Optional[Config] = None):
        """
        Initialize Gmail client.

        Args:
            config: Optional config object. Uses global config if not provided.
        """
        self.config = config or get_config()
        self._service = None
        self._creds = None
        self._processed_ids: set = set()
        self._load_processed_ids()

    def _get_credentials(self) -> Credentials:
        """Get or refresh Google API credentials for Gmail."""
        if self._creds and self._creds.valid:
            return self._creds

        token_path = self.config.auth_dir / "gmail_token.json"

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
        """Get Gmail API service instance."""
        if self._service is None:
            creds = self._get_credentials()
            self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def _load_processed_ids(self) -> None:
        """Load processed email IDs from file."""
        processed_file = self.config.data_dir / PROCESSED_EMAILS_FILENAME
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
        processed_file = self.config.data_dir / PROCESSED_EMAILS_FILENAME

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

        # Only fetch emails from Primary inbox
        # Ignore Promotions, Social, Updates, and Forums categories
        # This reduces noise and avoids sending irrelevant emails to AI
        query = f"after:{after_str} category:primary"

        logger.debug(f"Gmail query: {query}")
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

        service = self._get_service()
        query = self._build_query(hours)

        emails = []
        page_token = None

        while True:
            try:
                # List messages matching query
                results = service.users().messages().list(
                    userId="me",
                    q=query,
                    pageToken=page_token,
                    maxResults=50
                ).execute()

                messages = results.get("messages", [])

                for msg_info in messages:
                    msg_id = msg_info["id"]

                    # Skip already processed (unless skip_processed is False)
                    if skip_processed and self.is_processed(msg_id):
                        logger.debug(f"Skipping already processed: {msg_id}")
                        continue

                    # Fetch full message
                    try:
                        message = service.users().messages().get(
                            userId="me",
                            id=msg_id,
                            format="full"
                        ).execute()
                    except HttpError as e:
                        logger.warning(f"Failed to fetch message {msg_id}: {e}")
                        continue

                    # Parse headers
                    headers = {h["name"].lower(): h["value"]
                               for h in message.get("payload", {}).get("headers", [])}

                    subject = headers.get("subject", "(No Subject)")
                    sender_raw = headers.get("from", "")
                    date_str = headers.get("date", "")

                    sender_name, sender_email = self._parse_email_address(sender_raw)

                    # Parse date
                    try:
                        # Gmail dates can have various formats, try common ones
                        from email.utils import parsedate_to_datetime
                        msg_date = parsedate_to_datetime(date_str)
                    except (ValueError, TypeError):
                        msg_date = datetime.now()

                    # Get body
                    plain_text, html_text = self._get_email_body(message.get("payload", {}))

                    # Get Gmail category from labels
                    label_ids = message.get("labelIds", [])
                    category = "Unknown"
                    for label_id in label_ids:
                        if label_id in CATEGORY_LABELS:
                            category = CATEGORY_LABELS[label_id]
                            break

                    email_msg = EmailMessage(
                        message_id=msg_id,
                        subject=subject,
                        sender=sender_name or sender_email,
                        sender_email=sender_email,
                        date=msg_date,
                        body_text=plain_text,
                        body_html=html_text,
                        category=category
                    )
                    emails.append(email_msg)

                # Check for more pages
                page_token = results.get("nextPageToken")
                if not page_token:
                    break

            except HttpError as e:
                logger.error(f"Gmail API error: {e}")
                break

        logger.info(f"Fetched {len(emails)} new emails (after filtering processed)")
        return emails


# Singleton client instance
_client: Optional[GmailClient] = None


def get_gmail_client() -> GmailClient:
    """Get the global GmailClient instance."""
    global _client
    if _client is None:
        _client = GmailClient()
    return _client

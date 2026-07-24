"""
Tests for multi-account Gmail support.

Covers per-account token/state paths, the one-time legacy migration,
get_gmail_clients(), and GmailChecker fan-out across accounts.
"""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.gmail as gmail_module
from src.gmail import (
    GmailClient,
    EmailMessage,
    account_slug,
    get_gmail_clients,
    PROCESSED_EMAILS_FILENAME,
    PROCESSED_NEWSLETTER_EMAILS_FILENAME,
    TOKEN_FILENAME,
)


class FakeConfig:
    """Minimal stand-in exposing only what GmailClient/GmailChecker read."""

    def __init__(self, base_dir, accounts=None):
        self.base_dir = base_dir
        self.gmail_accounts = accounts or []
        self.gmail_lookback_days = 1
        self.newsletter_lookback_days = 7
        self.google_credentials_path = base_dir / "auth" / "credentials.json"
        self.oauth_local_port = 0
        self.oauth_timeout_seconds = 60
        self.discord = SimpleNamespace(enabled=False)
        self.user = SimpleNamespace(target_companies=[])
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def auth_dir(self):
        return self.base_dir / "auth"

    @property
    def data_dir(self):
        return self.base_dir / "data"


@pytest.fixture
def gmail_config(tmp_path):
    """Config rooted at a temp dir so auth/ and data/ are isolated."""
    return FakeConfig(tmp_path)


def with_accounts(config, accounts):
    """Same temp dirs, different configured accounts."""
    return FakeConfig(config.base_dir, accounts)


@pytest.fixture(autouse=True)
def reset_client_cache():
    """get_gmail_clients() caches globally; keep tests independent."""
    gmail_module._clients = None
    yield
    gmail_module._clients = None


def _write(path, ids):
    path.write_text(json.dumps({"processed_ids": ids}), encoding="utf-8")


# =============================================================================
# Slug + path derivation
# =============================================================================

def test_account_slug_is_filename_safe():
    assert account_slug("John.Doe+jobs@Gmail.com") == "john_doe_jobs_gmail_com"
    assert account_slug("me@school.edu") == "me_school_edu"


def test_legacy_mode_uses_unsuffixed_filenames(gmail_config):
    client = GmailClient(gmail_config)

    assert client.account == ""
    assert client.label == "default"
    assert client.token_path.name == TOKEN_FILENAME
    assert client.processed_path.name == PROCESSED_EMAILS_FILENAME
    assert client.newsletter_processed_path.name == PROCESSED_NEWSLETTER_EMAILS_FILENAME


def test_account_mode_suffixes_filenames(gmail_config):
    config = with_accounts(gmail_config, ["a@gmail.com", "b@school.edu"])
    client = GmailClient(config, account="b@school.edu")

    assert client.label == "b@school.edu"
    assert client.token_path.name == "gmail_token_b_school_edu.json"
    assert client.processed_path.name == "processed_emails_b_school_edu.json"
    assert client.newsletter_processed_path.name == "processed_newsletter_emails_b_school_edu.json"


def test_accounts_do_not_share_state_files(gmail_config):
    config = with_accounts(gmail_config, ["a@gmail.com", "b@school.edu"])
    first = GmailClient(config, account="a@gmail.com")
    second = GmailClient(config, account="b@school.edu")

    assert first.processed_path != second.processed_path

    first.mark_as_processed("msg-1")
    second.mark_as_processed("msg-2")

    assert first.is_processed("msg-1")
    assert not first.is_processed("msg-2")
    assert GmailClient(config, account="b@school.edu").is_processed("msg-2")


# =============================================================================
# Legacy migration
# =============================================================================

def test_first_account_inherits_legacy_state(gmail_config):
    config = with_accounts(gmail_config, ["a@gmail.com", "b@school.edu"])
    (config.auth_dir / TOKEN_FILENAME).write_text("{}", encoding="utf-8")
    _write(config.data_dir / PROCESSED_EMAILS_FILENAME, ["old-1"])
    _write(config.data_dir / PROCESSED_NEWSLETTER_EMAILS_FILENAME, ["news-1"])

    client = GmailClient(config, account="a@gmail.com")

    assert client.token_path.exists()
    assert not (config.auth_dir / TOKEN_FILENAME).exists()
    assert client.is_processed("old-1")
    assert "news-1" in client._load_processed_ids_from_file(client.newsletter_processed_path)


def test_second_account_does_not_take_legacy_state(gmail_config):
    config = with_accounts(gmail_config, ["a@gmail.com", "b@school.edu"])
    legacy_token = config.auth_dir / TOKEN_FILENAME
    legacy_token.write_text("{}", encoding="utf-8")
    _write(config.data_dir / PROCESSED_EMAILS_FILENAME, ["old-1"])

    client = GmailClient(config, account="b@school.edu")

    assert legacy_token.exists()  # untouched
    assert not client.token_path.exists()
    assert not client.is_processed("old-1")


def test_migration_does_not_clobber_existing_per_account_state(gmail_config):
    config = with_accounts(gmail_config, ["a@gmail.com"])
    legacy = config.data_dir / PROCESSED_EMAILS_FILENAME
    _write(legacy, ["old-1"])
    _write(config.data_dir / "processed_emails_a_gmail_com.json", ["new-1"])

    client = GmailClient(config, account="a@gmail.com")

    assert client.is_processed("new-1")
    assert not client.is_processed("old-1")
    assert legacy.exists()  # left alone


# =============================================================================
# Inbox query shape (works for both consumer Gmail and Workspace)
# =============================================================================

def _assert_inbox_query(query):
    # Scoped to inbox, noisy category tabs excluded, and — crucially — does NOT
    # use `category:primary`, which matches nothing on Workspace accounts.
    assert "in:inbox" in query
    assert "-category:promotions" in query
    assert "-category:social" in query
    assert "-category:forums" in query
    assert "category:primary" not in query


def test_query_shape_for_gmail_account(gmail_config):
    config = with_accounts(gmail_config, ["a@gmail.com"])
    _assert_inbox_query(GmailClient(config, account="a@gmail.com")._build_query(24))


def test_query_shape_for_workspace_account(gmail_config):
    config = with_accounts(gmail_config, ["user@school.edu"])
    _assert_inbox_query(GmailClient(config, account="user@school.edu")._build_query(24))


def test_query_shape_in_legacy_mode(gmail_config):
    _assert_inbox_query(GmailClient(gmail_config)._build_query(24))


# =============================================================================
# Client factory
# =============================================================================

def test_get_gmail_clients_single_account_when_unset(gmail_config):
    with patch("src.gmail.get_config", return_value=gmail_config):
        clients = get_gmail_clients()

    assert len(clients) == 1
    assert clients[0].account == ""


def test_get_gmail_clients_one_per_account(gmail_config):
    config = with_accounts(gmail_config, ["a@gmail.com", "b@school.edu"])
    with patch("src.gmail.get_config", return_value=config):
        clients = get_gmail_clients()

    assert [c.account for c in clients] == ["a@gmail.com", "b@school.edu"]
    assert gmail_module.get_gmail_client().account == "a@gmail.com"


# =============================================================================
# Identity guard
# =============================================================================

def _service_returning(email):
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": email}
    return service


def test_verify_account_rejects_wrong_mailbox(gmail_config):
    config = with_accounts(gmail_config, ["a@gmail.com"])
    client = GmailClient(config, account="a@gmail.com")

    with pytest.raises(RuntimeError, match="authorized as"):
        client._verify_account(_service_returning("someone-else@gmail.com"))


def test_verify_account_accepts_matching_mailbox(gmail_config):
    config = with_accounts(gmail_config, ["a@gmail.com"])
    client = GmailClient(config, account="a@gmail.com")

    client._verify_account(_service_returning("A@Gmail.com"))  # case-insensitive
    assert client._verified


def test_verify_account_ignores_gmail_dots_and_plus(gmail_config):
    # Gmail treats dots/+tags as insignificant: same mailbox, must not raise.
    config = with_accounts(gmail_config, ["johndoe@gmail.com"])
    client = GmailClient(config, account="johndoe@gmail.com")

    client._verify_account(_service_returning("john.doe@gmail.com"))
    assert client._verified


def test_verify_account_rejects_different_domain(gmail_config):
    # user@gmail.com vs user@school.edu are genuinely different accounts.
    config = with_accounts(gmail_config, ["user@gmail.com"])
    client = GmailClient(config, account="user@gmail.com")

    with pytest.raises(RuntimeError, match="authorized as"):
        client._verify_account(_service_returning("user@school.edu"))


def test_verify_account_noop_in_legacy_mode(gmail_config):
    client = GmailClient(gmail_config)
    client._verify_account(_service_returning("anything@gmail.com"))  # must not raise


# =============================================================================
# GmailChecker fan-out
# =============================================================================

def _email(msg_id, account):
    return EmailMessage(
        message_id=msg_id,
        subject=f"Subject {msg_id}",
        sender="Recruiter",
        sender_email="recruiter@example.com",
        date=datetime.now(),
        body_text="body",
        body_html="",
        category="Primary",
        account=account,
    )


def test_checker_processes_every_account_and_marks_on_owner(gmail_config):
    client_a = MagicMock(label="a@gmail.com")
    client_a.fetch_recent_emails.return_value = [_email("a-1", "a@gmail.com")]
    client_b = MagicMock(label="b@school.edu")
    client_b.fetch_recent_emails.return_value = [_email("b-1", "b@school.edu")]

    with patch("check_gmail.get_gmail_clients", return_value=[client_a, client_b]), \
         patch("check_gmail.get_classifier"), \
         patch("check_gmail.get_sheets_client"), \
         patch("check_gmail.apply_privacy_filters", return_value=(False, "filtered")):
        from check_gmail import GmailChecker

        checker = GmailChecker(gmail_config)
        stats = checker.run()

    assert stats["accounts_checked"] == 2
    assert stats["emails_fetched"] == 2
    client_a.mark_as_processed.assert_called_once_with("a-1")
    client_b.mark_as_processed.assert_called_once_with("b-1")


def test_checker_continues_when_one_account_fails(gmail_config):
    broken = MagicMock(label="broken@gmail.com")
    broken.fetch_recent_emails.side_effect = RuntimeError("token revoked")
    working = MagicMock(label="ok@gmail.com")
    working.fetch_recent_emails.return_value = [_email("ok-1", "ok@gmail.com")]

    with patch("check_gmail.get_gmail_clients", return_value=[broken, working]), \
         patch("check_gmail.get_classifier"), \
         patch("check_gmail.get_sheets_client"), \
         patch("check_gmail.apply_privacy_filters", return_value=(False, "filtered")):
        from check_gmail import GmailChecker

        checker = GmailChecker(gmail_config)
        stats = checker.run()

    assert stats["accounts_checked"] == 1
    assert stats["emails_fetched"] == 1
    working.mark_as_processed.assert_called_once_with("ok-1")


# =============================================================================
# Newsletter fan-out
# =============================================================================

def test_newsletter_parser_fetches_from_all_accounts(gmail_config):
    from src.config import NewsletterSource
    from src.newsletter_parser import NewsletterParser

    client_a = MagicMock(label="a@gmail.com")
    client_a.fetch_emails_by_senders.return_value = [_email("a-1", "a@gmail.com")]
    client_b = MagicMock(label="b@school.edu")
    client_b.fetch_emails_by_senders.return_value = [_email("b-1", "b@school.edu")]

    parser = NewsletterParser(gmail_config, gmail_clients=[client_a, client_b])
    source = NewsletterSource(name="simplify", sender_emails=["noreply@simplify.jobs"])

    fetched = parser.fetch_newsletter_emails(source)

    assert [(c.label, e.message_id) for c, e in fetched] == [
        ("a@gmail.com", "a-1"),
        ("b@school.edu", "b-1"),
    ]

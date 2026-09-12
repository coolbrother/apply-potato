"""
Once-a-day look at the Promotions tab.

The Gmail checker's query deliberately excludes Promotions, Social and Forums on every
account, so the AI classifier never sees that traffic. Gmail is mostly right about what
belongs there, but not always: a Google request for transcripts was filed under
Promotions, and only a manual move to Primary got it processed.

This scan reads the tab once a day and sorts each message into one of three buckets:

  ignore   general marketing - a talent-team newsletter, a job-match digest, a discount.
  matched  about an application the user made, and the tracker has the row for it.
  review   about an application, or possibly so, but no single row could be tied to
           it - or the AI could not decide. The user asked for exactly this: when in
           doubt, send the subject and date and let them look.

It never writes to the sheet. Moving a reported email to Primary is the user's action,
and the checker then handles the row on its next pass, with the staleness guard and the
tie rules it already has. The main fetch stays as narrow as it was.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .email_classifier import EmailClassification, EmailClassifier
from .email_filters import apply_privacy_filters
from .gmail import EmailMessage, GmailClient, account_slug, body_as_text

logger = logging.getLogger(__name__)

PROMPT_FILENAME = "promotion_triage.txt"
SEEN_FILENAME = "promotions_seen.json"
SEEN_TTL_DAYS = 30
DISCORD_LIMIT = 2000

VERDICT_APPLICATION = "application"
VERDICT_UNSURE = "unsure"
VERDICT_MARKETING = "marketing"
VERDICTS = (VERDICT_APPLICATION, VERDICT_UNSURE, VERDICT_MARKETING)

BUCKET_MATCHED = "matched"
BUCKET_REVIEW = "review"

# (classification, email) -> MatchOutcome. In production this is
# GmailChecker._find_matching_job, so requisition ids, subsidiaries, ties and struck
# rows behave exactly as they do for Primary mail.
Matcher = Callable[[EmailClassification, EmailMessage], object]


@dataclass
class Triage:
    """What the AI made of one Promotions email."""
    verdict: str
    confidence: float = 0.0
    company_candidates: List[str] = field(default_factory=list)
    position: Optional[str] = None
    action_required: Optional[str] = None
    summary: Optional[str] = None


@dataclass
class Finding:
    """One email worth telling the user about, and why."""
    email: Optional[EmailMessage]
    bucket: str
    triage: Optional[Triage] = None
    job: Optional[object] = None                    # JobRow when a row was tied to it
    candidates: list = field(default_factory=list)  # tied rows, when the match was ambiguous
    reason: str = ""                                # why it is in review


# --- Triage ------------------------------------------------------------------------


def load_prompt(prompts_dir: Path) -> str:
    path = prompts_dir / PROMPT_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def build_prompt(template: str, email: EmailMessage, body: str) -> str:
    return (
        template
        .replace("{subject}", email.subject or "")
        .replace("{sender}", f"{email.sender} <{email.sender_email}>")
        .replace("{date}", email.date.strftime("%Y-%m-%d %H:%M"))
        .replace("{body}", body)
    )


def parse_triage(raw: str) -> Optional[Triage]:
    """
    Read the AI's JSON. A verdict outside the three known values becomes "unsure":
    an answer that cannot be read is exactly the case the user wants to see.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                pass
    if not isinstance(data, dict):
        logger.error(f"Triage response is not JSON. First 200 chars: {text[:200]}")
        return None

    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        logger.warning(f"Unknown triage verdict {verdict!r}, treating as unsure")
        verdict = VERDICT_UNSURE

    candidates = data.get("company_candidates") or []
    if isinstance(candidates, str):
        candidates = [candidates]
    candidates = [str(c).strip() for c in candidates if str(c or "").strip()]

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    def text_or_none(key: str) -> Optional[str]:
        value = data.get(key)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    return Triage(
        verdict=verdict,
        confidence=confidence,
        company_candidates=candidates,
        position=text_or_none("position"),
        action_required=text_or_none("action_required"),
        summary=text_or_none("summary"),
    )


def triage_email(classifier: EmailClassifier, template: str, email: EmailMessage) -> Optional[Triage]:
    """Ask the configured provider for a verdict; None when it gave nothing usable."""
    body = body_as_text(email)
    if not body.strip():
        logger.warning(f"Promotions email has no body: {email.subject}")
        return None
    raw = classifier.complete(build_prompt(template, email, body))
    if not raw:
        return None
    return parse_triage(raw)


def to_classification(triage: Triage) -> EmailClassification:
    """The shape the row matcher takes. Category is not a stage, so "unknown"."""
    return EmailClassification(
        category="unknown",
        confidence=triage.confidence,
        company_candidates=list(triage.company_candidates),
        position=triage.position,
        action_required=triage.action_required,
        key_details=triage.summary,
    )


# --- Decision ----------------------------------------------------------------------


def decide(triage: Optional[Triage], outcome) -> Optional[Finding]:
    """
    Which bucket, if any. Returns a Finding without its email set, or None to ignore.

    Marketing is dropped. An "application" verdict with exactly one row is matched.
    Everything else the user sees: an application verdict the tracker cannot place,
    an unsure verdict (even one the tracker could place - the AI's doubt is the
    signal), and a triage that failed outright.
    """
    if triage is None:
        return Finding(email=None, bucket=BUCKET_REVIEW, reason="could not be triaged")

    if triage.verdict == VERDICT_MARKETING:
        return None

    job = getattr(outcome, "job", None) if outcome is not None else None
    candidates = list(getattr(outcome, "candidates", []) or []) if outcome is not None else []

    if triage.verdict == VERDICT_APPLICATION:
        if job is not None:
            return Finding(email=None, bucket=BUCKET_MATCHED, triage=triage, job=job)
        if outcome is None:
            reason = "no company named"
        else:
            reason = getattr(outcome, "reason", "") or "no sheet row matched"
        return Finding(email=None, bucket=BUCKET_REVIEW, triage=triage,
                       candidates=candidates, reason=reason)

    # unsure
    return Finding(email=None, bucket=BUCKET_REVIEW, triage=triage, job=job,
                   candidates=candidates, reason="AI unsure whether this is about an application")


# --- Seen ids ----------------------------------------------------------------------


class SeenStore:
    """
    Message ids already reported for one account, so the overlapping daily windows
    cannot report an email twice. Kept 30 days, which outlives any window by far.
    """

    def __init__(self, data_dir: Path, account: str, now: Optional[datetime] = None):
        slug = account_slug(account) if account else ""
        stem, ext = SEEN_FILENAME.rsplit(".", 1)
        self.path = data_dir / (f"{stem}_{slug}.{ext}" if slug else SEEN_FILENAME)
        self._now = now or datetime.now()
        self._ids: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read {self.path.name}, starting empty: {e}")
            return
        if isinstance(data, dict):
            self._ids = {str(k): str(v) for k, v in data.items()}
        self.prune()

    def prune(self) -> None:
        cutoff = self._now - timedelta(days=SEEN_TTL_DAYS)
        kept = {}
        for msg_id, stamp in self._ids.items():
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if when.tzinfo is not None:
                when = when.astimezone().replace(tzinfo=None)
            if when >= cutoff:
                kept[msg_id] = stamp
        self._ids = kept

    def __contains__(self, msg_id: str) -> bool:
        return msg_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, msg_id: str, when: Optional[datetime] = None) -> None:
        self._ids[msg_id] = (when or self._now).isoformat(timespec="seconds")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._ids, indent=2), encoding="utf-8")


# --- Scan --------------------------------------------------------------------------


def _local_naive(when: datetime) -> datetime:
    """Gmail dates arrive tz-aware; the cutoff is local wall-clock. Compare like with like."""
    if when.tzinfo is not None:
        return when.astimezone().replace(tzinfo=None)
    return when


def scan_account(
    client: GmailClient,
    classifier: EmailClassifier,
    template: str,
    matcher: Matcher,
    since: datetime,
    store: SeenStore,
    dry_run: bool = False,
) -> List[Finding]:
    """
    Every Promotions email on one account newer than `since`, sorted and decided.

    An email is recorded as seen only once a decision was reached, so a provider
    outage mid-run leaves the rest to be picked up tomorrow rather than lost.
    A dry run records nothing.
    """
    # Gmail's after: is a calendar date; the hour is applied here.
    query = (
        f"in:inbox category:promotions "
        f"after:{(since - timedelta(days=1)).strftime('%Y/%m/%d')}"
    )
    emails = client._fetch_messages(query, set(), skip_processed=False)
    emails = [e for e in emails if _local_naive(e.date) >= since]
    emails.sort(key=lambda e: e.date)
    logger.info(f"[{client.label}] {len(emails)} Promotions email(s) since {since:%Y-%m-%d %H:%M}")

    findings: List[Finding] = []
    for email in emails:
        if email.message_id in store:
            logger.debug(f"[{client.label}] already reported: {email.subject}")
            continue

        safe, why = apply_privacy_filters(email)
        if not safe:
            logger.info(f"[{client.label}] skipped ({why}): {email.subject}")
            if not dry_run:
                store.add(email.message_id, _local_naive(email.date))
            continue

        triage = triage_email(classifier, template, email)
        outcome = None
        if triage is not None and triage.company_candidates:
            outcome = matcher(to_classification(triage), email)

        finding = decide(triage, outcome)
        verdict = triage.verdict if triage else "none"
        if finding is None:
            logger.info(f"[{client.label}] ignored ({verdict}): {email.subject}")
        else:
            finding.email = email
            findings.append(finding)
            logger.info(f"[{client.label}] {finding.bucket} ({verdict}): {email.subject}")

        if not dry_run:
            store.add(email.message_id, _local_naive(email.date))

    if not dry_run:
        store.save()
    return findings


# --- Message -----------------------------------------------------------------------


def _when(email: EmailMessage) -> str:
    return _local_naive(email.date).strftime("%m-%d %H:%M")


def _render_matched(f: Finding) -> str:
    job = f.job
    head = f"• **{job.company}** — {job.position} (row {job.row_number}, {job.status})"
    lines = [head, f'  "{f.email.subject}" · {_when(f.email)} · {f.email.account or "default"}']
    ask = (f.triage.action_required if f.triage else None) or (f.triage.summary if f.triage else None)
    if ask:
        lines.append(f"  ↳ {ask}")
    return "\n".join(lines)


def _render_review(f: Finding) -> str:
    lines = [f'• "{f.email.subject}" · {_when(f.email)} · {f.email.account or "default"}']
    bits = []
    if f.triage and f.triage.company_candidates:
        bits.append(f.triage.company_candidates[0])
    if f.job is not None:
        bits.append(f"row {f.job.row_number} ({f.job.status})")
    elif f.candidates:
        rows = ", ".join(str(getattr(c, "row_number", "?")) for c in f.candidates[:4])
        bits.append(f"rows {rows}")
    if f.reason:
        bits.append(f.reason)
    if bits:
        lines.append("  ↳ " + " · ".join(bits))
    return "\n".join(lines)


def build_message(findings: List[Finding], now: datetime) -> str:
    """
    The Discord digest, or "" when there is nothing to say. Trimmed from the end of the
    review list, then the matched list, to stay under Discord's hard 2000-character cap.
    """
    matched = [f for f in findings if f.bucket == BUCKET_MATCHED]
    review = [f for f in findings if f.bucket == BUCKET_REVIEW]
    if not matched and not review:
        return ""

    header = (
        f"───────────────\n\n📬 **Promotions tab check — {now:%Y-%m-%d %H:%M}**\n"
        "These landed in Promotions, where the status checker does not look. "
        "Move the real ones to Primary and it will pick them up.\n"
    )

    def render(n_matched: int, n_review: int) -> str:
        parts = [header]
        if matched:
            parts.append("\n**About one of your applications**")
            parts.extend(_render_matched(f) for f in matched[:n_matched])
            if n_matched < len(matched):
                parts.append(f"• …and {len(matched) - n_matched} more")
        if review:
            parts.append("\n**Please review**")
            parts.extend(_render_review(f) for f in review[:n_review])
            if n_review < len(review):
                parts.append(f"• …and {len(review) - n_review} more")
        return "\n".join(parts)

    n_matched, n_review = len(matched), len(review)
    msg = render(n_matched, n_review)
    while len(msg) > DISCORD_LIMIT and (n_matched or n_review):
        if n_review:
            n_review -= 1
        else:
            n_matched -= 1
        msg = render(n_matched, n_review)
    return msg

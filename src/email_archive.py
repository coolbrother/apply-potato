"""
Archive status emails into the per-job folder.

A row records that a stage happened and when, but the email itself — the assessment
link, the deadline, the interviewer names — lives only in Gmail. For OA and later
stages the message is copied into the same folder job_desc.py writes to, so the job's
paperwork sits in one place.
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .gmail import EmailMessage
from .job_desc import _sanitize

logger = logging.getLogger(__name__)


# Stages worth keeping the correspondence for. "confirmation" is excluded: it is an
# auto-reply with nothing in it worth reading later.
ARCHIVED_CATEGORIES = ("oa", "phone", "technical", "offer", "rejection")

_TAG = re.compile(r"<[^>]+>")
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLANK_RUN = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    """
    Crude tag strip for emails that ship no text/plain part.

    Not a parser and not trying to be: the point is to preserve the words when the
    alternative is an empty file. The Criteria assessment invite arrives this way.
    """
    if not html:
        return ""
    text = _SCRIPT_OR_STYLE.sub(" ", html)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.IGNORECASE)
    text = _TAG.sub("", text)
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
        .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    )
    return _tidy("\n".join(line.strip() for line in text.splitlines()))


def _tidy(text: str) -> str:
    """
    Trim trailing whitespace and collapse blank runs.

    Marketing-templated mail pads its text/plain part heavily — the Codility invite
    carries thirty consecutive blank lines — which buries the content when the file is
    read later. Only whitespace is touched; no word is altered, so the copy stays
    faithful.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def _body_of(email: EmailMessage) -> str:
    body = (email.body_text or "").strip()
    if body:
        return _tidy(body)
    return _html_to_text(email.body_html or "")


def _unique_path(folder: Path, stem: str, category: str, when: datetime) -> Path:
    """
    `308_Chicago_Trading_Company_oa.08.03.2026.md`, suffixed if that name is taken.

    Carries the same `{row}_{company}` prefix as the job-description files already in
    the folder, so everything for a job sorts together and a file still identifies
    itself once it is out of its directory.

    Two assessment invites on one day is the multi-round case this exists for, so a
    same-day repeat must not overwrite the first.
    """
    base = f"{stem}_{category}.{when.strftime('%m.%d.%Y')}"
    path = folder / f"{base}.md"
    counter = 2
    while path.exists():
        path = folder / f"{base}-{counter}.md"
        counter += 1
    return path


def _render(row_num: int, company: str, category: str, email: EmailMessage) -> str:
    body = _body_of(email) or "_(no readable body)_"
    return "\n".join([
        f"# {category.upper()} — {company}",
        "",
        f"- **Stage:** {category}",
        f"- **Row:** {row_num}",
        f"- **Received:** {email.date.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **From:** {email.sender_email}",
        f"- **Subject:** {email.subject}",
        f"- **Account:** {email.account or '(default)'}",
        f"- **Message ID:** {email.message_id}",
        "",
        "---",
        "",
        body,
        "",
    ])


def save_status_email(
    row_num: int,
    company: str,
    category: str,
    email: EmailMessage,
    base_dir: Path,
) -> Optional[Path]:
    """
    Write a status email into the job's folder as markdown.

    Returns the path written, or None when the category is not archived or the write
    failed. Creating the folder is expected: it only exists when a job description was
    saved for that row, which most rows reaching OA will not have.

    Args:
        row_num: 1-indexed sheet row, used for the folder name and the header.
        company: Company as recorded on the row; sanitized for the folder name.
        category: Email category from the classifier.
        email: The message to archive.
        base_dir: JOB_DESC_OUTPUT_DIR — the jobs/ folder in the resume repo.
    """
    if category not in ARCHIVED_CATEGORIES:
        return None

    stem = f"{row_num}_{_sanitize(company)}"
    folder = base_dir / stem

    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = _unique_path(folder, stem, category, email.date)
        path.write_text(_render(row_num, company, category, email), encoding="utf-8")
        logger.info(f"  Status email archived: {path}")
        return path
    except Exception as e:
        logger.warning(f"  Could not archive {category} email for row {row_num}: {e}")
        return None

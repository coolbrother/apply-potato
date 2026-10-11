"""
Shadow log for Jev.

The LLM classifier decides; Jev runs beside it and this file is the only output. An
email is recorded when the comparison has something to say:

- the two labels differ;
- Jev is unsure (confidence below JEV_LOG_CONFIDENCE), whatever the labels;
- the LLM said "unknown" but Jev put real weight on a status label. "unknown" writes
  nothing anywhere, so a status email filed there is lost without a trace — the Meta
  application receipt was. This is the case the trial most needs to measure.

Agreements are not recorded, for the reason eligibility_log.py gives: the file exists to
be read by a person, and a log where the interesting rows are 1% of the lines does not
get read. The run stats count agreements, so the rate is still known.

Entries are deduped on message ID so --reprocess does not multiply them.

Two things read this file back to the user (review mode). The daily summary lists the
disagreements and possible misses in its window; low confidence alone is left out, as it
mostly marks mail both models agree is noise. And check_gmail.py pings Discord at once
for the sharpest case — the LLM said unknown, Jev chose a status and is sure of it —
which over the backtest month was six emails, two of them real missed receipts.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SHADOW_FILENAME = "jev_shadow.json"

# Weight on status labels, under an LLM "unknown", that counts as a possible silent miss.
SILENT_MISS_THRESHOLD = 0.2

REASON_DISAGREE = "disagree"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_POSSIBLE_MISS = "possible_miss"

# The reasons the daily summary surfaces. Low confidence alone is not among them.
SUMMARY_REASONS = (REASON_DISAGREE, REASON_POSSIBLE_MISS)

# Subjects are cut to this in summary lines and alerts, matching the needs-review lines.
MAX_SUBJECT = 55


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / SHADOW_FILENAME


def load_entries(data_dir: Path) -> List[Dict[str, Any]]:
    """Every recorded entry. Empty list when the file is missing or corrupt."""
    path = _path(data_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read {path.name}: {e}")
        return []
    return data if isinstance(data, list) else []


def reasons_to_record(llm_category: str, jev, confidence_threshold: float) -> List[str]:
    """
    Why this comparison is worth a line, or an empty list when it is not.

    Args:
        llm_category: The LLM classifier's category (authoritative).
        jev: The JevResult for the same email.
        confidence_threshold: config.jev_log_confidence.
    """
    reasons = []
    if jev.label != llm_category:
        reasons.append(REASON_DISAGREE)
    if jev.confidence < confidence_threshold:
        reasons.append(REASON_LOW_CONFIDENCE)
    if llm_category == "unknown" and jev.status_probability() >= SILENT_MISS_THRESHOLD:
        reasons.append(REASON_POSSIBLE_MISS)
    return reasons


def record(
    data_dir: Path,
    *,
    email,
    llm_category: str,
    llm_confidence: Optional[float],
    jev,
    reasons: List[str],
    rows: Optional[List[int]] = None,
) -> bool:
    """
    Record one comparison. Returns True if an entry was written.

    Never raises: shadow mode is observation, and a logging failure must not take down a
    run that is otherwise fine.

    Args:
        rows: Sheet rows the email's company points to — one when it matched, several
            when tied, none when nothing matched. Shown so the user can find the job.
    """
    try:
        path = _path(data_dir)
        entries = load_entries(data_dir)
        if any(e.get("message_id") == email.message_id for e in entries):
            return False

        entries.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "message_id": email.message_id,
            "account": email.account,
            "date": email.date.strftime("%Y-%m-%d %H:%M"),
            "sender": email.sender_email,
            "subject": email.subject,
            "rows": list(rows or []),
            "reasons": reasons,
            # Authoritative — this is what the pipeline acted on.
            "llm": {"category": llm_category, "confidence": llm_confidence},
            "jev": {
                "category": jev.label,
                "confidence": round(jev.confidence, 4),
                "probabilities": {k: round(v, 4) for k, v in sorted(
                    jev.probabilities.items(), key=lambda kv: -kv[1])},
                "model": jev.model,
            },
        })

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not record Jev shadow entry: {e}")
        return False


def should_alert(llm_category: str, jev, threshold: float) -> bool:
    """
    Whether this comparison warrants an immediate Discord alert: the LLM filed the email
    as unknown — so nothing was written anywhere — while Jev puts it in a status category
    and is at least `threshold` sure. The Meta and TransMarket receipts both had this
    shape. A threshold above 1 never fires.
    """
    return (llm_category == "unknown"
            and jev.label != "unknown"
            and jev.confidence >= threshold)


def _short(subject: str) -> str:
    subject = (subject or "").strip() or "(no subject)"
    return subject if len(subject) <= MAX_SUBJECT else subject[:MAX_SUBJECT - 1] + "…"


def rows_label(rows: Optional[List[int]]) -> str:
    """'row 1842', 'rows 1025/1842', or 'no row' — the user's way into the sheet."""
    rows = list(rows or [])
    if not rows:
        return "no row"
    if len(rows) == 1:
        return f"row {rows[0]}"
    return "rows " + "/".join(str(r) for r in rows)


def alert_message(email, llm_category: str, jev, rows: Optional[List[int]] = None) -> str:
    """The Discord text for an immediate alert."""
    return (
        "───────────────\n\n"
        "⚠️ **Possible missed status email**\n"
        f"Jev: **{jev.label}** ({jev.confidence:.2f}) · LLM: {llm_category}\n"
        f"{rows_label(rows)} · {_short(email.subject)} · {email.date.strftime('%m/%d %H:%M')}\n"
        "_Nothing was written to the sheet. Check the row if this is one of yours._"
    )


def summary_entries(entries: List[Dict[str, Any]], start: datetime,
                    end: datetime) -> List[Dict[str, Any]]:
    """
    Entries recorded within [start, end] that the daily summary should name, oldest
    first. Windowed on when the entry was recorded, so each one appears in exactly one
    summary however old the email itself was.
    """
    picked = []
    for e in entries:
        if not any(r in SUMMARY_REASONS for r in e.get("reasons", [])):
            continue
        try:
            recorded = datetime.fromisoformat(e.get("timestamp", ""))
        except ValueError:
            continue
        if start <= recorded <= end:
            picked.append(e)
    return sorted(picked, key=lambda e: e.get("timestamp", ""))


def summary_line(entry: Dict[str, Any]) -> str:
    """One summary line: what each model said, the row, then the subject."""
    llm = entry.get("llm", {}).get("category", "?")
    jev = entry.get("jev", {})
    verdict = f"{llm} → {jev.get('category', '?')} {float(jev.get('confidence', 0)):.2f}"
    # A possible miss where Jev also chose unknown reads "unknown → unknown" — say which
    # status pulled it, since that is the whole reason the line is there.
    if jev.get("category") == "unknown":
        statuses = [(k, v) for k, v in (jev.get("probabilities") or {}).items() if k != "unknown"]
        if statuses:
            label, p = max(statuses, key=lambda kv: kv[1])
            verdict += f", leans {label} {float(p):.2f}"
    return f"{verdict} · {rows_label(entry.get('rows'))} · {_short(entry.get('subject', ''))}"

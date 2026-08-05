"""
Disagreement log for shadow mode.

In shadow mode both eligibility paths run and `filters.py` still decides, so nothing
in the sheet changes. This file is the only output: every posting the two paths
disagree about, with both verdicts and the sentence the AI quoted.

That is the point of shadow mode. Switching straight from code to AI would trade a
known failure mode for an unmeasured one; this turns the question into data. Four
false rejections in one day (PDT x2, Millennium x2) were each found by hand — this
finds the next ones without anyone looking.

Agreements are not recorded. The file exists to be read by a person, and a log where
the interesting rows are 1% of the lines does not get read.

Entries are deduped on the normalized URL so re-runs do not multiply them.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .deduplication import normalize_url

logger = logging.getLogger(__name__)

DISAGREEMENTS_FILENAME = "eligibility_disagreements.json"


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / DISAGREEMENTS_FILENAME


def load_disagreements(data_dir: Path) -> List[Dict[str, Any]]:
    """Every recorded disagreement. Empty list when the file is missing or corrupt."""
    path = _path(data_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read {path.name}: {e}")
        return []
    return data if isinstance(data, list) else []


def record_disagreement(
    data_dir: Path,
    *,
    url: str,
    company: str,
    title: str,
    code_passed: bool,
    code_reason: str,
    code_category: str,
    judgment,
) -> bool:
    """
    Record one posting where the two paths disagreed.

    Returns True if an entry was written. Never raises: shadow mode is observation,
    and a logging failure must not take down a pipeline run that is otherwise fine.

    Args:
        data_dir: config.data_dir.
        url: Job URL — deduped on its normalized form.
        company/title: For reading the log without opening every link.
        code_passed/code_reason/code_category: What filters.py decided (authoritative).
        judgment: The EligibilityJudgment that disagreed.
    """
    try:
        path = _path(data_dir)
        entries = load_disagreements(data_dir)

        key = normalize_url(url) if url else ""
        if key and any(e.get("url_key") == key for e in entries):
            return False

        failing = judgment.failing_check if judgment else None
        entries.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "url": url,
            "url_key": key,
            "company": company,
            "title": title,
            # Authoritative in shadow mode — this is what actually happened.
            "code": {
                "passed": code_passed,
                "reason": code_reason,
                "category": code_category,
            },
            # What the AI would have done had the mode been "ai".
            #
            # Every check is kept, not just a failing one. The most interesting
            # disagreement is "code rejected, AI would have kept it" — and there the
            # AI has no failing check at all, so summarising only the failure would
            # drop the evidence for the direction most worth reading. `evidence` is
            # the whole value of the record: it makes a disagreement checkable
            # against the posting without re-scraping it.
            "ai": {
                "eligible": bool(judgment.eligible) if judgment else None,
                "reason": judgment.reason() if judgment else None,
                "usable": bool(judgment.usable) if judgment else None,
                "discarded_reason": judgment.discarded_reason if judgment else None,
                "failing_dimension": failing.dimension if failing else None,
                "evidence": (
                    failing.evidence if failing
                    else next((c.evidence for c in (judgment.checks if judgment else [])
                               if c.evidence), None)
                ),
                "evidence_verified": failing.evidence_verified if failing else None,
                "reasoning": failing.reasoning if failing else None,
                "checks": [
                    {
                        "dimension": c.dimension,
                        "passes": c.passes,
                        "evidence": c.evidence,
                        "reasoning": c.reasoning,
                        "evidence_verified": c.evidence_verified,
                    }
                    for c in (judgment.checks if judgment else [])
                ],
            },
        })

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not record eligibility disagreement: {e}")
        return False


def summarize(data_dir: Path) -> Dict[str, int]:
    """Counts for the digest: how often, and in which direction."""
    entries = load_disagreements(data_dir)
    ai_would_keep = sum(
        1 for e in entries
        if not e.get("code", {}).get("passed") and e.get("ai", {}).get("eligible")
    )
    ai_would_reject = sum(
        1 for e in entries
        if e.get("code", {}).get("passed") and e.get("ai", {}).get("eligible") is False
    )
    return {
        "total": len(entries),
        "ai_would_keep": ai_would_keep,      # code rejected, AI would have kept
        "ai_would_reject": ai_would_reject,  # code kept, AI would have rejected
    }

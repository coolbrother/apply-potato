"""
The daily Promotions-tab scan.

The Gmail checker never reads Promotions; this scan does, once a day, and reports mail
about the user's applications. Three buckets: marketing is ignored, an application
email the tracker can place is reported with its row, and anything else the AI said
"application" or "unsure" about - or could not read - is listed with subject and date
for the user. That last rule came from the user: when in doubt, show it.

No network, no AI: the classifier, Gmail client and row matcher are fakes.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.gmail import EmailMessage
from src.promotions_scan import (
    BUCKET_MATCHED, BUCKET_REVIEW, DISCORD_LIMIT, SEEN_TTL_DAYS,
    Finding, SeenStore, Triage,
    build_message, decide, parse_triage, scan_account, to_classification,
)


NOW = datetime(2026, 9, 12, 17, 0)
SINCE = NOW - timedelta(hours=25)


def _email(subject, body="Hello, some body text.", hours_ago=2, account="me@example.com", msg_id=None, sender="Sender"):
    when = (NOW - timedelta(hours=hours_ago)).astimezone(timezone.utc)
    return EmailMessage(
        message_id=msg_id or f"id-{abs(hash(subject)) % 10_000}",
        subject=subject, sender=sender, sender_email="noreply@example.com",
        date=when, body_text=body, body_html="", category="Promotions", account=account,
    )


def _row(row_number=100, company="Acme", position="SWE Intern", status="Applied"):
    return SimpleNamespace(row_number=row_number, company=company, position=position, status=status)


def _outcome(job=None, reason="", candidates=()):
    return SimpleNamespace(job=job, reason=reason, candidates=list(candidates), matched=job is not None)


class _Classifier:
    """Answers by subject substring; anything unlisted is marketing."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        for key, answer in self.answers.items():
            if key in prompt:
                return answer if isinstance(answer, str) else json.dumps(answer)
        return json.dumps({"verdict": "marketing", "confidence": 0.9, "company_candidates": []})


class _Client:
    def __init__(self, emails, account="me@example.com"):
        self.emails = emails
        self.account = account
        self.label = account
        self.queries = []

    def _fetch_messages(self, query, processed_ids, skip_processed):
        self.queries.append(query)
        return list(self.emails)


# --- parse_triage ------------------------------------------------------------------


def test_parse_triage_reads_fenced_json():
    raw = '```json\n{"verdict": "application", "confidence": 0.8, "company_candidates": ["Acme"], "position": "SWE Intern", "action_required": "Send transcript", "summary": "Transcript request"}\n```'
    t = parse_triage(raw)
    assert t.verdict == "application"
    assert t.company_candidates == ["Acme"]
    assert t.action_required == "Send transcript"
    assert t.confidence == 0.8


def test_parse_triage_unknown_verdict_becomes_unsure():
    t = parse_triage('{"verdict": "maybe", "company_candidates": "Globex"}')
    assert t.verdict == "unsure"
    assert t.company_candidates == ["Globex"]


def test_parse_triage_garbage_is_none():
    assert parse_triage("I cannot help with that.") is None
    assert parse_triage("") is None


def test_to_classification_is_not_a_stage():
    c = to_classification(Triage(verdict="application", company_candidates=["Acme", "Acme Holdings"], position="SWE"))
    assert c.category == "unknown"
    assert c.company_candidates == ["Acme", "Acme Holdings"]
    assert c.position == "SWE"


# --- decide ------------------------------------------------------------------------


def test_marketing_is_ignored_even_with_a_row():
    assert decide(Triage(verdict="marketing", company_candidates=["Globex"]), _outcome(job=_row(company="Globex"))) is None


def test_application_with_one_row_is_matched():
    f = decide(Triage(verdict="application", company_candidates=["Acme"]), _outcome(job=_row()))
    assert f.bucket == BUCKET_MATCHED
    assert f.job.row_number == 100


def test_application_with_no_row_goes_to_review():
    f = decide(Triage(verdict="application", company_candidates=["Acme"]), _outcome(reason="no matching job for Acme"))
    assert f.bucket == BUCKET_REVIEW
    assert f.reason == "no matching job for Acme"


def test_application_with_a_tie_goes_to_review_with_candidates():
    tied = [_row(1, "Vandelay", "SWE, Desk A"), _row(2, "Vandelay", "SWE, Desk B")]
    f = decide(Triage(verdict="application", company_candidates=["Vandelay"]), _outcome(reason="ambiguous", candidates=tied))
    assert f.bucket == BUCKET_REVIEW
    assert [c.row_number for c in f.candidates] == [1, 2]


def test_application_naming_no_company_goes_to_review():
    f = decide(Triage(verdict="application", company_candidates=[]), None)
    assert f.bucket == BUCKET_REVIEW
    assert f.reason == "no company named"


def test_unsure_goes_to_review_even_when_a_row_matched():
    """The AI's doubt is the signal; the row is shown for context, not used to decide."""
    f = decide(Triage(verdict="unsure", company_candidates=["Acme"]), _outcome(job=_row()))
    assert f.bucket == BUCKET_REVIEW
    assert f.job.row_number == 100


def test_failed_triage_goes_to_review():
    f = decide(None, None)
    assert f.bucket == BUCKET_REVIEW
    assert "triaged" in f.reason


# --- SeenStore ---------------------------------------------------------------------


def test_seen_store_round_trips_and_is_per_account(tmp_path):
    a = SeenStore(tmp_path, "me@example.com", now=NOW)
    a.add("m1", NOW)
    a.save()
    assert a.path.name == "promotions_seen_me_example_com.json"

    again = SeenStore(tmp_path, "me@example.com", now=NOW)
    assert "m1" in again
    assert "m1" not in SeenStore(tmp_path, "other@example.com", now=NOW)


def test_seen_store_prunes_old_ids(tmp_path):
    s = SeenStore(tmp_path, "me@example.com", now=NOW)
    s.add("old", NOW - timedelta(days=SEEN_TTL_DAYS + 1))
    s.add("fresh", NOW - timedelta(days=1))
    s.save()
    reloaded = SeenStore(tmp_path, "me@example.com", now=NOW)
    assert "fresh" in reloaded
    assert "old" not in reloaded


def test_seen_store_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "promotions_seen_me_example_com.json"
    path.write_text("{not json", encoding="utf-8")
    assert len(SeenStore(tmp_path, "me@example.com", now=NOW)) == 0


# --- scan_account ------------------------------------------------------------------


ACME = {"verdict": "application", "confidence": 0.9, "company_candidates": ["Acme"],
          "position": "Software Engineer Intern, Summer 2027",
          "action_required": "Send unofficial transcripts as PDF", "summary": "Transcript request"}
UNSURE = {"verdict": "unsure", "confidence": 0.4, "company_candidates": ["Initech"], "summary": "Candidate account"}


def _scan(emails, classifier, matcher, tmp_path, dry_run=False, since=SINCE, account="me@example.com"):
    client = _Client(emails, account=account)
    store = SeenStore(tmp_path, account, now=NOW)
    findings = scan_account(client, classifier, "T {subject} {sender} {date} {body}", matcher, since, store, dry_run=dry_run)
    return client, store, findings


def test_scan_sorts_into_buckets(tmp_path):
    emails = [
        _email("One more thing for your Acme application", hours_ago=20),
        _email("Level up this month with Globex", hours_ago=10),
        _email("Your Initech candidate profile is ready", hours_ago=5),
    ]
    classifier = _Classifier({"Acme application": ACME, "Initech": UNSURE})
    matched_to = {"Acme": _outcome(job=_row()), "Initech": _outcome(reason="no matching job for Initech")}
    matcher = lambda c, e: matched_to[c.company_candidates[0]]

    _client, store, findings = _scan(emails, classifier, matcher, tmp_path)

    assert [(f.bucket, f.email.subject) for f in findings] == [
        (BUCKET_MATCHED, "One more thing for your Acme application"),
        (BUCKET_REVIEW, "Your Initech candidate profile is ready"),
    ]
    # Every decided email is recorded, the ignored newsletter included.
    assert all(e.message_id in store for e in emails)
    assert store.path.exists()


def test_scan_uses_the_promotions_query_and_the_window(tmp_path):
    inside = _email("Inside", hours_ago=24)
    outside = _email("Outside", hours_ago=26)
    classifier = _Classifier()
    client, _store, _ = _scan([inside, outside], classifier, lambda c, e: None, tmp_path)

    assert "category:promotions" in client.queries[0]
    assert "in:inbox" in client.queries[0]
    assert len(classifier.prompts) == 1
    assert "Inside" in classifier.prompts[0]


def test_scan_skips_ids_already_reported(tmp_path):
    email = _email("One more thing for your Acme application")
    store = SeenStore(tmp_path, "me@example.com", now=NOW)
    store.add(email.message_id, NOW)
    store.save()
    classifier = _Classifier({"Acme application": ACME})

    _, _, findings = _scan([email], classifier, lambda c, e: _outcome(job=_row()), tmp_path)

    assert findings == []
    assert classifier.prompts == []


def test_dry_run_records_nothing(tmp_path):
    email = _email("One more thing for your Acme application")
    classifier = _Classifier({"Acme application": ACME})
    _, store, findings = _scan([email], classifier, lambda c, e: _outcome(job=_row()), tmp_path, dry_run=True)

    assert len(findings) == 1
    assert email.message_id not in store
    assert not store.path.exists()


def test_scan_does_not_call_the_matcher_without_a_company(tmp_path):
    email = _email("We need one more thing")
    classifier = _Classifier({"one more thing": {"verdict": "application", "company_candidates": []}})
    calls = []
    _, _, findings = _scan([email], classifier, lambda c, e: calls.append(c), tmp_path)

    assert calls == []
    assert findings[0].bucket == BUCKET_REVIEW


def test_unreadable_ai_answer_is_still_reported(tmp_path):
    email = _email("Something about your application")
    classifier = _Classifier({"Something": "no json here"})
    _, _, findings = _scan([email], classifier, lambda c, e: None, tmp_path)

    assert findings[0].bucket == BUCKET_REVIEW
    assert "triaged" in findings[0].reason


def test_sensitive_mail_is_never_sent_to_the_ai(tmp_path):
    email = _email("Reset your password", body="Click here to reset your password now.")
    classifier = _Classifier({"password": ACME})
    _, store, findings = _scan([email], classifier, lambda c, e: None, tmp_path)

    assert findings == []
    assert classifier.prompts == []
    assert email.message_id in store


# --- build_message -----------------------------------------------------------------


def test_no_findings_means_no_message():
    assert build_message([], NOW) == ""


def test_message_shows_row_for_matched_and_subject_date_for_review():
    acme = _email("One more thing for your Acme application", hours_ago=20, account="a@x.com")
    initech = _email("Your Initech candidate profile is ready", hours_ago=5, account="b@y.edu")
    findings = [
        Finding(email=acme, bucket=BUCKET_MATCHED, triage=Triage("application", 0.9, ["Acme"], action_required="Send transcripts"), job=_row(1234)),
        Finding(email=initech, bucket=BUCKET_REVIEW, triage=Triage("unsure", 0.4, ["Initech"]), reason="AI unsure"),
    ]
    msg = build_message(findings, NOW)

    assert "**Acme** — SWE Intern (row 1234, Applied)" in msg
    assert "Send transcripts" in msg
    assert '"Your Initech candidate profile is ready"' in msg
    assert "b@y.edu" in msg
    assert "Initech · AI unsure" in msg
    assert msg.index("About one of your applications") < msg.index("Please review")


def test_message_lists_tied_rows():
    tied = [_row(41, "Vandelay"), _row(42, "Vandelay")]
    f = Finding(email=_email("Vandelay | Thanks for applying"), bucket=BUCKET_REVIEW, triage=Triage("application", 0.9, ["Vandelay"]), candidates=tied, reason="ambiguous")
    assert "rows 41, 42" in build_message([f], NOW)


def test_message_stays_under_the_discord_cap():
    findings = [
        Finding(email=_email(f"A long subject line number {i} that goes on and on", msg_id=f"m{i}"), bucket=BUCKET_REVIEW,
                triage=Triage("unsure", 0.4, ["Company"]), reason="AI unsure whether this is about an application")
        for i in range(60)
    ]
    msg = build_message(findings, NOW)
    assert len(msg) <= DISCORD_LIMIT
    assert "…and" in msg

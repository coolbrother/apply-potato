"""
Tests for the Jev shadow classifier: the classifier wrapper, the shadow log, the config
switch, and the guarantee that shadow mode changes nothing the pipeline does.

The TypeSafe client is always mocked; no test reaches the network.

Usage:
    pytest tests/test_jev_shadow.py -v
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts.daily_summary import MAX_JEV_DETAIL, _jev_block
from src.config import JEV_MODE_OFF, JEV_MODE_SHADOW, load_config
from src.email_classifier import EmailClassification
from src.gmail import EmailMessage
from src.email_categories import CATEGORIES, CATEGORIES_FILENAME, load_categories
from src.jev_classifier import JevClassifier, JevResult, load_prompt
from src import jev_log
from src.sheets import COLUMNS
from tests.mocks.mock_sheets import MockSheetsClient

PROMPTS = Path(__file__).parent.parent / "prompts"


# =============================================================================
# Helpers
# =============================================================================

def _email(message_id: str = "msg-1", body_text: str = "Thanks for applying to Uber.",
           body_html: str = "", when: datetime = datetime(2026, 10, 6, 14, 51)) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        subject="Your application",
        sender="Recruiting",
        sender_email="noreply@example.com",
        date=when,
        body_text=body_text,
        body_html=body_html,
        category="Updates",
        account="me@example.com",
    )


def _jev(label: str, confidence: float = 0.95, **probabilities) -> JevResult:
    probs = probabilities or {label: confidence}
    return JevResult(label=label, confidence=confidence, probabilities=probs, model="jev-test")


def _sdk_response(choice="confirmation", confidence=0.91, probabilities=None):
    answer = SimpleNamespace(choice=choice, confidence=confidence,
                             probabilities=probabilities or {"confirmation": 0.91, "unknown": 0.09})
    return SimpleNamespace(choices={"category": answer}, model="jev-1.13.0",
                           usage=SimpleNamespace(input_tokens=812, output_tokens=0))


def _classifier(key="ts-key", prompts_dir=PROMPTS):
    return JevClassifier(SimpleNamespace(typesafe_api_key=key, prompts_dir=prompts_dir))


# =============================================================================
# JevClassifier
# =============================================================================

class TestJevClassifier:
    def test_maps_the_sdk_answer(self):
        jc = _classifier()
        client = MagicMock()
        client.system_one.return_value = _sdk_response()
        jc._client = client

        result = jc.classify(_email())

        assert result.label == "confirmation"
        assert result.confidence == pytest.approx(0.91)
        assert result.probabilities == {"confirmation": 0.91, "unknown": 0.09}
        assert result.model == "jev-1.13.0"
        assert result.input_tokens == 812

    def test_sends_one_choice_question_over_all_categories(self):
        jc = _classifier()
        client = MagicMock()
        client.system_one.return_value = _sdk_response()
        jc._client = client

        jc.classify(_email())

        state, questions = client.system_one.call_args.args
        assert set(questions) == {"category"}
        instructions, criteria = load_prompt(PROMPTS)
        assert questions["category"].instructions == instructions
        assert dict(questions["category"].criteria) == criteria
        assert tuple(criteria) == CATEGORIES

    def test_broken_prompt_file_returns_none_and_never_raises(self, tmp_path):
        (tmp_path / CATEGORIES_FILENAME).write_text("[guidance]\ng\n[oa]\nx\n", encoding="utf-8")
        jc = _classifier(prompts_dir=tmp_path)
        jc._client = MagicMock()
        assert jc.classify(_email()) is None
        jc._client.system_one.assert_not_called()

    def test_state_carries_the_whole_body(self):
        """No truncation: the trial compares Jev and the LLM on identical input."""
        long_body = "x" * 50_000
        state = JevClassifier.build_state(_email(body_text=long_body))
        assert state["body"] == long_body
        assert state["subject"] == "Your application"
        assert state["sender"] == "Recruiting <noreply@example.com>"
        assert state["date"] == "2026-10-06 14:51"

    def test_html_only_mail_is_converted_like_the_llm_path(self):
        state = JevClassifier.build_state(
            _email(body_text="", body_html="<p>We received your application</p>"))
        assert "We received your application" in state["body"]

    def test_no_key_means_no_call(self):
        jc = _classifier(key=None)
        jc._client = MagicMock()
        assert jc.classify(_email()) is None
        jc._client.system_one.assert_not_called()

    def test_empty_body_means_no_call(self):
        jc = _classifier()
        jc._client = MagicMock()
        assert jc.classify(_email(body_text="", body_html="")) is None
        jc._client.system_one.assert_not_called()

    def test_api_failure_returns_none_and_never_raises(self):
        jc = _classifier()
        jc._client = MagicMock()
        jc._client.system_one.side_effect = RuntimeError("timeout")
        assert jc.classify(_email()) is None

    def test_sdk_debug_logging_is_held_above_debug(self):
        """Its debug log carries request bodies — whole emails — unredacted."""
        assert logging.getLogger("typesafe_sdk").level >= logging.INFO

    def test_status_probability_excludes_unknown(self):
        jev = _jev("unknown", 0.6, unknown=0.6, confirmation=0.3, oa=0.1)
        assert jev.status_probability() == pytest.approx(0.4)


class TestJevPrompt:
    """Jev reads only the shared categories file."""

    def test_question_is_the_shared_guidance(self):
        guidance, _ = load_categories(PROMPTS / CATEGORIES_FILENAME)
        instructions, _ = load_prompt(PROMPTS)
        assert instructions == guidance
        assert instructions.startswith("A CUE COUNTS WHEREVER IT APPEARS")

    def test_needs_nothing_but_the_shared_file(self, tmp_path):
        (tmp_path / CATEGORIES_FILENAME).write_text(
            (PROMPTS / CATEGORIES_FILENAME).read_text(encoding="utf-8"), encoding="utf-8")
        instructions, criteria = load_prompt(tmp_path)  # no other file in the directory
        assert tuple(criteria) == CATEGORIES


# =============================================================================
# Shadow log
# =============================================================================

class TestReasonsToRecord:
    def test_confident_agreement_is_not_recorded(self):
        assert jev_log.reasons_to_record("oa", _jev("oa", 0.97), 0.8) == []

    def test_disagreement(self):
        assert jev_log.reasons_to_record("oa", _jev("technical", 0.9), 0.8) == ["disagree"]

    def test_low_confidence_even_on_agreement(self):
        assert jev_log.reasons_to_record("oa", _jev("oa", 0.55), 0.8) == ["low_confidence"]

    def test_possible_silent_miss_under_llm_unknown(self):
        """The Meta shape: the LLM files a receipt as unknown, Jev sees a status."""
        jev = _jev("unknown", 0.85, unknown=0.7, confirmation=0.3)
        assert jev_log.reasons_to_record("unknown", jev, 0.8) == ["possible_miss"]

    def test_faint_status_weight_under_unknown_is_not_a_miss(self):
        jev = _jev("unknown", 0.9, unknown=0.9, confirmation=0.1)
        assert jev_log.reasons_to_record("unknown", jev, 0.8) == []

    def test_reasons_combine(self):
        jev = _jev("confirmation", 0.5, confirmation=0.5, unknown=0.5)
        assert jev_log.reasons_to_record("unknown", jev, 0.8) == [
            "disagree", "low_confidence", "possible_miss"]


class TestRecord:
    def test_writes_an_entry_with_both_answers(self, tmp_path):
        jev = _jev("confirmation", 0.62, confirmation=0.62, unknown=0.38)
        assert jev_log.record(tmp_path, email=_email(), llm_category="unknown",
                              llm_confidence=0.9, jev=jev, reasons=["disagree"])

        [entry] = json.loads((tmp_path / "jev_shadow.json").read_text(encoding="utf-8"))
        assert entry["message_id"] == "msg-1"
        assert entry["llm"] == {"category": "unknown", "confidence": 0.9}
        assert entry["jev"]["category"] == "confirmation"
        assert list(entry["jev"]["probabilities"]) == ["confirmation", "unknown"]
        assert entry["reasons"] == ["disagree"]

    def test_deduped_on_message_id(self, tmp_path):
        kwargs = dict(email=_email(), llm_category="oa", llm_confidence=0.9,
                      jev=_jev("technical"), reasons=["disagree"])
        assert jev_log.record(tmp_path, **kwargs) is True
        assert jev_log.record(tmp_path, **kwargs) is False
        assert len(jev_log.load_entries(tmp_path)) == 1

    def test_unwritable_location_returns_false_and_never_raises(self, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        assert jev_log.record(blocker, email=_email(), llm_category="oa", llm_confidence=0.9,
                              jev=_jev("technical"), reasons=["disagree"]) is False

    def test_corrupt_file_reads_as_empty(self, tmp_path):
        (tmp_path / "jev_shadow.json").write_text("{not json", encoding="utf-8")
        assert jev_log.load_entries(tmp_path) == []


# =============================================================================
# Config
# =============================================================================

class TestConfig:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path):
        for key, value in {
            "USER_EMAIL": "me@example.com", "GOOGLE_SHEET_ID": "sheet",
            "GITHUB_REPOS": "owner/repo", "OPENAI_API_KEY": "sk-test", "AI_PROVIDER": "openai",
        }.items():
            monkeypatch.setenv(key, value)
        for key in ("TYPESAFE_API_KEY", "JEV_MODE", "JEV_LOG_CONFIDENCE"):
            monkeypatch.delenv(key, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("", encoding="utf-8")
        return monkeypatch, env_file

    def test_default_is_off(self, env):
        _, env_file = env
        config = load_config(env_file)
        assert config.jev_mode == JEV_MODE_OFF
        assert config.jev_log_confidence == 0.8
        assert config.jev_alert_confidence == 0.9

    def test_alert_threshold_is_configurable(self, env):
        monkeypatch, env_file = env
        monkeypatch.setenv("JEV_ALERT_CONFIDENCE", "1.5")
        assert load_config(env_file).jev_alert_confidence == 1.5

    def test_shadow_with_key(self, env):
        monkeypatch, env_file = env
        monkeypatch.setenv("JEV_MODE", "Shadow")
        monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
        config = load_config(env_file)
        assert config.jev_mode == JEV_MODE_SHADOW
        assert config.typesafe_api_key == "ts-key"

    def test_shadow_without_key_runs_off(self, env, capsys):
        monkeypatch, env_file = env
        monkeypatch.setenv("JEV_MODE", "shadow")
        assert load_config(env_file).jev_mode == JEV_MODE_OFF
        assert "needs TYPESAFE_API_KEY" in capsys.readouterr().out

    def test_unknown_value_runs_off(self, env, capsys):
        monkeypatch, env_file = env
        monkeypatch.setenv("JEV_MODE", "decide")
        monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
        assert load_config(env_file).jev_mode == JEV_MODE_OFF
        assert "JEV_MODE must be one of" in capsys.readouterr().out


# =============================================================================
# check_gmail: shadow changes nothing
# =============================================================================

def _classification(category: str) -> EmailClassification:
    return EmailClassification(category=category, confidence=0.95,
                               company_candidates=["Uber"], position="SWE Intern")


def _run(tmp_path, jev_mode, jev_answers, alert_confidence=0.9, notify=None):
    """One checker run over a fixed batch; returns (stats, sheet rows, jev mock)."""
    sheets = MockSheetsClient()
    # A fixed Added Date: the mock otherwise stamps now(), and two runs straddling a
    # second boundary would differ for a reason that has nothing to do with Jev.
    sheets.add_job({"company": "Uber", "position": "SWE Intern",
                    "added_date": "10/01/2026 08:00:00"})

    batch = [
        _email("confirmation", when=datetime(2026, 10, 1, 9, 0)),
        _email("newsletter", when=datetime(2026, 10, 2, 9, 0)),
        _email("oa", when=datetime(2026, 10, 3, 9, 0)),
    ]
    by_id = {"confirmation": _classification("confirmation"),
             "newsletter": _classification("unknown"),
             "oa": _classification("oa")}

    client = MagicMock(label="me@example.com")
    client.fetch_recent_emails.return_value = batch
    classifier = MagicMock()
    classifier.classify.side_effect = lambda e: by_id[e.message_id]
    jev = MagicMock()
    jev.classify.side_effect = lambda e: jev_answers.get(e.message_id)

    config = SimpleNamespace(
        discord=SimpleNamespace(enabled=False),
        user=SimpleNamespace(target_companies=[]),
        gmail_lookback_days=3,
        jev_mode=jev_mode,
        jev_log_confidence=0.8,
        jev_alert_confidence=alert_confidence,
        data_dir=tmp_path,
    )
    with patch("check_gmail.get_gmail_clients", return_value=[client]), \
         patch("check_gmail.get_classifier", return_value=classifier), \
         patch("check_gmail.get_jev_classifier", return_value=jev), \
         patch("check_gmail.get_sheets_client", return_value=sheets), \
         patch("check_gmail.notify_jev_flag", notify or MagicMock()), \
         patch("check_gmail.apply_privacy_filters", return_value=(True, "")):
        from check_gmail import GmailChecker

        stats = GmailChecker(config).run()
    return stats, [list(r) for r in sheets.rows], jev


class TestCheckGmailShadow:
    ANSWERS = {
        "confirmation": _jev("confirmation", 0.97),
        "newsletter": _jev("unknown", 0.8, unknown=0.65, confirmation=0.35),  # possible miss
        "oa": _jev("technical", 0.9),                                          # disagreement
    }

    def test_shadow_leaves_the_sheet_and_decisions_unchanged(self, tmp_path):
        off_stats, off_rows, off_jev = _run(tmp_path / "off", JEV_MODE_OFF, self.ANSWERS)
        on_stats, on_rows, _ = _run(tmp_path / "on", JEV_MODE_SHADOW, self.ANSWERS)

        assert on_rows == off_rows
        strip = lambda s: {k: v for k, v in s.items() if not k.startswith("jev_")}
        assert strip(on_stats) == strip(off_stats)
        off_jev.classify.assert_not_called()

    def test_shadow_counts_and_records(self, tmp_path):
        stats, rows, jev = _run(tmp_path, JEV_MODE_SHADOW, self.ANSWERS)

        assert jev.classify.call_count == 3  # unknown mail is compared too
        assert stats["jev_agree"] == 2
        assert stats["jev_disagree"] == 1
        assert stats["jev_recorded"] == 2
        entries = {e["message_id"]: e for e in jev_log.load_entries(tmp_path)}
        assert set(entries) == {"newsletter", "oa"}
        assert entries["newsletter"]["reasons"] == ["possible_miss"]
        assert entries["oa"]["reasons"] == ["disagree"]
        assert rows[1][COLUMNS["status"]] == "OA"  # the LLM's verdict, not Jev's (row 0 is headers)

    def test_jev_failure_is_counted_and_harmless(self, tmp_path):
        stats, rows, _ = _run(tmp_path, JEV_MODE_SHADOW, {})
        assert stats["jev_failed"] == 3
        assert stats["updated"] == 2
        assert jev_log.load_entries(tmp_path) == []


# =============================================================================
# Review mode: immediate alert and daily summary
# =============================================================================

MISSED_RECEIPT = {"newsletter": _jev("confirmation", 0.99, confirmation=0.99, unknown=0.01)}


class TestImmediateAlert:
    def test_should_alert_only_for_a_sure_status_under_llm_unknown(self):
        assert jev_log.should_alert("unknown", _jev("confirmation", 0.99), 0.9)
        assert not jev_log.should_alert("unknown", _jev("confirmation", 0.85), 0.9)
        assert not jev_log.should_alert("oa", _jev("technical", 0.99), 0.9)   # LLM acted
        assert not jev_log.should_alert("unknown", _jev("unknown", 0.99), 0.9)
        assert not jev_log.should_alert("unknown", _jev("confirmation", 1.0), 1.5)  # off

    def test_alert_message_names_both_answers_and_the_email(self):
        msg = jev_log.alert_message(_email(), "unknown", _jev("confirmation", 0.99))
        assert "Possible missed status email" in msg
        assert "**confirmation** (0.99)" in msg
        assert "LLM: unknown" in msg
        assert "no row · Your application · 10/06 14:51" in msg
        msg = jev_log.alert_message(_email(), "unknown", _jev("confirmation", 0.99), [2534])
        assert "row 2534 · Your application · 10/06 14:51" in msg
        assert "example.com" not in msg

    def test_run_sends_the_alert_once(self, tmp_path):
        notify = MagicMock()
        stats, _, _ = _run(tmp_path, JEV_MODE_SHADOW, MISSED_RECEIPT, notify=notify)
        assert stats["jev_alerted"] == 1
        notify.assert_called_once()
        assert "**confirmation** (0.99)" in notify.call_args.args[0]

    def test_flagged_email_carries_its_row(self, tmp_path):
        """An unknown email skips the matcher on the normal path; a flagged one is looked up."""
        notify = MagicMock()
        _run(tmp_path, JEV_MODE_SHADOW, MISSED_RECEIPT, notify=notify)
        [entry] = jev_log.load_entries(tmp_path)
        assert entry["rows"] == [2]  # the seeded Uber row (row 1 is headers)
        assert "row 2 ·" in notify.call_args.args[0]

    def test_reprocessing_the_same_mail_does_not_repeat_it(self, tmp_path):
        notify = MagicMock()
        _run(tmp_path, JEV_MODE_SHADOW, MISSED_RECEIPT, notify=notify)
        stats, _, _ = _run(tmp_path, JEV_MODE_SHADOW, MISSED_RECEIPT, notify=notify)
        assert stats["jev_alerted"] == 0
        notify.assert_called_once()

    def test_threshold_above_one_sends_nothing(self, tmp_path):
        notify = MagicMock()
        stats, _, _ = _run(tmp_path, JEV_MODE_SHADOW, MISSED_RECEIPT,
                           alert_confidence=1.5, notify=notify)
        assert stats["jev_recorded"] == 1
        notify.assert_not_called()

    def test_discord_disabled_sends_nothing(self):
        from src import notifications
        config = SimpleNamespace(discord=SimpleNamespace(enabled=False))
        sender = MagicMock()
        with patch("src.notifications.get_config", return_value=config), \
             patch("src.notifications.get_discord_sender", return_value=sender):
            assert notifications.notify_jev_flag("x") is False
        sender.send_message.assert_not_called()


def _entry(ts: str, reasons, llm="unknown", jev="confirmation", conf=0.99,
           subject="Confirm your profile", sender="no-reply@recruiting.example.com"):
    return {"timestamp": ts, "reasons": reasons, "subject": subject, "sender": sender,
            "llm": {"category": llm, "confidence": 0.9},
            "jev": {"category": jev, "confidence": conf, "probabilities": {}}}


class TestDailySummarySection:
    START, END = datetime(2026, 10, 10, 9, 0), datetime(2026, 10, 10, 17, 0)

    def test_window_and_reasons(self):
        entries = [
            _entry("2026-10-10T08:59:00", ["disagree"]),          # before the window
            _entry("2026-10-10T10:00:00", ["disagree"]),
            _entry("2026-10-10T11:00:00", ["low_confidence"]),    # not surfaced
            _entry("2026-10-10T12:00:00", ["possible_miss"]),
            _entry("2026-10-10T17:01:00", ["disagree"]),          # after
            _entry("not a time", ["disagree"]),
        ]
        picked = jev_log.summary_entries(entries, self.START, self.END)
        assert [e["timestamp"] for e in picked] == ["2026-10-10T10:00:00", "2026-10-10T12:00:00"]

    def test_line_format(self):
        entry = _entry("2026-10-10T10:00:00", ["disagree"])
        assert jev_log.summary_line(entry) == "unknown → confirmation 0.99 · no row · Confirm your profile"
        entry["rows"] = [1842]
        assert jev_log.summary_line(entry) == "unknown → confirmation 0.99 · row 1842 · Confirm your profile"
        entry["rows"] = [1025, 1842]
        assert "· rows 1025/1842 ·" in jev_log.summary_line(entry)

    def test_possible_miss_line_names_the_leaning_status(self):
        entry = _entry("t", ["possible_miss"], jev="unknown", conf=0.6)
        entry["jev"]["probabilities"] = {"unknown": 0.6, "confirmation": 0.26, "oa": 0.14}
        assert jev_log.summary_line(entry).startswith(
            "unknown → unknown 0.60, leans confirmation 0.26 · ")

    def test_long_subject_is_cut(self):
        line = jev_log.summary_line(_entry("t", ["disagree"], subject="x" * 200))
        assert line.endswith("x" * (jev_log.MAX_SUBJECT - 1) + "…")

    def test_empty_window_omits_the_section(self):
        assert _jev_block([]) == ""

    def test_block_lists_then_collapses(self):
        entries = [_entry(f"2026-10-10T1{i}:00:00", ["disagree"]) for i in range(MAX_JEV_DETAIL + 2)]
        block = _jev_block(entries)
        assert block.startswith("🤖 Jev second opinion:")
        assert f"**{MAX_JEV_DETAIL + 2}**" in block
        assert block.count("• unknown → confirmation") == MAX_JEV_DETAIL
        assert "• +2 more" in block

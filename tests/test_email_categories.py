"""
Tests for the shared email categories file and the withdrawn category.

prompts/email_categories.txt is the single source of the classification rules for the
LLM classifier and for Jev. These tests pin that both are built from it, and that a
withdrawal email moves a row to Withdrawn the way the other outcomes are written.

Usage:
    pytest tests/test_email_categories.py -v
"""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.email_categories import (
    CATEGORIES,
    CATEGORIES_FILENAME,
    jev_criteria,
    load_categories,
    render_llm,
)
from src.email_classifier import EmailClassification, EmailClassifier
from src.gmail import EmailMessage
from src.jev_classifier import load_prompt
from src.sheets import COLUMNS, STATUS_WITHDRAWN, event_key, event_label
from tests.mocks.mock_sheets import MockSheetsClient

PROMPTS = Path(__file__).parent.parent / "prompts"
SHARED = PROMPTS / CATEGORIES_FILENAME


def _llm_prompt() -> str:
    return EmailClassifier(SimpleNamespace(prompts_dir=PROMPTS)).prompt_template


# =============================================================================
# The shared file
# =============================================================================

class TestSharedFile:
    def test_loads_every_category_in_order(self):
        guidance, categories = load_categories(SHARED)
        assert tuple(categories) == CATEGORIES
        assert "withdrawn" in CATEGORIES
        assert "A CUE COUNTS WHEREVER IT APPEARS" in guidance
        assert all(c.summary for c in categories.values())

    def test_comments_are_not_loaded(self):
        guidance, categories = load_categories(SHARED)
        texts = [guidance] + [c.description() for c in categories.values()]
        assert not any(line.startswith("#") for t in texts for line in t.splitlines())

    @pytest.mark.parametrize("content, problem", [
        ("[oa]\nx\n", "no [guidance]"),
        ("[guidance]\ng\n[oa]\nx\n", "must be exactly"),
        ("[guidance]\ng\n" + "".join(f"[{c}]\nd\n" for c in CATEGORIES[:-1]) + "[unknown]\n",
         "has no summary"),
    ])
    def test_malformed_file_is_rejected(self, tmp_path, content, problem):
        path = tmp_path / CATEGORIES_FILENAME
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match=problem.replace("[", r"\[").replace("]", r"\]")):
            load_categories(path)


class TestBothModelsGetTheSameRules:
    """The point of one file: a rule written once reaches both classifiers."""

    def test_llm_prompt_has_every_category_summary_and_rule(self):
        prompt = _llm_prompt()
        guidance, categories = load_categories(SHARED)
        assert guidance in prompt
        for cat in categories.values():
            assert cat.summary.splitlines()[0] in prompt, cat.name
            if cat.rules:
                assert f"{cat.name}:\n{cat.rules}" in prompt, cat.name

    def test_llm_prompt_lists_every_category_in_the_json_schema(self):
        assert f'"category": "{" | ".join(CATEGORIES)}"' in _llm_prompt()

    def test_llm_prompt_has_no_unfilled_category_placeholder(self):
        prompt = _llm_prompt()
        for placeholder in ("{category_list}", "{category_names}", "{category_rules}"):
            assert placeholder not in prompt
        # The per-email placeholders are still there, to be filled for each message.
        for placeholder in ("{subject}", "{sender}", "{date}", "{body}"):
            assert placeholder in prompt

    def test_jev_gets_each_category_whole(self):
        _, categories = load_categories(SHARED)
        _, criteria = load_prompt(PROMPTS)
        assert criteria == jev_criteria(categories)
        assert "A reply to the applicant's withdrawal" in criteria["withdrawn"]

    def test_list_bullets_indent_wrapped_summaries(self):
        _, categories = load_categories(SHARED)
        rendered = render_llm("{category_list}", "g", categories)
        assert '- "withdrawn": The applicant withdrew' in rendered
        assert "\n  acknowledges it" in rendered

    def test_rules_follow_the_file_order_with_stage_done_first(self):
        """
        Placement changes behaviour: with stage_done's rules after the invitation
        categories, nano read "invited to interview" as a completed stage. The rules keep
        the order the original prompt had; the category list keeps CATEGORIES order.
        """
        prompt = _llm_prompt()
        rules = prompt[prompt.index("CLASSIFICATION GUIDELINES:"):]
        positions = [rules.index(f"\n{name}:\n") for name in
                     ("stage_done", "confirmation", "oa", "phone", "technical", "offer",
                      "rejection", "withdrawn")]
        assert positions == sorted(positions)
        assert rules.index("\nstage_done:\n") < rules.index("A CUE COUNTS WHEREVER IT APPEARS")
        listing = prompt[:prompt.index("Extract the following fields")]
        assert [listing.index(f'- "{c}":') for c in CATEGORIES] == \
            sorted(listing.index(f'- "{c}":') for c in CATEGORIES)

    def test_file_may_order_sections_freely_but_not_drop_one(self, tmp_path):
        text = SHARED.read_text(encoding="utf-8")
        path = tmp_path / CATEGORIES_FILENAME
        path.write_text(text.replace("[withdrawn]", "[withdrew]"), encoding="utf-8")
        with pytest.raises(ValueError, match="must be exactly"):
            load_categories(path)

    def test_withdrawal_rules_reach_rejection_too(self):
        """The case that prompted this: a reply to the user's withdrawal read as a rejection."""
        _, categories = load_categories(SHARED)
        assert '"withdrawn", not "rejection"' in categories["rejection"].rules
        assert "out-of-office" in categories["withdrawn"].rules


# =============================================================================
# The classifier accepts withdrawn
# =============================================================================

class TestClassifierParsesWithdrawn:
    def test_withdrawn_is_a_valid_category(self):
        clf = EmailClassifier(SimpleNamespace(prompts_dir=PROMPTS))
        result = clf._parse_response(json.dumps(
            {"category": "withdrawn", "confidence": 0.9, "company_candidates": ["Acme"]}))
        assert result.category == "withdrawn"

    def test_made_up_category_still_falls_back_to_unknown(self):
        clf = EmailClassifier(SimpleNamespace(prompts_dir=PROMPTS))
        result = clf._parse_response(json.dumps(
            {"category": "declined", "company_candidates": ["Acme"]}))
        assert result.category == "unknown"

    def test_last_event_label_round_trips(self):
        assert event_label("withdrawn") == "Withdrawn"
        assert event_key("Withdrawn") == "withdrawn"


# =============================================================================
# check_gmail writes Withdrawn
# =============================================================================

def _email(when=datetime(2026, 10, 5, 12, 0)) -> EmailMessage:
    return EmailMessage(
        message_id="w-1", subject="Re: Withdrawal of Application", sender="Recruiting",
        sender_email="recruiting@acme.example", date=when, body_text="Sorry to see you go.",
        body_html="", category="Primary",
    )


def _withdrawn() -> EmailClassification:
    return EmailClassification(category="withdrawn", confidence=0.95,
                               company_candidates=["Acme"], position="SWE Intern",
                               key_details="Applicant withdrew")


def _checker(sheets):
    config = SimpleNamespace(discord=SimpleNamespace(enabled=False),
                             user=SimpleNamespace(target_companies=[]))
    with patch("check_gmail.get_gmail_clients", return_value=[MagicMock()]), \
         patch("check_gmail.get_classifier"), \
         patch("check_gmail.get_sheets_client", return_value=sheets):
        from check_gmail import GmailChecker
        return GmailChecker(config)


def _row(sheets, status: str) -> int:
    row = sheets.add_job({"company": "Acme", "position": "SWE Intern",
                          "added_date": "10/01/2026 08:00:00"})
    sheets.rows[row - 1][COLUMNS["status"]] = status
    return row


def _cell(sheets, row: int, column: str) -> str:
    return sheets.rows[row - 1][COLUMNS[column]]


class TestWithdrawnWrites:
    @pytest.mark.parametrize("before", ["Applied", "OA", "Phone", "Technical", "Offer"])
    def test_moves_a_live_row_to_withdrawn_at_any_stage(self, before):
        sheets = MockSheetsClient()
        row = _row(sheets, before)
        checker = _checker(sheets)
        job = next(j for j in sheets.get_all_jobs() if j.row_number == row)

        assert checker._update_job_status(job, _withdrawn(), _email()) is True
        assert _cell(sheets, row, "status") == STATUS_WITHDRAWN
        assert _cell(sheets, row, "last_event") == "Withdrawn"
        assert "Applicant withdrew" in _cell(sheets, row, "notes")

    @pytest.mark.parametrize("before", ["Rejected", "Ghosted"])
    def test_an_ended_row_stays_ended(self, before):
        sheets = MockSheetsClient()
        row = _row(sheets, before)
        checker = _checker(sheets)
        job = next(j for j in sheets.get_all_jobs() if j.row_number == row)

        checker._update_job_status(job, _withdrawn(), _email())
        assert _cell(sheets, row, "status") == before

    def test_withdrawn_is_final_against_a_later_rejection(self):
        sheets = MockSheetsClient()
        row = _row(sheets, "Applied")
        checker = _checker(sheets)
        job = next(j for j in sheets.get_all_jobs() if j.row_number == row)
        checker._update_job_status(job, _withdrawn(), _email())

        job = next(j for j in sheets.get_all_jobs() if j.row_number == row)
        rejection = EmailClassification(category="rejection", confidence=0.9,
                                        company_candidates=["Acme"])
        checker._update_job_status(job, rejection, _email(datetime(2026, 10, 6, 9, 0)))
        assert _cell(sheets, row, "status") == STATUS_WITHDRAWN

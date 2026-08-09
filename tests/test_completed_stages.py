"""
Tests for the Completed Stages column and the completion-email path.

    pytest tests/test_completed_stages.py -v
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.email_classifier import EmailClassifier
from src.sheets import (
    COLUMNS,
    COMPLETABLE_STAGES,
    HEADERS,
    LAST_COL,
    add_stage,
    event_key,
    event_label,
    col_letter,
    reached_stage,
    remove_stage,
    split_stages,
)


def _load_daily_summary():
    """
    scripts/ is not a package, so the summary is loaded by path. Worth the awkwardness:
    _stage_progress holds the rule that decides what appears in the Discord to-do list,
    and testing it through the Sheets API instead would test almost nothing.
    """
    path = Path(__file__).resolve().parents[1] / "scripts" / "daily_summary.py"
    spec = importlib.util.spec_from_file_location("daily_summary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_stage_progress = _load_daily_summary()._stage_progress


class TestSchema:
    def test_completed_stages_is_column_w(self):
        assert col_letter(COLUMNS["completed_stages"]) == "W"
        assert HEADERS[COLUMNS["completed_stages"]] == "Completed Stages"

    def test_last_event_is_column_x(self):
        """
        The record cannot say whether newer work arrived. This column can: an "oa" here
        on a row already carrying "OA" in W means one assessment is done and another is
        waiting — row 308's exact state.
        """
        assert col_letter(COLUMNS["last_event"]) == "X"
        assert HEADERS[COLUMNS["last_event"]] == "Last Event"
        assert LAST_COL == "X"

    def test_event_labels_do_not_collide_with_other_columns(self):
        """
        Every earlier spelling read as a restatement of a different column. "Stage Done"
        echoed Completed Stages; "Applied" duplicated Status while meaning something
        else — the stage a row sits at, versus the email that just arrived.
        """
        assert event_label("stage_done") == "Assessment Submitted"
        assert event_label("confirmation") == "Application Received"
        assert event_label("oa") == "OA Invite"

    def test_category_labels_round_trip(self):
        for key in ("oa", "phone", "technical", "stage_done", "rejection", "offer"):
            assert event_key(event_label(key)) == key

    def test_earlier_spellings_still_resolve(self):
        """Rows written before the rename must not silently stop matching."""
        assert event_key("oa") == "oa"
        assert event_key("Stage Done") == "stage_done"
        assert event_key("stage_done") == "stage_done"

    def test_offer_is_not_completable(self):
        """An offer is an outcome, not work the applicant does."""
        assert "Offer" not in COMPLETABLE_STAGES
        assert COMPLETABLE_STAGES == ("OA", "Phone", "Technical")


class TestStageCell:
    def test_adds_to_an_empty_cell(self):
        assert add_stage("", "OA") == "OA"
        assert add_stage(None, "OA") == "OA"

    def test_is_idempotent(self):
        """A reprocessed email must not append a second copy."""
        assert add_stage("OA", "OA") == "OA"
        assert add_stage("OA; Phone", "Phone") == "OA; Phone"

    def test_accepts_any_casing(self):
        """The cell is hand-editable, so 'oa' typed by the user still counts."""
        assert add_stage("oa", "OA") == "OA"
        assert split_stages("oa ; TECHNICAL") == ["OA", "Technical"]

    def test_stores_in_pipeline_order(self):
        """Reads the way the process runs, whichever email landed first."""
        assert add_stage("Technical", "OA") == "OA; Technical"
        assert add_stage("Phone; Technical", "OA") == "OA; Phone; Technical"

    def test_rejects_an_unknown_stage(self):
        with pytest.raises(ValueError):
            add_stage("", "Offer")

    def test_remove_is_the_inverse(self):
        assert remove_stage("OA; Phone", "Phone") == "OA"
        assert remove_stage("OA", "OA") == ""
        assert remove_stage("OA", "Phone") == "OA"

    def test_unknown_text_is_preserved(self):
        """Silently discarding something the user typed is worse than carrying it."""
        assert "Coffee chat" in split_stages("OA; Coffee chat")


class TestReachedStage:
    """
    A completion is attributed by whether the row *reached* the stage, never by where it
    sits now. Rows 261, 262, 315 and 317 are all Rejected with an OA Date; the
    assessments were taken and the outcome merely arrived afterwards.
    """

    def _job(self, **fields):
        base = {"status": "New", "oa_date": "", "phone_date": "", "tech_date": ""}
        base.update(fields)
        return SimpleNamespace(**base)

    def test_a_rejected_row_with_an_oa_date_still_counts(self):
        assert reached_stage(self._job(status="Rejected", oa_date="7/27/2026"), "OA")

    def test_sitting_at_the_stage_counts_before_the_date_is_written(self):
        assert reached_stage(self._job(status="OA"), "OA")

    def test_an_advanced_row_still_counts_for_the_earlier_stage(self):
        """Reaching Phone does not undo having sat the OA."""
        assert reached_stage(self._job(status="Phone", oa_date="7/27/2026"), "OA")

    def test_a_row_that_never_got_there_does_not_count(self):
        assert not reached_stage(self._job(status="Applied"), "OA")
        assert not reached_stage(self._job(status="Rejected"), "OA")

    def test_stages_are_independent(self):
        job = self._job(status="Rejected", oa_date="7/27/2026")
        assert reached_stage(job, "OA")
        assert not reached_stage(job, "Phone")
        assert not reached_stage(job, "Technical")

    def test_whitespace_is_not_a_date(self):
        assert not reached_stage(self._job(status="Rejected", oa_date="   "), "OA")


class TestOutstanding:
    """
    The to-do list asks "what is still owed", which the record alone cannot answer.
    Row 308 completed one Chicago assessment and was sent a second: W reads "OA" and an
    assessment is still outstanding.
    """

    def _job(self, row=1, status="OA", completed="", category="", oa_date=""):
        return SimpleNamespace(row_number=row, company="Acme", position="Intern",
                               status=status, completed_stages=completed,
                               last_event=category, season_year="Summer 2027",
                               oa_date=oa_date, phone_date="", tech_date="")

    def _split(self, jobs):
        progress = _stage_progress(jobs, struck=set(), target_season_year=None)
        completed = progress["OA"]["completed"]
        todo = [j.row_number for j in progress["OA"]["todo"]]
        # The old shape returned (done, todo); keep the call sites readable by deriving
        # a done-list from the rows that are not outstanding.
        done = [j.row_number for j in jobs
                if j.status == "OA" and j.row_number not in todo]
        return done, todo

    def test_a_second_invitation_makes_it_outstanding_again(self):
        """Row 308: completed one, sent another. The record says OA; work remains."""
        done, todo = self._split([self._job(308, completed="OA", category="oa")])
        assert todo == [308] and done == []

    def test_a_receipt_as_the_last_email_means_nothing_is_owed(self):
        done, todo = self._split([self._job(462, completed="OA", category="stage_done")])
        assert done == [462] and todo == []

    def test_never_completed_is_outstanding(self):
        done, todo = self._split([self._job(518, completed="", category="oa")])
        assert todo == [518]

    def test_falls_back_to_the_record_without_a_category(self):
        """Rows predating the column still resolve from Completed Stages alone."""
        done, _ = self._split([self._job(404, completed="OA", category="")])
        assert done == [404]

    def test_an_unrelated_last_email_does_not_create_work(self):
        """A phone invitation does not make the OA outstanding again."""
        done, todo = self._split([self._job(1, completed="OA", category="phone")])
        assert done == [1] and todo == []

    def test_a_rejected_row_owes_nothing_but_still_counts_as_completed(self):
        """Row 261: the assessment was taken; the rejection came afterwards."""
        progress = _stage_progress(
            [self._job(261, status="Rejected", completed="OA",
                       category="rejection", oa_date="7/27/2026")],
            struck=set(), target_season_year=None,
        )
        assert progress["OA"]["todo"] == []
        assert progress["OA"]["completed"] == 1
        assert progress["OA"]["total"] == 1

    def test_struck_rows_are_excluded_from_outstanding(self):
        done, todo = self._split([self._job(215, completed="", category="oa")])
        assert todo == [215]
        progress = _stage_progress([self._job(215, completed="", category="oa")],
                                   struck={215}, target_season_year=None)
        assert progress["OA"]["todo"] == []

    def test_the_counts_always_partition_the_total(self):
        """
        outstanding + completed == total, by construction. Row 308 finished one
        assessment and was sent another; it counts as outstanding only, because
        something is still owed there.
        """
        jobs = [
            self._job(308, completed="OA", category="oa", oa_date="7/24/2026"),
            self._job(462, completed="OA", category="stage_done", oa_date="8/6/2026"),
            self._job(518, completed="", category="oa"),
        ]
        oa = _stage_progress(jobs, struck=set(), target_season_year=None)["OA"]
        assert len(oa["todo"]) == 2          # 308 and 518
        assert oa["completed"] == 1          # 462
        assert oa["total"] == 3
        assert len(oa["todo"]) + oa["completed"] == oa["total"]

    def test_completed_is_not_the_same_as_assessments_sat(self):
        """
        The Discord figure means "nothing further owed". How many assessments were
        actually sat is a different question, answered by the record — and 308 is in it.
        """
        job = self._job(308, completed="OA", category="oa", oa_date="7/24/2026")
        oa = _stage_progress([job], struck=set(), target_season_year=None)["OA"]
        assert oa["completed"] == 0
        assert "OA" in split_stages(job.completed_stages)

    def test_total_uses_the_season_totals_rule(self):
        """
        The denominator must equal the stage's Season Totals line, so it is counted the
        same way: reached the stage, whatever the status is now.
        """
        jobs = [
            self._job(1, status="Rejected", oa_date="7/1/2026"),   # reached, closed
            self._job(2, status="OA"),                             # sitting at it
            self._job(3, status="Phone", oa_date="7/2/2026"),      # advanced past it
            self._job(4, status="Applied"),                        # never got there
        ]
        progress = _stage_progress(jobs, struck=set(), target_season_year=None)
        assert progress["OA"]["total"] == 3

    def test_other_seasons_are_excluded(self):
        job = self._job(1, status="OA")
        job.season_year = "Summer 2026"
        progress = _stage_progress([job], struck=set(), target_season_year="Summer 2027")
        assert progress["OA"]["total"] == 0


class TestClassification:
    @pytest.fixture
    def classifier(self):
        """
        Built without __init__ on purpose: _parse_response reads no config at all, and
        every config fixture here either needs a live TEST_GOOGLE_SHEET_ID (skipping the
        file) or is currently broken. These assertions are pure parsing.
        """
        return EmailClassifier.__new__(EmailClassifier)

    def _parse(self, classifier, **fields):
        payload = {"company_candidates": ["Susquehanna"], "confidence": 0.9}
        payload.update(fields)
        return classifier._parse_response(json.dumps(payload))

    def test_stage_done_carries_the_stage(self, classifier):
        result = self._parse(classifier, category="stage_done", stage_completed="OA")
        assert result.category == "stage_done"
        assert result.stage_completed == "OA"

    def test_stage_is_normalised(self, classifier):
        result = self._parse(classifier, category="stage_done", stage_completed="technical")
        assert result.stage_completed == "Technical"

    def test_stage_done_without_a_stage_is_demoted(self, classifier):
        """
        A completion that names no stage says nothing actionable. Demoting it here keeps
        the ambiguity out of the caller, which would otherwise have to guess.
        """
        result = self._parse(classifier, category="stage_done")
        assert result.category == "unknown"
        assert result.stage_completed is None

    def test_an_unknown_stage_is_demoted(self, classifier):
        result = self._parse(classifier, category="stage_done", stage_completed="Offer")
        assert result.category == "unknown"

    def test_other_categories_carry_no_stage(self, classifier):
        assert self._parse(classifier, category="oa").stage_completed is None

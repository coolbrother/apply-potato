"""
Tests for the AI eligibility pass and the ELIGIBILITY_MODE routing.

The pass exists because encoding a prose requirement into minimum/maximum bounds
repeatedly produced false rejections — PDT Partners x2 and Millennium x2, each needing
its own patch. It asks the concrete question instead. What it must never do is invent
a rejection, so most of what is tested here is the refusal path.

Usage:
    pytest tests/test_eligibility.py -v
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.config import (
    ELIGIBILITY_MODE_AI,
    ELIGIBILITY_MODE_CODE,
    ELIGIBILITY_MODE_SHADOW,
    ELIGIBILITY_MODES,
)
from src.eligibility import (
    EligibilityCheck,
    EligibilityJudge,
    EligibilityJudgment,
    EligibilityUnavailable,
    JUDGED_DIMENSIONS,
)
from src.eligibility_log import load_disagreements, record_disagreement, summarize
from src.filters import passes_hard_filters
from src.ai_extractor import ExtractedJob, ClassStandingRange


# The Millennium wording, which the bounds encoding read as graduates-only.
POSTING = (
    "What You Bring\n"
    "Graduating between December 2027 and July 2028\n"
    "Expected GPA of 3.5 or above\n"
    "Pursuing a Bachelor's or Master's degree in Computer Science, Mathematics, "
    "Physics, Engineering, or a related quantitative field\n"
)


@pytest.fixture
def judge():
    config = SimpleNamespace(
        prompts_dir=Path("prompts"),
        ai_provider="openai",
        openai_api_key="sk-test",
        openai_model="gpt-4o-mini",
        openai_max_tokens=None,
        gemini_api_key=None,
        gemini_model="gemini-2.0-flash",
        gemini_max_output_tokens=None,
        max_retries=2,
        retry_base_delay_seconds=0,
        # Mirrors the real UserProfile: majors is a list, not a `major` string.
        user=SimpleNamespace(
            class_standing="Junior", graduation_date="May 2028",
            majors=["Computer Science", "Mathematics"],
            work_authorization="US Citizen", degree_level="Bachelors",
        ),
    )
    return EligibilityJudge(config)


def _check(dimension="class_standing", passes=False, evidence="", reasoning="r"):
    return {"dimension": dimension, "passes": passes,
            "evidence": evidence, "reasoning": reasoning}


def _job(**kw):
    kw.setdefault("company", "Millennium")
    kw.setdefault("title", "Quant Dev Intern")
    kw.setdefault("job_type", "Internship")
    kw.setdefault("season_year", "Summer 2027")
    return ExtractedJob(**kw)


@pytest.fixture
def user():
    return SimpleNamespace(
        class_standing="Junior", graduation_date="May 2028",
        target_job_type="Internship", target_season_year="Summer 2027",
        work_authorization="US Citizen", majors=["Computer Science"],
    )


class TestPromptBuilding:
    """The prompt reads the real UserProfile shape, not a convenient stand-in."""

    def test_majors_list_is_rendered(self, judge):
        prompt = judge._build_prompt("posting text", judge.config.user)
        assert "Computer Science, Mathematics" in prompt
        assert "{major}" not in prompt and "{class_standing}" not in prompt
        assert "posting text" in prompt

    def test_missing_fields_do_not_crash(self, judge):
        bare = SimpleNamespace(class_standing=None)
        prompt = judge._build_prompt("posting text", bare)
        assert "(not specified" in prompt


# =============================================================================
# Evidence validation — the pass may not reject without quoting the posting
# =============================================================================

class TestEvidenceValidation:
    def test_rejection_quoting_the_posting_is_usable(self, judge):
        data = {"eligible": False, "blocking_reason": "needs a PhD",
                "checks": [_check(evidence="Pursuing a Bachelor's or Master's degree")]}
        result = judge._build_judgment(data, POSTING, "raw")

        assert result.usable is True
        assert result.eligible is False
        assert result.failing_check.evidence_verified is True

    def test_rejection_on_invented_evidence_still_stands(self, judge):
        """
        The quote guard is gone. It was there to stop a rejection resting on a sentence
        the posting never contained — but it never caught a real failure (gpt-4o-mini
        rejected 23 of 24 qualified postings while quoting them accurately), and its only
        remedy was handing the job to the code filters, which is the path being retired.
        """
        data = {"eligible": False,
                "checks": [_check(evidence="Must be a PhD candidate in astrophysics")]}
        result = judge._build_judgment(data, POSTING, "raw")

        assert result.usable is True
        assert result.eligible is False

    def test_bare_verdict_stands_without_checks(self, judge):
        """
        The prompt returns {"eligible", "reason"} and nothing else. A rejection used to
        be discarded when no check failed, which would now throw away every rejection
        the model makes — the opposite of letting it decide.
        """
        data = {"eligible": False, "reason": "The posting requires an MBA."}
        result = judge._build_judgment(data, POSTING, "raw")

        assert result.usable is True
        assert result.eligible is False
        assert "requires an MBA" in result.reason()

    def test_reason_is_read_as_the_blocking_reason(self, judge):
        """`reason` is the current prompt's field; `blocking_reason` the older one."""
        data = {"eligible": False, "reason": "Requires enrollment in Hong Kong."}
        assert judge._build_judgment(data, POSTING, "raw").blocking_reason == (
            "Requires enrollment in Hong Kong.")

    def test_checks_are_still_parsed_when_a_response_carries_them(self, judge):
        """The schema is no longer required, but a response using it is not discarded."""
        data = {"eligible": False, "blocking_reason": "needs a PhD",
                "checks": [_check(evidence="Pursuing a Bachelor's or Master's degree")]}
        result = judge._build_judgment(data, POSTING, "raw")

        assert result.usable is True
        assert result.discarded_reason is None
        assert result.failing_check.dimension == "class_standing"

    def test_whitespace_differences_still_verify(self, judge):
        """The scraped text wraps differently from the quote; only words should matter."""
        data = {"eligible": False, "checks": [
            _check(evidence="Pursuing   a Bachelor's\n or Master's    degree")]}
        assert judge._build_judgment(data, POSTING, "raw").usable is True

    def test_passing_verdict_needs_no_evidence(self, judge):
        """Only rejections have to prove themselves — a pass costs nothing if wrong."""
        data = {"eligible": True, "checks": [_check(passes=True, evidence="")]}
        result = judge._build_judgment(data, POSTING, "raw")

        assert result.usable is True and result.eligible is True

    def test_unjudged_dimensions_are_dropped(self, judge):
        """job_type stays in code; a check for it is ignored."""
        data = {"eligible": True, "checks": [
            _check(dimension="job_type", passes=True),
            _check(dimension="class_standing", passes=True),
        ]}
        result = judge._build_judgment(data, POSTING, "raw")

        assert [c.dimension for c in result.checks] == ["class_standing"]
        assert all(d in JUDGED_DIMENSIONS for d in (c.dimension for c in result.checks))

    def test_season_year_checks_are_kept(self, judge):
        """The pass owns season_year now, so its check must survive _build_judgment."""
        data = {"eligible": True, "checks": [
            _check(dimension="season_year", passes=True),
            _check(dimension="class_standing", passes=True),
        ]}
        result = judge._build_judgment(data, POSTING, "raw")

        assert [c.dimension for c in result.checks] == ["season_year", "class_standing"]


# =============================================================================
# Failure paths — every one must yield None, never a rejection
# =============================================================================

class TestFailsOpen:
    def test_empty_content_returns_none(self, judge):
        assert judge.judge("") is None
        assert judge.judge("   ") is None

    def test_api_error_returns_none(self, judge):
        with patch.object(judge, "_call_openai", side_effect=RuntimeError("boom")):
            assert judge.judge(POSTING) is None

    def test_unparseable_response_returns_none(self, judge):
        with patch.object(judge, "_call_openai", return_value="I think they qualify!"):
            assert judge.judge(POSTING) is None

    def test_empty_response_returns_none(self, judge):
        with patch.object(judge, "_call_openai", return_value=""):
            assert judge.judge(POSTING) is None

    @pytest.mark.parametrize("raw", [
        '{"eligible": true, "checks": []}',
        '```json\n{"eligible": true, "checks": []}\n```',
        'Here you go:\n{"eligible": true, "checks": []}',
    ])
    def test_response_shapes_that_should_parse(self, judge, raw):
        """Models fence their JSON or preface it; none of that should lose a verdict."""
        with patch.object(judge, "_call_openai", return_value=raw):
            result = judge.judge(POSTING)
        assert result is not None and result.eligible is True


# =============================================================================
# Mode routing in passes_hard_filters
# =============================================================================

class TestModeRouting:
    def _rejecting_judgment(self):
        return EligibilityJudgment(
            eligible=False,
            checks=[EligibilityCheck("class_standing", False, "Must be a PhD student", "no")],
            blocking_reason="requires a PhD",
        )

    def _passing_judgment(self):
        return EligibilityJudgment(eligible=True, checks=[])

    def test_code_mode_ignores_the_judgment(self, user):
        """The default must behave exactly as it did before the flag existed."""
        job = _job()
        passed, _, _ = passes_hard_filters(
            user, job, judgment=self._rejecting_judgment(), mode=ELIGIBILITY_MODE_CODE
        )
        assert passed is True

    def test_shadow_mode_ignores_the_judgment(self, user):
        """Shadow observes; filters.py still decides."""
        job = _job()
        passed, _, _ = passes_hard_filters(
            user, job, judgment=self._rejecting_judgment(), mode=ELIGIBILITY_MODE_SHADOW
        )
        assert passed is True

    def test_ai_mode_rejects_on_the_judgment(self, user):
        job = _job()
        passed, reason, category = passes_hard_filters(
            user, job, judgment=self._rejecting_judgment(), mode=ELIGIBILITY_MODE_AI
        )
        assert passed is False
        assert category == "class_standing"
        assert "requires a PhD" in reason

    def test_ai_mode_keeps_the_millennium_job(self, user):
        """
        The case that motivated all of this: bounds say Graduate, the AI says the
        posting invites Bachelor's students.
        """
        job = _job(class_standing_range=ClassStandingRange(minimum="Graduate", maximum=None),
                   class_standing_requirement="Graduating between December 2027 and July 2028")
        passed, _, _ = passes_hard_filters(
            user, job, judgment=self._passing_judgment(), mode=ELIGIBILITY_MODE_AI
        )
        assert passed is True

    def test_no_judgment_raises_rather_than_falling_back(self, user):
        """
        An outage is not a verdict, and it must not become one. Deferring to the code
        path would decide the job by the very checks the pass replaced — and since
        extraction no longer produces the normalized bounds those checks read, they
        would wave everything through on None.
        """
        job = _job(class_standing_range=ClassStandingRange(minimum="Graduate", maximum=None),
                   class_standing_requirement="graduate students only")
        with pytest.raises(EligibilityUnavailable):
            passes_hard_filters(user, job, judgment=None, mode=ELIGIBILITY_MODE_AI)

    def test_unusable_judgment_raises_rather_than_falling_back(self, user):
        bad = EligibilityJudgment(eligible=False, checks=[], usable=False,
                                  discarded_reason="whatever the reason")
        with pytest.raises(EligibilityUnavailable):
            passes_hard_filters(user, _job(), judgment=bad, mode=ELIGIBILITY_MODE_AI)

    def test_code_mode_is_unaffected_by_a_missing_judgment(self, user):
        """Explicitly choosing code still means code, judgment or no judgment."""
        job = _job(class_standing_range=ClassStandingRange(minimum="Graduate", maximum=None),
                   class_standing_requirement="graduate students only")
        passed, _, category = passes_hard_filters(
            user, job, judgment=None, mode=ELIGIBILITY_MODE_CODE
        )
        assert passed is False and category == "class_standing"

    def test_job_type_still_runs_in_ai_mode(self, user):
        """job_type stays code's job in every mode — controlled vocabulary, never wrong."""
        passed, _, category = passes_hard_filters(
            user, _job(job_type="Full-Time"),
            judgment=self._passing_judgment(), mode=ELIGIBILITY_MODE_AI,
        )
        assert passed is False and category == "job_type"

    def test_ai_pass_owns_season_year(self, user):
        """
        A cleared judgment settles season/year too — it is not re-checked in code.

        The PNC posting named no term at all; extraction manufactured 2026 out of an
        application window ("posted for two business days from 08/03/2026") and the
        re-check rejected a job the pass had already cleared. Re-checking a dimension the
        pass owns only lets the weaker input win, so the extracted field must not be able
        to override the judgment here.
        """
        from src.ai_extractor import SeasonYearParsed

        passed, _, _ = passes_hard_filters(
            user, _job(season_year=None,
                       season_year_parsed=SeasonYearParsed(season=None, years=[2026])),
            judgment=self._passing_judgment(), mode=ELIGIBILITY_MODE_AI,
        )
        assert passed is True

    def test_ai_mode_rejects_on_a_season_year_judgment(self, user):
        """Owning the dimension means it can still reject on it — from the page, not a field."""
        judgment = EligibilityJudgment(
            eligible=False,
            checks=[EligibilityCheck("season_year", False,
                                     "Summer 2026 Internship", "targets Summer 2027")],
            blocking_reason="posting is for Summer 2026",
        )
        passed, reason, category = passes_hard_filters(
            user, _job(), judgment=judgment, mode=ELIGIBILITY_MODE_AI,
        )
        assert passed is False
        assert category == "season_year"
        assert "Summer 2026" in reason

    def test_code_mode_still_enforces_season_year(self, user):
        """The default is unchanged: without the pass, the extracted field still decides."""
        from src.ai_extractor import SeasonYearParsed

        passed, _, category = passes_hard_filters(
            user, _job(season_year="Summer 2026",
                       season_year_parsed=SeasonYearParsed(season="Summer", years=[2026])),
            judgment=self._passing_judgment(), mode=ELIGIBILITY_MODE_CODE,
        )
        assert passed is False and category == "season_year"


# =============================================================================
# Disagreement log
# =============================================================================

class TestDisagreementLog:
    def _judgment(self, eligible=True):
        return EligibilityJudgment(
            eligible=eligible,
            checks=[EligibilityCheck("class_standing", eligible,
                                     "Pursuing a Bachelor's or Master's degree", "why")],
        )

    def test_records_and_reloads(self, tmp_path):
        written = record_disagreement(
            tmp_path, url="https://x.test/1", company="Millennium", title="Quant Dev",
            code_passed=False, code_reason="below minimum standing (Graduate)",
            code_category="class_standing", judgment=self._judgment(True),
        )
        entries = load_disagreements(tmp_path)

        assert written is True and len(entries) == 1
        assert entries[0]["code"]["passed"] is False
        assert entries[0]["ai"]["eligible"] is True
        # "code rejected, AI would have kept it" is the direction most worth reading,
        # and there the AI has no failing check — the evidence must survive anyway.
        assert "Bachelor's" in entries[0]["ai"]["evidence"]
        assert entries[0]["ai"]["checks"][0]["dimension"] == "class_standing"

    def test_every_check_is_kept_not_just_a_failing_one(self, tmp_path):
        judgment = EligibilityJudgment(eligible=True, checks=[
            EligibilityCheck("class_standing", True, "Pursuing a Bachelor's", "ok"),
            EligibilityCheck("graduation_timeline", True, "Graduating in 2028", "ok"),
        ])
        record_disagreement(
            tmp_path, url="https://x.test/2", company="M", title="T",
            code_passed=False, code_reason="r", code_category="c", judgment=judgment,
        )

        checks = load_disagreements(tmp_path)[0]["ai"]["checks"]
        assert [c["dimension"] for c in checks] == ["class_standing", "graduation_timeline"]

    def test_same_url_is_not_recorded_twice(self, tmp_path):
        for _ in range(3):
            record_disagreement(
                tmp_path, url="https://x.test/1?utm_source=a", company="M", title="T",
                code_passed=False, code_reason="r", code_category="c",
                judgment=self._judgment(),
            )
        assert len(load_disagreements(tmp_path)) == 1

    def test_summary_counts_each_direction(self, tmp_path):
        record_disagreement(tmp_path, url="https://x.test/keep", company="A", title="T",
                            code_passed=False, code_reason="r", code_category="c",
                            judgment=self._judgment(eligible=True))
        record_disagreement(tmp_path, url="https://x.test/reject", company="B", title="T",
                            code_passed=True, code_reason="r", code_category="none",
                            judgment=self._judgment(eligible=False))

        assert summarize(tmp_path) == {"total": 2, "ai_would_keep": 1, "ai_would_reject": 1}

    def test_missing_file_reads_as_empty(self, tmp_path):
        assert load_disagreements(tmp_path / "nope") == []

    def test_corrupt_file_reads_as_empty(self, tmp_path):
        (tmp_path / "eligibility_disagreements.json").write_text("{not json", encoding="utf-8")
        assert load_disagreements(tmp_path) == []

    def test_write_failure_never_raises(self, tmp_path):
        """Shadow mode is observation; a logging failure must not break the run."""
        with patch("src.eligibility_log.Path.write_text", side_effect=OSError("disk full")):
            assert record_disagreement(
                tmp_path, url="https://x.test/1", company="A", title="T",
                code_passed=True, code_reason="r", code_category="none",
                judgment=self._judgment(),
            ) is False


class TestModeConstants:
    def test_code_is_the_default_mode(self):
        assert ELIGIBILITY_MODES[0] == ELIGIBILITY_MODE_CODE

    def test_all_modes_are_distinct(self):
        assert len(set(ELIGIBILITY_MODES)) == len(ELIGIBILITY_MODES)

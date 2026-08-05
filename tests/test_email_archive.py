"""
Tests for archiving OA-and-later status emails into the per-job folder.

A row records that a stage happened and when; the email holds the assessment link,
the deadline and the instructions. save_status_email() copies it into the same folder
job_desc.py writes to, named to match the files already there.

Usage:
    pytest tests/test_email_archive.py -v
"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.email_archive import ARCHIVED_CATEGORIES, save_status_email, _html_to_text
from src.gmail import EmailMessage


def _email(
    when: datetime = datetime(2026, 8, 3, 17, 21, 19),
    subject: str = "Chicago Trading Company (CTC) invites you to a test at Codility",
    body_text: str = "Hi Shawn Zou, please complete the assessment linked below.",
    body_html: str = "",
    message_id: str = "19fc980bfe4c7758",
) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        subject=subject,
        sender="Codility",
        sender_email="robot@codility.com",
        date=when,
        body_text=body_text,
        body_html=body_html,
        category="Updates",
        account="sz684@cornell.edu",
    )


class TestFileName:
    def test_matches_the_job_desc_naming_convention(self, tmp_path):
        path = save_status_email(308, "Chicago Trading Company", "oa", _email(), tmp_path)

        assert path is not None
        assert path.name == "308_Chicago_Trading_Company_oa.08.03.2026.md"
        assert path.parent.name == "308_Chicago_Trading_Company"

    def test_date_comes_from_the_email_not_today(self, tmp_path):
        """A reprocessed email must keep the name it would have had originally."""
        path = save_status_email(
            308, "Chicago Trading Company", "oa",
            _email(when=datetime(2026, 7, 24, 16, 5, 0)), tmp_path,
        )

        assert path.name == "308_Chicago_Trading_Company_oa.07.24.2026.md"

    def test_company_is_sanitized_like_the_folder(self, tmp_path):
        path = save_status_email(412, "PDT Partners, LLC.", "phone", _email(), tmp_path)

        assert path.parent.name == "412_PDT_Partners_LLC"
        assert path.name.startswith("412_PDT_Partners_LLC_phone.")

    def test_same_day_repeat_does_not_overwrite(self, tmp_path):
        """Two assessment invites in one day is the multi-round case this exists for."""
        first = save_status_email(308, "CTC", "oa", _email(message_id="a"), tmp_path)
        second = save_status_email(308, "CTC", "oa", _email(message_id="b"), tmp_path)
        third = save_status_email(308, "CTC", "oa", _email(message_id="c"), tmp_path)

        assert first.name == "308_CTC_oa.08.03.2026.md"
        assert second.name == "308_CTC_oa.08.03.2026-2.md"
        assert third.name == "308_CTC_oa.08.03.2026-3.md"
        assert first.read_text(encoding="utf-8") != ""
        assert "a" in first.read_text(encoding="utf-8")
        assert "b" in second.read_text(encoding="utf-8")


class TestWhichCategories:
    @pytest.mark.parametrize("category", ["oa", "phone", "technical", "offer", "rejection"])
    def test_archived_stages_are_written(self, tmp_path, category):
        assert save_status_email(1, "Uber", category, _email(), tmp_path) is not None

    @pytest.mark.parametrize("category", ["confirmation", "unknown", ""])
    def test_other_categories_write_nothing(self, tmp_path, category):
        assert save_status_email(1, "Uber", category, _email(), tmp_path) is None
        assert list(tmp_path.iterdir()) == []

    def test_rejection_is_archived(self, tmp_path):
        """A rejection is a terminal outcome rather than a later stage, but its text
        is still worth keeping."""
        assert "rejection" in ARCHIVED_CATEGORIES


class TestContent:
    def test_body_is_copied_verbatim(self, tmp_path):
        body = "Complete by Aug 16.\n\nRegards,\nCTC"
        path = save_status_email(308, "CTC", "oa", _email(body_text=body), tmp_path)

        assert body in path.read_text(encoding="utf-8")

    def test_padded_blank_runs_are_collapsed(self, tmp_path):
        """
        The Codility invite ships thirty consecutive blank lines in its text part,
        which buries the content. Whitespace only — no word is altered.
        """
        body = "Codility" + "\n" * 30 + "Hi Shawn Zou,\n\n\n\nComplete the assessment."
        path = save_status_email(308, "CTC", "oa", _email(body_text=body), tmp_path)

        text = path.read_text(encoding="utf-8")
        assert "Codility\n\nHi Shawn Zou,\n\nComplete the assessment." in text
        assert "\n\n\n" not in text

    def test_trailing_whitespace_is_trimmed(self, tmp_path):
        path = save_status_email(
            308, "CTC", "oa", _email(body_text="Line one   \nLine two\t\n"), tmp_path
        )

        assert "Line one\nLine two" in path.read_text(encoding="utf-8")

    def test_header_carries_the_provenance(self, tmp_path):
        path = save_status_email(308, "CTC", "oa", _email(), tmp_path)
        text = path.read_text(encoding="utf-8")

        assert "**Row:** 308" in text
        assert "**Stage:** oa" in text
        assert "**From:** robot@codility.com" in text
        assert "**Received:** 2026-08-03 17:21:19" in text
        assert "**Account:** sz684@cornell.edu" in text
        assert "19fc980bfe4c7758" in text

    def test_html_is_used_when_there_is_no_text_part(self, tmp_path):
        """The Criteria assessment invite arrives with an empty text/plain part."""
        path = save_status_email(
            308, "CTC", "oa",
            _email(body_text="", body_html="<p>Take the <b>test</b> by Friday</p>"),
            tmp_path,
        )

        text = path.read_text(encoding="utf-8")
        assert "Take the test by Friday" in text
        assert "<p>" not in text

    def test_script_and_style_are_dropped(self, tmp_path):
        path = save_status_email(
            308, "CTC", "oa",
            _email(body_text="", body_html="<style>a{color:red}</style><p>Hello</p>"),
            tmp_path,
        )

        text = path.read_text(encoding="utf-8")
        assert "Hello" in text
        assert "color:red" not in text

    def test_empty_body_still_produces_a_file(self, tmp_path):
        path = save_status_email(308, "CTC", "oa", _email(body_text="", body_html=""), tmp_path)

        assert path is not None
        assert "no readable body" in path.read_text(encoding="utf-8")


class TestFolder:
    def test_folder_is_created_when_missing(self, tmp_path):
        """Only rows that had a job description saved have a folder already."""
        target = tmp_path / "999_Brand_New"
        assert not target.exists()

        save_status_email(999, "Brand New", "oa", _email(), tmp_path)

        assert target.is_dir()

    def test_existing_folder_is_reused(self, tmp_path):
        folder = tmp_path / "308_CTC"
        folder.mkdir()
        (folder / "308_CTC_job_desc.md").write_text("desc", encoding="utf-8")

        save_status_email(308, "CTC", "oa", _email(), tmp_path)

        names = sorted(p.name for p in folder.iterdir())
        assert names == ["308_CTC_job_desc.md", "308_CTC_oa.08.03.2026.md"]


class TestFailureIsNonFatal:
    def test_write_failure_returns_none_rather_than_raising(self, tmp_path):
        """The status update already landed; archiving must not undo that."""
        with patch("src.email_archive.Path.write_text", side_effect=OSError("disk full")):
            assert save_status_email(308, "CTC", "oa", _email(), tmp_path) is None

    def test_failure_is_logged(self, tmp_path, caplog):
        import logging

        with patch("src.email_archive.Path.mkdir", side_effect=OSError("read-only")):
            with caplog.at_level(logging.WARNING, logger="src.email_archive"):
                save_status_email(308, "CTC", "oa", _email(), tmp_path)

        assert "row 308" in caplog.text


class TestHtmlToText:
    def test_entities_are_decoded(self):
        assert _html_to_text("<p>A&nbsp;&amp;&nbsp;B</p>").replace(" ", " ").strip() == "A & B"

    def test_blank_input(self):
        assert _html_to_text("") == ""

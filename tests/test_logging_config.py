"""
Tests for log file rotation.

These exist because the Gmail checker's log went silent for six days while the checker
itself ran normally. Every component shared one file through a TimedRotatingFileHandler,
both daemons held it open, and rotation worked by renaming — which Windows refuses while
another process has the file open. The stdlib advances `rolloverAt` only after a
successful rename, so one failed midnight left the handler retrying the roll on every
record and discarding each one. Silence read as "the service is dead"; it was not.

Usage:
    pytest tests/test_logging_config.py -v
"""

import logging
from datetime import date, timedelta

import pytest

from src.logging_config import DateStampedFileHandler


def make_handler(tmp_path, prefix="gmail", day=None, backup_days=30):
    """A handler whose idea of 'today' can be moved without touching the system clock."""
    class Fixed(DateStampedFileHandler):
        current = day or date(2026, 8, 24)

        def _today(self):
            return self.current

    return Fixed(tmp_path, prefix, backup_days=backup_days)


def record(msg="hello"):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)


def names(tmp_path):
    return sorted(p.name for p in tmp_path.glob("*.log"))


class TestFileNaming:

    def test_writes_to_a_dated_file_for_its_component(self, tmp_path):
        handler = make_handler(tmp_path, "gmail")
        handler.emit(record("first line"))
        handler.close()

        assert names(tmp_path) == ["gmail-2026-08-24.log"]
        assert "first line" in (tmp_path / "gmail-2026-08-24.log").read_text(encoding="utf-8")

    def test_components_do_not_share_a_file(self, tmp_path):
        for prefix in ("gmail", "scrape"):
            h = make_handler(tmp_path, prefix)
            h.emit(record(f"from {prefix}"))
            h.close()

        assert names(tmp_path) == ["gmail-2026-08-24.log", "scrape-2026-08-24.log"]

    def test_appending_reuses_the_same_day_file(self, tmp_path):
        h1 = make_handler(tmp_path, "gmail")
        h1.emit(record("one"))
        h1.close()
        h2 = make_handler(tmp_path, "gmail")
        h2.emit(record("two"))
        h2.close()

        body = (tmp_path / "gmail-2026-08-24.log").read_text(encoding="utf-8")
        assert "one" in body and "two" in body


class TestDateSwitch:

    def test_a_new_day_opens_a_new_file(self, tmp_path):
        handler = make_handler(tmp_path, "gmail")
        handler.emit(record("tuesday"))

        handler.current = date(2026, 8, 25)
        handler.emit(record("wednesday"))
        handler.close()

        assert names(tmp_path) == ["gmail-2026-08-24.log", "gmail-2026-08-25.log"]
        assert "tuesday" in (tmp_path / "gmail-2026-08-24.log").read_text(encoding="utf-8")
        assert "wednesday" in (tmp_path / "gmail-2026-08-25.log").read_text(encoding="utf-8")

    def test_yesterdays_file_is_left_alone_not_renamed(self, tmp_path):
        """
        The whole point. Nothing is renamed, so no other process can block the switch.
        """
        handler = make_handler(tmp_path, "gmail")
        handler.emit(record("tuesday"))
        before = (tmp_path / "gmail-2026-08-24.log").read_text(encoding="utf-8")

        handler.current = date(2026, 8, 25)
        handler.emit(record("wednesday"))
        handler.close()

        assert (tmp_path / "gmail-2026-08-24.log").exists()
        assert (tmp_path / "gmail-2026-08-24.log").read_text(encoding="utf-8") == before

    def test_two_handlers_on_one_day_both_keep_writing_across_midnight(self, tmp_path):
        """Two processes, one file, a date change: neither may be silenced."""
        daemon = make_handler(tmp_path, "gmail")
        script = make_handler(tmp_path, "gmail")
        daemon.emit(record("daemon before"))
        script.emit(record("script before"))

        daemon.current = script.current = date(2026, 8, 25)
        daemon.emit(record("daemon after"))
        script.emit(record("script after"))
        daemon.close()
        script.close()

        new = (tmp_path / "gmail-2026-08-25.log").read_text(encoding="utf-8")
        assert "daemon after" in new
        assert "script after" in new


class TestFailureIsSurvivable:

    def test_a_failing_switch_costs_one_record_not_the_stream(self, tmp_path, monkeypatch):
        """
        The regression that mattered: after a failed roll the old handler closed its
        stream, never advanced its clock, and dropped every record from then on.
        """
        handler = make_handler(tmp_path, "gmail")
        handler.emit(record("before"))

        boom = {"n": 0}

        def explode(day):
            boom["n"] += 1
            raise OSError("cannot open the new file")

        monkeypatch.setattr(handler, "_switch_to", explode)
        monkeypatch.setattr(handler, "handleError", lambda rec: None)
        handler.current = date(2026, 8, 25)
        handler.emit(record("during the failure"))

        # Undo the sabotage: the handler must still be usable, not silenced.
        monkeypatch.undo()
        handler.emit(record("after"))
        handler.close()

        assert boom["n"] == 1
        body = (tmp_path / "gmail-2026-08-24.log").read_text(encoding="utf-8")
        assert "before" in body
        assert "during the failure" in body  # written to yesterday's file, not lost

    def test_an_undeletable_old_file_does_not_raise(self, tmp_path, monkeypatch):
        old = tmp_path / "gmail-2026-01-01.log"
        old.write_text("ancient", encoding="utf-8")

        def refuse(self):
            raise OSError("held open by another process")

        monkeypatch.setattr("pathlib.Path.unlink", refuse)
        handler = make_handler(tmp_path, "gmail")  # prunes during construction
        handler.emit(record("still working"))
        handler.close()

        assert old.exists()
        assert "still working" in (tmp_path / "gmail-2026-08-24.log").read_text(encoding="utf-8")


class TestRetention:

    def test_files_past_the_window_are_removed(self, tmp_path):
        today = date(2026, 8, 24)
        (tmp_path / f"gmail-{today - timedelta(days=40):%Y-%m-%d}.log").write_text("old", encoding="utf-8")
        (tmp_path / f"gmail-{today - timedelta(days=5):%Y-%m-%d}.log").write_text("recent", encoding="utf-8")

        handler = make_handler(tmp_path, "gmail", day=today, backup_days=30)
        handler.close()

        assert names(tmp_path) == ["gmail-2026-08-19.log", "gmail-2026-08-24.log"]

    def test_another_components_logs_are_not_touched(self, tmp_path):
        old_other = tmp_path / "scrape-2026-01-01.log"
        old_other.write_text("not mine to delete", encoding="utf-8")

        handler = make_handler(tmp_path, "gmail")
        handler.close()

        assert old_other.exists()

    def test_files_that_are_not_ours_are_ignored(self, tmp_path):
        stray = tmp_path / "gmail-notadate.log"
        stray.write_text("hand-renamed", encoding="utf-8")

        handler = make_handler(tmp_path, "gmail")
        handler.close()

        assert stray.exists()

    def test_retention_off_keeps_everything(self, tmp_path):
        ancient = tmp_path / "gmail-2020-01-01.log"
        ancient.write_text("keep me", encoding="utf-8")

        handler = make_handler(tmp_path, "gmail", backup_days=0)
        handler.close()

        assert ancient.exists()

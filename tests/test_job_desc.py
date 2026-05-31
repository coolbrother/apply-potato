"""
Tests for Phase 1 job description saving (src/job_desc.py).

Covers:
- _sanitize: name sanitization
- _fallback_markdown: structure of the fallback formatter
- save_job_description: folder/file creation with mocked Claude CLI
- commit_and_push_job_folder: git operations on a temp repo
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.ai_extractor import ExtractedJob
from src.job_desc import (
    _fallback_markdown,
    _sanitize,
    commit_and_push_job_folder,
    save_job_description,
)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_job() -> ExtractedJob:
    return ExtractedJob(
        company="Acme Corp",
        title="Software Engineering Intern",
        job_type="Internship",
        locations=["San Francisco, CA"],
        is_remote=False,
        work_model="Hybrid",
        salary_min=40,
        salary_max=50,
        salary_period="hourly",
        currency="USD",
        required_skills=["Python", "SQL"],
        preferred_skills=["Docker"],
        required_majors=["Computer Science"],
        gpa_requirement=3.0,
        class_standing_requirement="Junior or Senior",
        work_authorization="US Citizen or Green Card",
        sponsorship_available=False,
        posted_date="2026-05-01",
        deadline="2026-06-01",
        season_year="Summer 2026",
        job_category="Software Engineering",
    )


RAW = "Apply now for this great role. Requirements: Python, SQL. Salary: $40-50/hr."


# ---------------------------------------------------------------------------
# _sanitize
# ---------------------------------------------------------------------------

class TestSanitize:

    def test_strips_special_chars(self):
        assert _sanitize("Acme, Inc.!") == "Acme_Inc"

    def test_replaces_spaces(self):
        assert _sanitize("Foo Bar Baz") == "Foo_Bar_Baz"

    def test_truncates(self):
        result = _sanitize("A" * 100, max_len=10)
        assert len(result) == 10

    def test_empty_string(self):
        assert _sanitize("") == ""


# ---------------------------------------------------------------------------
# _fallback_markdown
# ---------------------------------------------------------------------------

class TestFallbackMarkdown:

    def test_contains_title_and_company(self, sample_job):
        md = _fallback_markdown(sample_job, RAW)
        assert "Software Engineering Intern" in md
        assert "Acme Corp" in md

    def test_contains_required_skills(self, sample_job):
        md = _fallback_markdown(sample_job, RAW)
        assert "Python" in md
        assert "SQL" in md

    def test_contains_preferred_skills(self, sample_job):
        md = _fallback_markdown(sample_job, RAW)
        assert "Docker" in md

    def test_contains_raw_content(self, sample_job):
        md = _fallback_markdown(sample_job, RAW)
        assert "Apply now" in md

    def test_no_required_skills_section_when_empty(self, sample_job):
        sample_job.required_skills = []
        md = _fallback_markdown(sample_job, RAW)
        assert "## Required Skills" not in md


# ---------------------------------------------------------------------------
# save_job_description
# ---------------------------------------------------------------------------

class TestSaveJobDescription:

    def test_creates_folder_and_raw_file(self, tmp_path, sample_job):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="# Formatted\n\nContent", stderr="")
            save_job_description(42, "Acme Corp", RAW, sample_job, tmp_path)

        folder = tmp_path / "42_Acme_Corp"
        assert folder.is_dir()
        raw_file = folder / "42_Acme_Corp_raw.txt"
        assert raw_file.exists()
        assert raw_file.read_text(encoding="utf-8") == RAW

    def test_saves_formatted_markdown_from_claude(self, tmp_path, sample_job):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="# Formatted\n\nContent", stderr="")
            path = save_job_description(42, "Acme Corp", RAW, sample_job, tmp_path)

        assert path is not None
        assert path.name == "42_Acme_Corp_job_desc.md"
        assert "Formatted" in path.read_text(encoding="utf-8")

    def test_falls_back_when_claude_fails(self, tmp_path, sample_job):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            path = save_job_description(42, "Acme Corp", RAW, sample_job, tmp_path)

        assert path is not None
        content = path.read_text(encoding="utf-8")
        assert "Acme Corp" in content

    def test_falls_back_when_claude_not_found(self, tmp_path, sample_job):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            path = save_job_description(42, "Acme Corp", RAW, sample_job, tmp_path)

        assert path is not None

    def test_sanitizes_company_name_in_folder(self, tmp_path, sample_job):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="# content", stderr="")
            save_job_description(5, "Foo & Bar, Inc.", RAW, sample_job, tmp_path)

        # Should not have special chars in folder name
        folders = list(tmp_path.iterdir())
        assert len(folders) == 1
        assert "&" not in folders[0].name
        assert "," not in folders[0].name

    def test_returns_none_on_total_failure(self, tmp_path, sample_job):
        with patch("subprocess.run", side_effect=FileNotFoundError), \
             patch("src.job_desc._fallback_markdown", side_effect=Exception("boom")):
            path = save_job_description(1, "X", RAW, sample_job, tmp_path)
        assert path is None


# ---------------------------------------------------------------------------
# commit_and_push_job_folder
# ---------------------------------------------------------------------------

class TestCommitAndPush:

    def _make_git_repo(self, path: Path) -> Path:
        """Initialize a bare git repo for testing."""
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"],
                       check=True, capture_output=True)
        return path

    def test_skips_non_git_dir(self, tmp_path):
        folder = tmp_path / "job_folder"
        folder.mkdir()
        result = commit_and_push_job_folder(folder, tmp_path, "1_TestCo")
        assert result is False

    def test_commits_new_folder(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._make_git_repo(repo)

        folder = repo / "1_TestCo"
        folder.mkdir()
        (folder / "1_TestCo_job_desc.md").write_text("# Test Job")

        # No remote — commit should succeed, push will fail
        with patch("subprocess.run", wraps=subprocess.run) as mock_run:
            # Allow git commands except push
            original = subprocess.run

            def side_effect(cmd, **kwargs):
                if "push" in cmd:
                    raise subprocess.CalledProcessError(1, cmd, stderr=b"no remote")
                return original(cmd, **kwargs)

            mock_run.side_effect = side_effect
            result = commit_and_push_job_folder(folder, repo, "1_TestCo")

        assert result is False  # Push failed, but that's expected without remote

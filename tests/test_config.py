"""
Tests for src/config.py helpers.

    pytest tests/test_config.py -v
"""

import pytest

from src.config import load_config, openai_token_limit


class TestOpenAITokenLimit:
    """
    OpenAI renamed the completion cap for its newer families. Sending the old name is a
    hard 400, so switching OPENAI_MODEL to gpt-5.4-nano broke extraction, eligibility and
    email classification at once.
    """

    def test_older_families_keep_max_tokens(self):
        assert openai_token_limit("gpt-4o-mini", 500) == {"max_tokens": 500}
        assert openai_token_limit("gpt-4.1-mini", 900) == {"max_tokens": 900}

    def test_newer_families_use_max_completion_tokens(self):
        assert openai_token_limit("gpt-5.4-nano", 500) == {"max_completion_tokens": 500}
        assert openai_token_limit("gpt-5-mini", 500) == {"max_completion_tokens": 500}
        assert openai_token_limit("o3-mini", 500) == {"max_completion_tokens": 500}

    def test_no_cap_configured_sends_nothing(self):
        """An empty dict splats to nothing, leaving the model's own default in place."""
        assert openai_token_limit("gpt-5.4-nano", None) == {}
        assert openai_token_limit("gpt-4o-mini", 0) == {}

    def test_missing_model_name_does_not_raise(self):
        assert openai_token_limit(None, 500) == {"max_tokens": 500}


class TestPromotionsCheckSwitch:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path):
        for key, value in {
            "USER_EMAIL": "me@example.com", "GOOGLE_SHEET_ID": "sheet",
            "OPENAI_API_KEY": "sk-test", "AI_PROVIDER": "openai",
        }.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("PROMOTIONS_CHECK_ENABLED", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("", encoding="utf-8")
        return monkeypatch, env_file

    def test_on_by_default(self, env):
        _, env_file = env
        assert load_config(env_file).promotions_check_enabled is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no"])
    def test_turned_off(self, env, value):
        monkeypatch, env_file = env
        monkeypatch.setenv("PROMOTIONS_CHECK_ENABLED", value)
        assert load_config(env_file).promotions_check_enabled is False


class TestGitHubRepos:
    """
    GITHUB_REPOS is optional. It used to be required, so blanking it to stop scraping
    the repos would have exited every entry point - the Gmail checker included - since
    they all load the same config.
    """

    @pytest.fixture
    def env(self, monkeypatch, tmp_path):
        for key, value in {
            "USER_EMAIL": "me@example.com", "GOOGLE_SHEET_ID": "sheet",
            "OPENAI_API_KEY": "sk-test", "AI_PROVIDER": "openai",
        }.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("GITHUB_REPOS", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("", encoding="utf-8")
        return monkeypatch, env_file

    def test_unset_means_no_repos(self, env):
        _, env_file = env
        assert load_config(env_file).github_repos == []

    def test_blank_means_no_repos(self, env):
        monkeypatch, env_file = env
        monkeypatch.setenv("GITHUB_REPOS", "")
        assert load_config(env_file).github_repos == []

    def test_repos_and_branches_are_parsed(self, env):
        monkeypatch, env_file = env
        monkeypatch.setenv("GITHUB_REPOS", "owner/repo@dev, other/repo")
        repos = [(r.owner_repo, r.branch) for r in load_config(env_file).github_repos]
        assert repos == [("owner/repo", "dev"), ("other/repo", "main")]

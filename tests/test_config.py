"""
Tests for src/config.py helpers.

    pytest tests/test_config.py -v
"""

from src.config import openai_token_limit


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

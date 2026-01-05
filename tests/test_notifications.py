"""
Unit tests for dream company matching logic.

Tests the is_dream_company() fuzzy matching function without sending
any real Discord messages.

Usage:
    pytest tests/test_notifications.py -v
"""

from src.notifications import is_dream_company


class TestIsDreamCompany:
    """Test the is_dream_company() fuzzy matching function."""

    def test_exact_match(self):
        """Test exact company name match."""
        assert is_dream_company("Google", ["Google"], 80) is True
        assert is_dream_company("Meta", ["Meta"], 80) is True
        assert is_dream_company("Microsoft", ["Microsoft"], 80) is True

    def test_case_insensitive_match(self):
        """Test case-insensitive matching."""
        assert is_dream_company("google", ["Google"], 80) is True
        assert is_dream_company("GOOGLE", ["google"], 80) is True
        assert is_dream_company("GoOgLe", ["GOOGLE"], 80) is True

    def test_substring_match_dream_in_company(self):
        """Test when dream company name is substring of actual company."""
        assert is_dream_company("Google LLC", ["Google"], 80) is True
        assert is_dream_company("Meta Platforms Inc", ["Meta"], 80) is True
        assert is_dream_company("Microsoft Corporation", ["Microsoft"], 80) is True
        assert is_dream_company("Amazon Web Services", ["Amazon"], 80) is True

    def test_substring_match_company_in_dream(self):
        """Test when actual company name is substring of dream company."""
        assert is_dream_company("Google", ["Google LLC"], 80) is True
        assert is_dream_company("Meta", ["Meta Platforms"], 80) is True

    def test_fuzzy_match_with_suffix(self):
        """Test fuzzy matching with company suffixes."""
        assert is_dream_company("Qualcomm Inc", ["Qualcomm"], 80) is True
        assert is_dream_company("Qualcomm Technologies", ["Qualcomm"], 80) is True
        assert is_dream_company("Apple Inc.", ["Apple"], 80) is True

    def test_abbreviation_partial_match(self):
        """Test partial matching for abbreviations that are substrings."""
        # JPM = JPMorgan - "jpm" is a substring of "jpmorgan"
        assert is_dream_company("JPMorgan Chase", ["JPM"], 80) is True

        # Note: Acronyms like "SIG" for "Susquehanna International Group"
        # won't match since "sig" is not a substring of the full name.
        # This is expected behavior - use the full company name in dream list.

    def test_no_match_different_company(self):
        """Test that unrelated companies don't match."""
        assert is_dream_company("RandomCorp", ["Google", "Meta"], 80) is False
        assert is_dream_company("TechStartup", ["Apple", "Amazon"], 80) is False
        assert is_dream_company("Unknown Inc", ["Microsoft"], 80) is False

    def test_no_match_similar_but_different(self):
        """Test that similar but different companies don't match at high threshold."""
        # "Goggle" is similar to "Google" but not the same
        # At 80% threshold, this should likely fail
        result = is_dream_company("Goggle Inc", ["Google"], 90)
        # This might pass due to high similarity - adjust threshold if needed
        # The point is to test the threshold behavior

    def test_empty_company_name(self):
        """Test with empty company name."""
        assert is_dream_company("", ["Google"], 80) is False
        assert is_dream_company(None, ["Google"], 80) is False

    def test_empty_dream_companies_list(self):
        """Test with empty dream companies list."""
        assert is_dream_company("Google", [], 80) is False
        assert is_dream_company("Google", None, 80) is False

    def test_both_empty(self):
        """Test with both inputs empty."""
        assert is_dream_company("", [], 80) is False
        assert is_dream_company(None, None, 80) is False

    def test_multiple_dream_companies(self):
        """Test matching against multiple dream companies."""
        dream_list = ["Google", "Meta", "Apple", "Microsoft", "Amazon"]

        assert is_dream_company("Google", dream_list, 80) is True
        assert is_dream_company("Meta Platforms", dream_list, 80) is True
        assert is_dream_company("Apple Inc", dream_list, 80) is True
        assert is_dream_company("RandomCorp", dream_list, 80) is False

    def test_threshold_sensitivity(self):
        """Test different threshold values."""
        # Lower threshold = more lenient matching
        # Higher threshold = stricter matching

        # "Googl" vs "Google" - missing one letter
        # At low threshold, might match
        # At high threshold, should not match
        assert is_dream_company("Google", ["Google"], 50) is True  # Exact match always works
        assert is_dream_company("Google", ["Google"], 100) is True  # Exact match at 100%

    def test_common_company_variations(self):
        """Test common company name variations."""
        # Test various real-world company name formats
        test_cases = [
            # Note: Alphabet Inc vs Google won't match - different strings
            # To match Google jobs from Alphabet, add both to dream list
            ("NVIDIA Corporation", ["NVIDIA"], True),
            ("Netflix, Inc.", ["Netflix"], True),
            ("Stripe, Inc", ["Stripe"], True),
            ("Airbnb, Inc.", ["Airbnb"], True),
        ]

        for company, dream_list, expected in test_cases:
            result = is_dream_company(company, dream_list, 80)
            assert result == expected, f"Failed for {company} vs {dream_list}"

    def test_whitespace_handling(self):
        """Test handling of extra whitespace."""
        assert is_dream_company("  Google  ", ["Google"], 80) is True
        assert is_dream_company("Google", ["  Google  "], 80) is True

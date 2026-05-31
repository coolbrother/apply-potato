"""
Tests for dream company matching and salary threshold logic.

Usage:
    pytest tests/test_notifications.py -v
"""

import pytest

from src.notifications import is_dream_company, meets_salary_threshold


class TestIsDreamCompany:
    """Test is_dream_company() — exact match with legal suffix stripping."""

    # --- Name list matching ---

    def test_exact_match(self):
        assert is_dream_company("Google", ["Google"]) is True

    def test_case_insensitive(self):
        assert is_dream_company("google", ["Google"]) is True
        assert is_dream_company("GOOGLE", ["google"]) is True

    def test_strips_llc(self):
        assert is_dream_company("Google LLC", ["Google"]) is True

    def test_strips_inc(self):
        assert is_dream_company("Apple Inc", ["Apple"]) is True
        assert is_dream_company("Apple Inc.", ["Apple"]) is True

    def test_strips_corp(self):
        assert is_dream_company("Microsoft Corp", ["Microsoft"]) is True
        assert is_dream_company("Microsoft Corp.", ["Microsoft"]) is True

    def test_strips_ltd(self):
        assert is_dream_company("Acme Ltd", ["Acme"]) is True

    def test_strips_limited(self):
        assert is_dream_company("Acme Limited", ["Acme"]) is True

    def test_strips_technologies(self):
        assert is_dream_company("Qualcomm Technologies", ["Qualcomm"]) is True

    def test_strips_technology(self):
        assert is_dream_company("Qualcomm Technology", ["Qualcomm"]) is True

    def test_suffix_in_dream_list_entry(self):
        """Suffix stripping applies to both sides."""
        assert is_dream_company("Google", ["Google LLC"]) is True

    def test_no_match_different_company(self):
        assert is_dream_company("RandomCorp", ["Google", "Meta"]) is False

    def test_no_fuzzy_match(self):
        """Old fuzzy behavior must NOT apply — 'Goggle' should not match 'Google'."""
        assert is_dream_company("Goggle", ["Google"]) is False

    def test_no_substring_match(self):
        """Substring is no longer sufficient — must be exact after normalization."""
        assert is_dream_company("Applied Materials", ["Apple"]) is False
        assert is_dream_company("JPMorgan", ["JPM"]) is False

    def test_empty_company(self):
        assert is_dream_company("", ["Google"]) is False
        assert is_dream_company(None, ["Google"]) is False

    def test_empty_dream_list(self):
        assert is_dream_company("Google", []) is False

    def test_multiple_companies_in_list(self):
        dream_list = ["Google", "Meta", "Apple", "Microsoft"]
        assert is_dream_company("Meta", dream_list) is True
        assert is_dream_company("TechStartup", dream_list) is False

    @pytest.mark.parametrize("company,dream_list", [
        ("NVIDIA Corporation", ["NVIDIA"]),
        ("Netflix, Inc.", ["Netflix"]),
        ("Stripe, Inc", ["Stripe"]),
        ("Airbnb, Inc.", ["Airbnb"]),
        ("  Google  ", ["Google"]),
    ])
    def test_common_variations(self, company, dream_list):
        assert is_dream_company(company, dream_list) is True

    # --- Salary threshold matching (no name list) ---

    def test_salary_threshold_annual_match(self):
        assert is_dream_company(
            "SomeRandomCo", [],
            salary_min=120000, salary_max=150000, salary_period="yearly",
            min_salary_annual=100000,
        ) is True

    def test_salary_threshold_annual_miss(self):
        assert is_dream_company(
            "SomeRandomCo", [],
            salary_min=50000, salary_max=80000, salary_period="yearly",
            min_salary_annual=100000,
        ) is False

    def test_salary_threshold_hourly_match(self):
        assert is_dream_company(
            "SomeRandomCo", [],
            salary_min=55, salary_max=65, salary_period="hourly",
            min_salary_hourly=50,
        ) is True

    def test_salary_threshold_hourly_miss(self):
        assert is_dream_company(
            "SomeRandomCo", [],
            salary_min=30, salary_max=40, salary_period="hourly",
            min_salary_hourly=50,
        ) is False

    def test_no_threshold_configured_does_not_match(self):
        """Without thresholds set, salary alone should not make it a dream company."""
        assert is_dream_company(
            "SomeRandomCo", [],
            salary_min=500000, salary_max=500000, salary_period="yearly",
        ) is False

    def test_named_company_below_salary_threshold_still_matches(self):
        """A company in the user's list qualifies regardless of salary."""
        assert is_dream_company(
            "Google", ["Google"],
            salary_min=10000, salary_max=20000, salary_period="yearly",
            min_salary_annual=100000,
        ) is True

    def test_no_salary_info_does_not_match_on_threshold_alone(self):
        """No salary info → can't confirm threshold, so not a dream company."""
        assert is_dream_company(
            "SomeRandomCo", [],
            salary_min=None, salary_max=None, salary_period=None,
            min_salary_annual=100000,
        ) is False


class TestMeetsSalaryThreshold:
    """Test meets_salary_threshold() in isolation."""

    def test_no_threshold_configured(self):
        assert meets_salary_threshold(50000, 80000, "yearly", None, None) is False

    def test_no_salary_info(self):
        """No salary listed — can't confirm threshold met, so False."""
        assert meets_salary_threshold(None, None, None, 100000, 50) is False

    def test_no_period_assumes_yearly(self):
        """Unknown period treated as yearly."""
        assert meets_salary_threshold(None, 120000, None, 100000, None) is True
        assert meets_salary_threshold(None, 80000, None, 100000, None) is False

    def test_yearly_meets_annual_threshold(self):
        assert meets_salary_threshold(None, 120000, "yearly", 100000, None) is True

    def test_yearly_misses_annual_threshold(self):
        assert meets_salary_threshold(None, 80000, "yearly", 100000, None) is False

    def test_hourly_meets_hourly_threshold(self):
        assert meets_salary_threshold(None, 55, "hourly", None, 50) is True

    def test_hourly_misses_hourly_threshold(self):
        assert meets_salary_threshold(None, 40, "hourly", None, 50) is False

    def test_hourly_meets_annual_threshold(self):
        # $60/hr * 2080 = $124,800/yr > $100k
        assert meets_salary_threshold(None, 60, "hourly", 100000, None) is True

    def test_monthly_meets_annual_threshold(self):
        # $10,000/mo * 12 = $120,000/yr > $100k
        assert meets_salary_threshold(None, 10000, "monthly", 100000, None) is True

    def test_uses_salary_max_over_min(self):
        """Should check salary_max when available."""
        # min is below threshold, max is above
        assert meets_salary_threshold(80000, 120000, "yearly", 100000, None) is True

    def test_falls_back_to_salary_min(self):
        """Uses salary_min when max is None."""
        assert meets_salary_threshold(110000, None, "yearly", 100000, None) is True

    def test_either_threshold_sufficient(self):
        """Meeting either annual OR hourly threshold is enough."""
        # Below annual but above hourly
        assert meets_salary_threshold(None, 55, "hourly", 200000, 50) is True

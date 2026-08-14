"""
Tests for the three guards that stop one company's mail landing on another's row.

All three exist because of a single incident on 2026-08-10. A Maven Securities
assessment invite, sent by their vendor Arctic Shores, was written onto Akuna Capital
row 317 — a row already Rejected — and fired a dream-company Discord alert claiming an
Akuna OA. The classifier had pulled "UNA" out of the assessment host
`webassessment.una-arcticshores.com`, company lookup was bare containment so "una"
matched "akuna capital", and the terminal status did nothing to stop the write.

Usage:
    pytest tests/test_wrong_company_match.py -v
"""

from types import SimpleNamespace

from src.email_classifier import drop_hostname_candidates
from src.sheets import (
    STATUS_APPLIED,
    STATUS_GHOSTED,
    STATUS_OA,
    STATUS_OFFER,
    STATUS_REJECTED,
    as_acronym,
    company_acronyms,
    company_matches,
)


# =============================================================================
# company_matches — alphanumeric-boundary containment
# =============================================================================

class TestCompanyMatches:
    """The lookup rule itself."""

    def test_the_akuna_collision(self):
        """The exact match that misrouted row 317."""
        assert not company_matches("UNA", "Akuna Capital")

    def test_hostname_candidate_does_not_match_either(self):
        assert not company_matches("una-arcticshores.com", "Akuna Capital")

    def test_exact_name_matches(self):
        assert company_matches("Akuna Capital", "Akuna Capital")

    def test_case_insensitive(self):
        assert company_matches("akuna capital", "AKUNA CAPITAL")

    def test_bare_brand_matches_full_name(self):
        """An email says "Goldman", the sheet says "Goldman Sachs"."""
        assert company_matches("Goldman", "Goldman Sachs")

    def test_full_name_does_not_match_bare_brand(self):
        """Containment is one-directional; this was true before and stays true."""
        assert not company_matches("Goldman Sachs", "Goldman")

    def test_trailing_word_boundary(self):
        assert company_matches("Capital", "Akuna Capital")

    def test_short_real_companies_still_match(self):
        """Length is not the rule — DRW, IMC and SIG are three letters and real."""
        assert company_matches("DRW", "DRW Trading")
        assert company_matches("IMC", "IMC Trading")
        assert company_matches("SIG", "SIG Susquehanna")

    def test_short_name_inside_a_word_does_not(self):
        assert not company_matches("IMC", "IMCO Holdings")

    def test_hyphen_is_a_boundary(self):
        assert company_matches("Packard", "Hewlett-Packard")

    def test_dot_is_a_boundary(self):
        assert company_matches("Amazon", "Amazon.com")

    def test_ampersand_name(self):
        assert company_matches("AT&T", "AT&T Inc")

    def test_needle_ending_in_punctuation(self):
        r"""The reason for lookarounds rather than \b — "yahoo!" ends in a non-word char."""
        assert company_matches("Yahoo!", "Yahoo! Inc")

    def test_parenthesised_name(self):
        assert company_matches("SIG", "Susquehanna (SIG)")

    def test_concatenated_name_matches_on_its_prefix(self):
        """Row 453 is spelled "JPMorganChase"; a bare "JPMorgan" has to reach it."""
        assert company_matches("JPMorgan", "JPMorganChase")
        assert company_matches("Field", "FieldAI")

    def test_leading_camel_case_is_not_a_boundary(self):
        """Brand names lead, so a trailing fragment is far likelier to be noise."""
        assert not company_matches("Scale", "TribalScale")
        assert not company_matches("Dance", "ByteDance")

    def test_camel_rule_does_not_reopen_the_akuna_bug(self):
        """It reads the case of the sheet's name, and "Akuna" has no transition."""
        assert not company_matches("UNA", "Akuna Capital")
        assert not company_matches("una", "AkunaCapital")

    def test_all_caps_run_is_not_a_transition(self):
        assert not company_matches("Ultra", "ULTRAtech")

    def test_digits_are_boundaries_too(self):
        assert not company_matches("Two", "Two22 Capital")

    def test_empty_needle_matches_nothing(self):
        """Containment made "" match every row in the sheet."""
        assert not company_matches("", "Akuna Capital")

    def test_whitespace_needle_matches_nothing(self):
        assert not company_matches("   ", "Akuna Capital")

    def test_none_is_tolerated(self):
        assert not company_matches(None, "Akuna Capital")
        assert not company_matches("Akuna", None)

    def test_regex_metacharacters_are_literal(self):
        """A name is a name, not a pattern."""
        assert not company_matches("A.una", "Akuna Capital")
        assert company_matches("C++ Labs", "C++ Labs")

    def test_needle_surrounded_by_spaces_mid_name(self):
        assert company_matches("Morgan", "JP Morgan Chase")


# =============================================================================
# company_matches — acronyms
#
# Row 631 was scraped as "International Business Machines Corporation" and IBM's mail
# says IBM, so a confirmation and an OA both passed the row by and left it New.
# =============================================================================

class TestAcronymMatching:
    """One side abbreviating the other, in either direction."""

    def test_the_ibm_case(self):
        assert company_matches("IBM", "International Business Machines Corporation")

    def test_the_needle_may_carry_a_suffix_too(self):
        """A signature block gives the classifier "IBM Corporation" as readily as "IBM"."""
        assert company_matches("IBM Corporation", "International Business Machines Corporation")
        assert company_matches("IBM, Inc.", "International Business Machines Corporation")

    def test_legal_suffix_is_optional(self):
        """Quoted with or without the "Corporation", it is the same company."""
        assert company_matches("IBMC", "International Business Machines Corporation")
        assert company_matches("TFI", "Tyson Foods, Inc.")

    def test_other_real_abbreviations_on_the_sheet(self):
        assert company_matches("HRT", "Hudson River Trading")
        assert company_matches("CTC", "Chicago Trading Company")
        assert company_matches("BOA", "Bank of America")
        assert company_matches("TTD", "The Trade Desk")
        assert company_matches("NFCU", "Navy Federal Credit Union")

    def test_the_sheet_may_hold_the_acronym_instead(self):
        """Direction reversed: row says SEL, the email writes the name out."""
        assert company_matches("Schweitzer Engineering Laboratories", "SEL")

    def test_ampersand_is_dropped_not_counted(self):
        """SBD, not SB&D — and punctuation in the needle is stripped, so both arrive."""
        assert company_matches("SBD", "Stanley Black & Decker")
        assert company_matches("SB&D", "Stanley Black & Decker")

    def test_group_is_part_of_the_name(self):
        """Suffix peeling stops at the first non-suffix word, so JTG survives."""
        assert company_matches("JTG", "Jump Trading Group")

    def test_two_letter_acronyms_are_refused(self):
        """
        The collision-dense end of the range. On the sheet as it stands MS is Maven
        Securities, Morgan Stanley and Motorola Solutions at once.
        """
        assert not company_matches("MS", "Morgan Stanley")
        assert not company_matches("MS", "Maven Securities")
        assert not company_matches("CS", "Citadel Securities")
        assert not company_matches("GE", "Garff Enterprises")

    def test_two_letters_would_have_cost_a_correct_match(self):
        """
        "Western Digital" identifies row 637 today and must keep doing so; at two
        characters it would also claim the six rows filed under "WD" and go ambiguous.
        """
        assert not company_matches("Western Digital", "WD")

    def test_title_case_is_a_name_not_an_acronym(self):
        """
        Only an all-caps token reads as an acronym, so an ordinary word cannot claim a
        row by spelling out its initials.
        """
        assert not company_matches("Sun", "Stanford University Network")
        assert not company_matches("Ibm", "International Business Machines Corporation")

    def test_the_akuna_bug_stays_shut(self):
        assert not company_matches("UNA", "Akuna Capital")

    def test_initials_of_an_unrelated_company_do_not_match(self):
        assert not company_matches("HRT", "Hudson Bay Capital")
        assert not company_matches("IBM", "Intel")

    def test_single_word_company_has_no_acronym(self):
        assert company_acronyms("Stripe") == set()
        assert not company_matches("STR", "Stripe")

    def test_acronym_set(self):
        assert company_acronyms("International Business Machines Corporation") == {
            "IBM", "IBMC",
        }
        assert company_acronyms("Two Sigma Investments, LP") == {"TSI", "TSIL"}

    def test_as_acronym_recognises_only_acronyms(self):
        assert as_acronym("IBM") == "IBM"
        assert as_acronym("I.B.M.") == "IBM"
        assert as_acronym("IBM Corporation") == "IBM"
        assert as_acronym("WD Inc") is None  # still too short to identify anyone
        assert as_acronym("WD") is None
        assert as_acronym("Ibm") is None
        assert as_acronym("RTX9") is None
        assert as_acronym("") is None
        assert as_acronym(None) is None


# =============================================================================
# drop_hostname_candidates — keep vendor domains out of the candidate list
# =============================================================================

class TestDropHostnameCandidates:

    def test_drops_the_vendor_host(self):
        candidates = ["Maven", "Maven Securities", "una-arcticshores.com"]
        assert drop_hostname_candidates(candidates) == ["Maven", "Maven Securities"]

    def test_drops_a_full_url(self):
        candidates = ["Stripe", "https://boards.greenhouse.io/stripe"]
        assert drop_hostname_candidates(candidates) == ["Stripe"]

    def test_a_dotted_name_goes_when_a_bare_variant_remains(self):
        """Amazon.com is a host by shape. Losing it costs nothing while "Amazon" stands."""
        assert drop_hostname_candidates(["Amazon", "Amazon.com"]) == ["Amazon"]

    def test_keeps_everything_when_filtering_would_empty_the_list(self):
        """No candidates means no match at all, which is worse than a weak candidate."""
        candidates = ["Booking.com"]
        assert drop_hostname_candidates(candidates) == ["Booking.com"]

    def test_ordinary_names_are_untouched(self):
        candidates = ["Akuna Capital", "Akuna", "DRW"]
        assert drop_hostname_candidates(candidates) == candidates

    def test_multiword_name_is_never_a_host(self):
        """A host has no spaces, so "St. Jude Medical" is safe."""
        assert drop_hostname_candidates(["St. Jude Medical"]) == ["St. Jude Medical"]

    def test_empty_list(self):
        assert drop_hostname_candidates([]) == []

    def test_tolerates_blank_entries(self):
        assert drop_hostname_candidates(["Maven", ""]) == ["Maven", ""]


# =============================================================================
# Terminal statuses as a floor
# =============================================================================

def is_regression_call(current, new):
    """
    Call the guard unbound, so no Gmail client, Sheets client or classifier is built.

    The method reads nothing off self, so a bare namespace is a sufficient receiver.
    """
    from check_gmail import GmailChecker

    return GmailChecker._is_status_regression(SimpleNamespace(), current, new)


class TestTerminalStatusFloor:

    def test_rejected_row_refuses_an_oa(self):
        """Row 317: this is the write that should never have happened."""
        assert is_regression_call(STATUS_REJECTED, STATUS_OA)

    def test_rejected_row_refuses_an_offer(self):
        assert is_regression_call(STATUS_REJECTED, STATUS_OFFER)

    def test_rejected_row_refuses_a_confirmation(self):
        assert is_regression_call(STATUS_REJECTED, STATUS_APPLIED)

    def test_ghosted_row_refuses_a_stage(self):
        assert is_regression_call(STATUS_GHOSTED, STATUS_OA)

    def test_rejection_still_lands_on_a_ghosted_row(self):
        """Terminal to terminal is not a regression — the outcome is now known."""
        assert not is_regression_call(STATUS_GHOSTED, STATUS_REJECTED)

    def test_rejection_still_lands_on_an_oa_row(self):
        assert not is_regression_call(STATUS_OA, STATUS_REJECTED)

    def test_whitespace_around_a_terminal_status(self):
        assert is_regression_call("  Rejected  ", STATUS_OA)

    def test_the_original_ladder_still_holds(self):
        """The Castleton case: a confirmation must not reset a row already at OA."""
        assert is_regression_call(STATUS_OA, STATUS_APPLIED)
        assert is_regression_call(STATUS_OA, STATUS_OA)
        assert not is_regression_call(STATUS_APPLIED, STATUS_OA)

    def test_unknown_current_status_still_falls_through(self):
        assert not is_regression_call("Withdrawn", STATUS_OA)

    def test_blank_current_status_still_falls_through(self):
        assert not is_regression_call("", STATUS_OA)
        assert not is_regression_call(None, STATUS_OA)

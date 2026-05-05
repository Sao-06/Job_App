"""Tests for the standalone _extract_name_from_text / _extract_location_from_text."""
import pytest

from pipeline.providers import _extract_location_from_text, _extract_name_from_text

pytestmark = pytest.mark.unit


# ── Name extraction ─────────────────────────────────────────────────────────


class TestExtractName:
    def test_first_line_simple_two_words(self):
        text = "Jane Smith\nemail@x.com\n"
        assert _extract_name_from_text(text) == "Jane Smith"

    def test_three_word_name(self):
        text = "Jane Marie Smith\nemail@x.com\n"
        assert _extract_name_from_text(text) == "Jane Marie Smith"

    def test_irish_apostrophe_name(self):
        text = "Sean O'Brien\n123 Main St\n"
        assert _extract_name_from_text(text) == "Sean O'Brien"

    def test_hyphenated_name(self):
        text = "Jean-Paul Marie\nemail@x.com\n"
        assert _extract_name_from_text(text) == "Jean-Paul Marie"

    def test_all_caps_name(self):
        text = "JOHN DOE\nemail@x.com\n"
        assert _extract_name_from_text(text) == "JOHN DOE"

    def test_skips_section_header(self):
        text = "EDUCATION\nMIT 2024\n"
        # Must not return "EDUCATION".
        assert _extract_name_from_text(text) != "EDUCATION"

    def test_skips_email_line(self):
        text = "alice@example.com\nAlice Wonder\nBerkeley CA\n"
        # First line has @, so heuristic moves on; should find "Alice Wonder".
        assert _extract_name_from_text(text) == "Alice Wonder"

    def test_skips_phone_line(self):
        text = "(415) 555-1234\nJane Smith\nBerkeley CA\n"
        assert _extract_name_from_text(text) == "Jane Smith"

    def test_falls_back_to_placeholder(self):
        # Resume with no recognizable name → placeholder returns OWNER_NAME from config.
        from pipeline.config import OWNER_NAME
        text = "EDUCATION\nSKILLS\nEXPERIENCE\n"
        assert _extract_name_from_text(text) == OWNER_NAME

    def test_handles_empty_input(self):
        from pipeline.config import OWNER_NAME
        assert _extract_name_from_text("") == OWNER_NAME

    def test_skips_long_lines(self):
        # First line 60+ chars is not a name.
        text = "This is definitely a long objective sentence that runs over the cap.\nJane Smith\n"
        assert _extract_name_from_text(text) == "Jane Smith"

    def test_accepts_unicode_accented_name(self):
        text = "José López\nemail@x.com\n"
        assert _extract_name_from_text(text) == "José López"


# ── Location extraction ─────────────────────────────────────────────────────


class TestExtractLocation:
    def test_city_state_pipe_separator(self):
        text = "Jane Smith\nBerkeley, CA  |  email@x.com  |  (415) 555-0199\n"
        out = _extract_location_from_text(text)
        assert "Berkeley" in out and "CA" in out

    def test_full_state_name(self):
        text = "Jane Smith\nLocation: Boston, Massachusetts\nemail@x.com\n"
        out = _extract_location_from_text(text)
        assert "Boston" in out

    def test_returns_state_name_when_only_state(self):
        text = "Jane Smith\nemail@x.com\nCalifornia is great\n"
        # Standalone state hit.
        out = _extract_location_from_text(text)
        assert out == "California"

    def test_empty_when_no_location(self):
        text = "Jane Smith\nemail@x.com\nSomewhere ambiguous\n"
        # No identifiable location → empty (NOT a hardcoded default).
        out = _extract_location_from_text(text)
        # Either empty or contains exactly the resolved hint — never a stub.
        assert out == "" or "Somewhere" in out or "ambiguous" in out

    def test_handles_blank_input(self):
        assert _extract_location_from_text("") == ""

    def test_international_city_country(self):
        text = "Jane Smith\nLocation: Toronto, Canada\nemail@x.com\n"
        out = _extract_location_from_text(text)
        # Either captures the international form, or finds nothing — both
        # are acceptable as long as we don't fabricate a US fallback.
        assert "Toronto" in out or out == ""

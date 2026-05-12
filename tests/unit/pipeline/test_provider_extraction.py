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

    def test_returns_empty_when_unrecognised(self):
        # Resume with no recognisable name → "" so the upstream merger
        # treats the field as unset rather than masking a real failure
        # with the OWNER_NAME placeholder ("Your Name").
        text = "EDUCATION\nSKILLS\nEXPERIENCE\n"
        assert _extract_name_from_text(text) == ""

    def test_handles_empty_input(self):
        assert _extract_name_from_text("") == ""

    def test_rejects_role_title_line(self):
        # A line that looks like a job title (contains "Engineer", "Manager",
        # etc.) must NOT be picked as a person name.
        text = "Software Engineer\nJane Smith\nemail@example.com\n"
        assert _extract_name_from_text(text) == "Jane Smith"

    def test_rejects_institution_line(self):
        # University / company names must not be picked as a person name.
        text = "Stanford University\nJane Smith\nemail@example.com\n"
        assert _extract_name_from_text(text) == "Jane Smith"

    def test_skips_long_lines(self):
        # First line 60+ chars is not a name.
        text = "This is definitely a long objective sentence that runs over the cap.\nJane Smith\n"
        assert _extract_name_from_text(text) == "Jane Smith"

    def test_accepts_unicode_accented_name(self):
        text = "José López\nemail@x.com\n"
        assert _extract_name_from_text(text) == "José López"

    def test_rejects_city_in_contact_block_sidebar_layout(self):
        # Regression for the Colin Tse CV bug: PDF text-extraction of a
        # sidebar-layout resume reads the left-column EDUCATION/CONTACT/SKILLS
        # blocks before the right-column header where the actual name lives.
        # "Hong Kong" (line ~21 of the extracted text) used to win as a 2-word
        # title-case candidate. After the fix the heuristic must return ""
        # so the LLM merge fills in the real name.
        text = (
            "EDUCATION\nPROFILE\n"
            "Some long profile paragraph that is more than fifty-five characters "
            "long so it is rejected by the line-length cap.\n"
            "CONTACT\n+44 7365 911772\n+852 5182 9335\n"
            "alice@example.com\n22329200@bucks.ac.uk\n"
            "Hong Kong\nHigh Wycombe, Buckinghamshire, UK\n"
            "SKILLS\nProject Management\nGuest Relations\n"
        )
        out = _extract_name_from_text(text)
        assert out != "Hong Kong", "city must not win as a person name"
        assert out != "Project Management", "skill phrase must not win as a person name"
        assert out != "Guest Relations", "skill phrase must not win as a person name"
        # "" is acceptable — the upstream merge with the LLM result will fill it.
        assert out == ""

    def test_rejects_standalone_city_blacklist(self):
        # Even with no surrounding section markers, well-known city/country
        # names that perfectly fit the 2-word title-case pattern must not
        # be returned as the candidate's name.
        for blacklisted in ("Hong Kong", "New York", "Los Angeles",
                            "Mexico City", "Tel Aviv", "Cape Town"):
            text = f"{blacklisted}\nemail@example.com\n"
            assert _extract_name_from_text(text) != blacklisted


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

    def test_returns_state_name_only_on_contact_line(self):
        # Bare state names without a contact-line signal (email/phone/pipe)
        # should NOT match — that line might just be experience prose, not
        # the candidate's residence.
        non_contact_text = "Jane Smith\nemail@x.com\nCalifornia is great\n"
        assert _extract_location_from_text(non_contact_text) == ""

        # State on a line that looks like contact info IS picked up.
        contact_text = "Jane Smith\nemail@x.com | California | (415) 555-0199\n"
        assert _extract_location_from_text(contact_text) == "California"

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

    def test_standalone_city_in_sidebar_contact_block(self):
        # Sidebar-layout resumes (Colin Tse's CV) push the contact block
        # well past the first 12 lines. After widening the window to 30 the
        # international city pattern should still find a usable location.
        text = (
            "EDUCATION\nPROFILE\n"
            "A long profile paragraph that easily exceeds the line-length cap "
            "so it does not get matched as a header line itself.\n"
            "CONTACT\n+44 7365 911772\n+852 5182 9335\n"
            "alice@example.com\n22329200@bucks.ac.uk\n"
            "Hong Kong\nHigh Wycombe, Buckinghamshire, UK\nSKILLS\n"
        )
        out = _extract_location_from_text(text)
        # Either the standalone "Hong Kong" or "Buckinghamshire, UK" is
        # acceptable — both are sidebar contact-block locations and
        # legitimate answers; the bug being fixed is "" → real value.
        assert out != "", "sidebar-layout location must no longer be empty"
        assert "Hong Kong" in out or "UK" in out or "Buckinghamshire" in out

    def test_standalone_metropolis_requires_contact_neighborhood(self):
        # A bare "Hong Kong" line surrounded by experience prose, with no
        # contact signal nearby, must NOT be picked as the residence.
        text = (
            "Jane Smith\nSenior Engineer\n"
            "Worked closely with the Hong Kong office\n"
            "Built distributed systems\n"
        )
        assert _extract_location_from_text(text) == ""

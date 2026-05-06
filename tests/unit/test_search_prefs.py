"""Tests for _infer_experience_levels_from_profile and the
force_refresh path of _apply_profile_search_prefs in app.py.

These cover the regression where uploading a NEW primary resume left the
job-search-pref chips (location / experience_levels / education_filter)
pinned to the OLD resume's values, leading to job postings mis-aimed at
the previous persona.
"""
import pytest

from app import (
    _apply_profile_search_prefs,
    _infer_experience_levels_from_profile,
)

pytestmark = pytest.mark.unit


# ── Experience-level inference ─────────────────────────────────────────────


class TestInferExperienceLevels:
    def test_handles_empty_profile(self):
        assert _infer_experience_levels_from_profile({}) == []
        assert _infer_experience_levels_from_profile(None) == []

    def test_student_summary_wins_over_chief_title(self):
        # Regression: Colin Tse held "Chief of Operation" at a student
        # film club. His summary said "fresh graduating student". The
        # student signal MUST beat the senior keyword in the title.
        profile = {
            "summary": "I am a fresh graduating student currently studying at university.",
            "experience": [
                {"title": "Chief of Operation", "company": "QMersTV", "dates": "2020-2023"},
            ],
        }
        assert _infer_experience_levels_from_profile(profile) == ["internship", "entry-level"]

    def test_currently_studying_phrase(self):
        profile = {
            "summary": "Computer-science undergrad, currently studying at MIT.",
            "experience": [],
        }
        assert _infer_experience_levels_from_profile(profile) == ["internship", "entry-level"]

    def test_intern_in_title_without_summary_signal(self):
        profile = {
            "summary": "Building distributed systems.",
            "experience": [
                {"title": "Software Engineering Intern", "company": "Acme", "dates": "2024"},
            ],
        }
        assert _infer_experience_levels_from_profile(profile) == ["internship", "entry-level"]

    def test_senior_title_returns_senior(self):
        profile = {
            "summary": "Engineer.",
            "experience": [
                {"title": "Senior Software Engineer", "company": "Acme", "dates": "2019-Present"},
            ],
        }
        assert _infer_experience_levels_from_profile(profile) == ["senior"]

    def test_principal_title_returns_senior(self):
        profile = {
            "experience": [
                {"title": "Principal Engineer", "company": "Acme", "dates": "2018-Present"},
            ],
        }
        assert _infer_experience_levels_from_profile(profile) == ["senior"]

    def test_long_tenure_falls_through_to_senior(self):
        # No senior token in title, but ≥7 calendar years across roles
        # → senior via the years-of-experience fallback.
        profile = {
            "summary": "Software developer.",
            "experience": [
                {"title": "Software Engineer", "company": "Acme", "dates": "2013-Present"},
            ],
        }
        assert _infer_experience_levels_from_profile(profile) == ["senior"]

    def test_three_to_seven_years_returns_mid(self):
        profile = {
            "summary": "Software developer.",
            "experience": [
                {"title": "Software Engineer", "company": "Acme", "dates": "2020-Present"},
            ],
        }
        # ~6 years span → mid-level.
        assert _infer_experience_levels_from_profile(profile) == ["mid-level"]

    def test_short_tenure_returns_entry(self):
        profile = {
            "summary": "Software developer.",
            "experience": [
                {"title": "Software Engineer", "company": "Acme", "dates": "2024-Present"},
            ],
        }
        # ~1 year span → entry-level.
        assert _infer_experience_levels_from_profile(profile) == ["entry-level"]

    def test_no_dates_no_signals_returns_entry(self):
        profile = {"summary": "Builder.", "experience": []}
        assert _infer_experience_levels_from_profile(profile) == ["entry-level"]

    def test_in_progress_education_overrides_chief_title(self):
        # Robustness backup for the LLM-rewrite scenario: even if the
        # summary doesn't say "fresh graduating student", a degree with
        # a future end-year + chief/lead student club title should still
        # classify as student rather than senior.
        profile = {
            "summary": "Final-year major at Buckinghamshire seeking ops roles.",
            "experience": [
                {"title": "Chief of Operation", "dates": "2020-2023"},
            ],
            "education": [
                {"degree": "BSc Aviation Management", "year": "2024-2027"},
            ],
        }
        assert _infer_experience_levels_from_profile(profile) == ["internship", "entry-level"]

    def test_senior_with_mba_in_progress_stays_senior(self):
        # Mid-career engineer pursuing an MBA — must NOT be miscategorised
        # as student. Work span ≥ 5 years gates the in-progress-edu rule.
        profile = {
            "summary": "Engineer.",
            "experience": [
                {"title": "Software Engineer", "dates": "2017-Present"},
            ],
            "education": [
                {"degree": "BSc CS", "year": "2013-2017"},
                {"degree": "MBA", "year": "2025-2027"},
            ],
        }
        assert _infer_experience_levels_from_profile(profile) == ["senior"]

    def test_present_marker_on_education(self):
        # ``Present`` / ``Current`` on an education entry counts as
        # in-progress just like a future end year.
        profile = {
            "summary": "",
            "experience": [
                {"title": "Software Engineering Intern", "dates": "2024"},
            ],
            "education": [
                {"degree": "BSc CS", "year": "2022-Present"},
            ],
        }
        assert _infer_experience_levels_from_profile(profile) == ["internship", "entry-level"]


# ── _apply_profile_search_prefs force-refresh policy ───────────────────────


class TestApplyProfileSearchPrefs:
    def test_no_force_only_fills_empty_fields(self):
        # User has explicit prefs from a prior resume. With force_refresh=False
        # we must NOT clobber them — only blank fields get filled.
        state = {
            "job_titles": "Hardware Engineer",
            "location": "Seattle, WA",
            "experience_levels": [],
            "education_filter": ["bachelors"],
        }
        new_profile = {
            "summary": "Fresh graduating student.",
            "location": "Hong Kong",
            "target_titles": ["Park Ambassador"],
            "experience": [],
            "education": [{"degree": "BSc", "field": "Aviation"}],
        }
        _apply_profile_search_prefs(state, new_profile, force_refresh=False)
        # Pre-set scalars are preserved.
        assert state["job_titles"] == "Hardware Engineer"
        assert state["location"] == "Seattle, WA"
        assert state["education_filter"] == ["bachelors"]
        # The previously-empty list IS populated.
        assert state["experience_levels"] == ["internship", "entry-level"]

    def test_force_refresh_overwrites_stale_prefs(self):
        # Regression for the user's complaint: uploading a new primary resume
        # MUST refresh location / experience / education chips.
        state = {
            "job_titles": "Hardware Engineer",
            "location": "Seattle, WA",
            "experience_levels": ["mid-level"],
            "education_filter": ["bachelors"],
        }
        new_profile = {
            "summary": "I am a fresh graduating student currently studying.",
            "location": "Hong Kong",
            "target_titles": ["Park Operation Ambassador"],
            "experience": [],
            "education": [{"degree": "BSc Aviation Management"}],
        }
        _apply_profile_search_prefs(state, new_profile, force_refresh=True)
        assert state["location"] == "Hong Kong"
        assert state["job_titles"] == "Park Operation Ambassador"
        assert state["experience_levels"] == ["internship", "entry-level"]
        # Bachelor → bachelors (matched both old and new — value stays).
        assert state["education_filter"] == ["bachelors"]

    def test_force_refresh_skips_blank_profile_fields(self):
        # If the new profile lacks a value, force_refresh must NOT wipe state.
        state = {
            "job_titles": "Hardware Engineer",
            "location": "Seattle, WA",
            "experience_levels": ["senior"],
            "education_filter": ["masters"],
        }
        sparse_profile = {"summary": "", "location": "", "target_titles": [], "experience": [], "education": []}
        _apply_profile_search_prefs(state, sparse_profile, force_refresh=True)
        # Nothing in the profile to overwrite with — state survives.
        assert state["location"] == "Seattle, WA"
        assert state["job_titles"] == "Hardware Engineer"
        assert state["experience_levels"] == ["senior"]
        assert state["education_filter"] == ["masters"]

    def test_returns_false_when_profile_is_none(self):
        assert _apply_profile_search_prefs({}, None) is False
        assert _apply_profile_search_prefs({}, {}) is False

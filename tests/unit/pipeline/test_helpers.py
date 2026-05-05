"""Tests for pipeline.helpers — stateless inference + dedup utilities."""
import pytest

from pipeline import helpers
from pipeline.helpers import (
    EDUCATION_RANK,
    check_citizenship_requirement,
    clean_location_for_glassdoor,
    deduplicate_jobs,
    education_filter,
    education_matches,
    filter_jobs_by_education,
    infer_citizenship_required,
    infer_education_required,
    infer_experience_level,
    infer_job_category,
)

pytestmark = pytest.mark.unit


# ── infer_experience_level ───────────────────────────────────────────────────


class TestInferExperienceLevel:
    @pytest.mark.parametrize("title", [
        "Software Engineering Intern",
        "FPGA Intern",
        "Hardware Engineer Intern,",
        "Engineering Intern (Summer 2026)",
    ])
    def test_intern_titles(self, title):
        assert infer_experience_level({"title": title, "description": ""}) == "internship"

    def test_internship_in_description(self):
        assert infer_experience_level({
            "title": "Photonics Engineer", "description": "Internship program"
        }) == "internship"

    def test_co_op_recognized(self):
        assert infer_experience_level({
            "title": "Engineer", "description": "co-op opportunity"
        }) == "internship"

    @pytest.mark.parametrize("title", [
        "Senior Hardware Engineer",
        "Sr. FPGA Designer",
        "Staff Engineer",
        "Principal Architect",
        "Lead Software Engineer",
        "Engineering Manager",
        "Director of Engineering",
    ])
    def test_senior_titles(self, title):
        assert infer_experience_level({"title": title, "description": ""}) == "senior"

    def test_senior_title_overrides_intern_in_description(self):
        # Senior-title cue wins even when "internal candidates" mentions appear.
        assert infer_experience_level({
            "title": "Senior Hardware Engineer",
            "description": "Open to internal candidates with internal mobility.",
        }) == "senior"

    @pytest.mark.parametrize("text", ["entry-level", "new grad", "0-2 years", "junior", "associate"])
    def test_entry_level(self, text):
        assert infer_experience_level({"title": "Engineer", "description": text}) == "entry-level"

    @pytest.mark.parametrize("text", ["2-5 years", "3+ years", "mid-level"])
    def test_mid_level(self, text):
        assert infer_experience_level({"title": "Engineer", "description": text}) == "mid-level"

    @pytest.mark.parametrize("text", ["5+ years", "7+ years", "10+ years"])
    def test_senior_via_years(self, text):
        assert infer_experience_level({"title": "Engineer", "description": text}) == "senior"

    def test_unknown(self):
        assert infer_experience_level({"title": "Engineer", "description": "Solve problems."}) == "unknown"

    def test_intern_substring_does_not_match_internal(self):
        # The word-boundary fix: "internal" / "international" must not register as "intern".
        assert infer_experience_level({
            "title": "International Operations Coordinator",
            "description": "Manage internal operations for our international team.",
        }) != "internship"


# ── infer_education_required ────────────────────────────────────────────────


class TestInferEducationRequired:
    def test_phd_required_wins(self):
        assert infer_education_required({
            "title": "Research Scientist",
            "description": "PhD required in physics or related field.",
            "requirements": [],
        }) == "phd"

    def test_masters_required(self):
        assert infer_education_required({
            "title": "ML Engineer",
            "description": "Master's required.",
            "requirements": [],
        }) == "masters"

    def test_bachelors_required(self):
        assert infer_education_required({
            "title": "SWE",
            "description": "Bachelor's required.",
            "requirements": [],
        }) == "bachelors"

    def test_senior_title_with_bachelors_and_masters_resolves_higher(self):
        # Senior roles that mention both are tagged by the higher level.
        assert infer_education_required({
            "title": "Senior Engineer",
            "description": "Bachelor's required. Master's preferred.",
            "requirements": [],
        }) == "masters"

    def test_unknown_when_no_signal(self):
        assert infer_education_required({
            "title": "Engineer", "description": "Build cool things.", "requirements": [],
        }) == "unknown"

    def test_picks_highest_when_multiple_mentioned_without_required(self):
        # No explicit "required" — fall back to highest mentioned.
        assert infer_education_required({
            "title": "Researcher",
            "description": "Doctoral degree preferred. Master's of science accepted.",
            "requirements": [],
        }) == "phd"

    def test_high_school_recognized(self):
        assert infer_education_required({
            "title": "Tech",
            "description": "High school diploma required",
            "requirements": [],
        }) == "high_school"

    def test_no_degree_required(self):
        assert infer_education_required({
            "title": "Tech", "description": "no degree required",
            "requirements": [],
        }) == "high_school"


# ── infer_citizenship_required ──────────────────────────────────────────────


class TestInferCitizenshipRequired:
    @pytest.mark.parametrize("text", [
        "U.S. citizenship is required",
        "Must be a US citizen",
        "ITAR-controlled",
        "Active Secret clearance required",
        "TS/SCI required",
        "Sponsorship is not available",
        "we cannot sponsor",
    ])
    def test_hard_block_yields_yes(self, text):
        assert infer_citizenship_required({
            "title": "Engineer", "description": text, "requirements": [],
        }) == "yes"

    @pytest.mark.parametrize("text", [
        "Authorized to work in the U.S.",
        "Equal Opportunity Employer",
        "Visa sponsorship available",
        "Open to all work authorizations",
    ])
    def test_soft_phrasing_does_not_yield_yes(self, text):
        # Soft phrases must NOT trip the hard-block path.
        assert infer_citizenship_required({
            "title": "Engineer", "description": text, "requirements": [],
        }) in ("no", "unknown")

    def test_check_citizenship_requirement_subtracts_soft_first(self):
        # Mixed text — soft phrase should be stripped before regex matches.
        text = "Authorized to work in the US. Equal opportunity employer."
        assert check_citizenship_requirement(text) is False

    def test_check_citizenship_requirement_handles_none(self):
        assert check_citizenship_requirement(None) is False
        assert check_citizenship_requirement("") is False


# ── infer_job_category ──────────────────────────────────────────────────────


class TestInferJobCategory:
    @pytest.mark.parametrize("title,expected", [
        ("Senior Software Engineer", "engineering"),
        ("FPGA Hardware Engineer", "engineering"),
        ("Data Scientist", "data"),
        ("ML Engineer", "data"),
        ("Account Executive", "sales"),
        ("Marketing Manager", "marketing"),
        ("UX Designer", "design"),
        ("Registered Nurse", "healthcare"),
        ("Paralegal", "legal"),
        ("Teacher", "education"),
        ("Recruiter", "hr"),
        ("Operations Manager", "operations"),
        ("Product Manager", "product"),
    ])
    def test_categories(self, title, expected):
        assert infer_job_category({"title": title, "description": ""}) == expected

    def test_general_fallback(self):
        # No keyword match → "general".
        assert infer_job_category({"title": "Foo Bar Baz", "description": ""}) == "general"


# ── deduplicate_jobs ────────────────────────────────────────────────────────


class TestDeduplicateJobs:
    def test_merges_same_company_title(self):
        helpers._last_merge_count = -1  # sanity
        jobs = [
            {"company": "Acme", "title": "FPGA Intern", "location": "Boston, MA"},
            {"company": "Acme", "title": "FPGA Intern", "location": "Austin, TX"},
            {"company": "Acme", "title": "FPGA Intern", "location": "Boston, MA"},
        ]
        result = deduplicate_jobs(jobs)
        assert len(result) == 1
        assert "Boston" in result[0]["location"] and "Austin" in result[0]["location"]
        assert helpers._last_merge_count == 2

    def test_preserves_distinct_titles(self):
        jobs = [
            {"company": "Acme", "title": "FPGA Intern", "location": "Boston"},
            {"company": "Acme", "title": "Photonics Intern", "location": "Boston"},
        ]
        assert len(deduplicate_jobs(jobs)) == 2

    def test_promotes_remote_flag(self):
        jobs = [
            {"company": "Acme", "title": "FPGA Intern", "location": "Boston", "remote": False},
            {"company": "Acme", "title": "FPGA Intern", "location": "Remote", "remote": True},
        ]
        result = deduplicate_jobs(jobs)
        assert result[0]["remote"] is True

    def test_fills_missing_url_and_salary(self):
        jobs = [
            {"company": "Acme", "title": "FPGA Intern", "location": "Boston"},
            {"company": "Acme", "title": "FPGA Intern", "location": "Boston",
             "application_url": "https://acme.example/apply", "salary_range": "$30-40/hr"},
        ]
        result = deduplicate_jobs(jobs)
        assert result[0]["application_url"] == "https://acme.example/apply"
        assert result[0]["salary_range"] == "$30-40/hr"


# ── education filtering ─────────────────────────────────────────────────────


class TestEducationFilter:
    def test_education_rank_ordering(self):
        assert EDUCATION_RANK["high_school"] < EDUCATION_RANK["bachelors"] < EDUCATION_RANK["phd"]

    def test_education_matches_directional(self):
        # bachelors required, masters user → True
        assert education_matches("bachelors", "masters") is True
        # masters required, bachelors user → False
        assert education_matches("masters", "bachelors") is False
        # equal levels → True
        assert education_matches("bachelors", "bachelors") is True

    def test_education_matches_unknown_returns_false(self):
        assert education_matches(None, "bachelors") is False
        assert education_matches("bachelors", "doctoraat") is False

    def test_filter_jobs_by_education_keeps_unknown_when_flag_set(self):
        jobs = [
            {"education_required": "bachelors"},
            {"education_required": "masters"},
            {"education_required": "unknown"},
        ]
        kept = filter_jobs_by_education(jobs, "bachelors", include_unknown=True)
        assert len(kept) == 2  # bachelors + unknown
        assert helpers._last_education_dropped_mismatch == 1

    def test_filter_jobs_by_education_drops_unknown_when_flag_unset(self):
        jobs = [
            {"education_required": "bachelors"},
            {"education_required": "unknown"},
        ]
        kept = filter_jobs_by_education(jobs, "bachelors", include_unknown=False)
        assert len(kept) == 1
        assert helpers._last_education_dropped_unknown == 1

    def test_filter_with_no_user_education_passes_through(self):
        jobs = [{"education_required": "phd"}]
        assert filter_jobs_by_education(jobs, []) == jobs

    def test_education_filter_predicate_is_inclusive(self):
        # Inclusive-by-default — even unknowns return True.
        assert education_filter({"education_required": "unknown"}) is True
        assert education_filter({"education_required": "bachelors"}) is True
        assert education_filter(None) is True


# ── clean_location_for_glassdoor ────────────────────────────────────────────


class TestCleanLocationForGlassdoor:
    @pytest.mark.parametrize("raw,expected", [
        ("United States", "United States"),
        ("usa", "United States"),
        ("us", "United States"),
        ("America", "United States"),
        ("anywhere", "Remote"),
        ("Remote, US", "Remote"),
        ("REMOTE", "Remote"),
        ("Boston, MA", "Boston, MA"),
        ("San Francisco", "San Francisco"),
    ])
    def test_normalisations(self, raw, expected):
        assert clean_location_for_glassdoor(raw) == expected

    def test_handles_none(self):
        assert clean_location_for_glassdoor(None) == "United States"

    def test_handles_empty(self):
        assert clean_location_for_glassdoor("   ") == "United States"

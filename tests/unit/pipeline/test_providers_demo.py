"""Tests for pipeline.providers.DemoProvider — pure-Python regex/template path."""
import pytest

from pipeline.providers import DemoProvider, _build_rubric_result, compute_skill_coverage

pytestmark = pytest.mark.unit


@pytest.fixture
def provider():
    return DemoProvider()


@pytest.fixture
def sample_resume_text(fixtures_dir):
    return (fixtures_dir / "resumes" / "sample_text.txt").read_text(encoding="utf-8")


# ── Section splitter ────────────────────────────────────────────────────────


class TestSplitSections:
    def test_recognises_canonical_headers(self, sample_resume_text):
        sections = DemoProvider._split_sections(sample_resume_text)
        # Canonical bucket names from _HEADER_ALIASES.
        assert "education" in sections
        assert "skills" in sections
        assert "projects" in sections
        assert "experience" in sections
        assert "research experience" in sections

    def test_classify_header_aliases(self):
        for raw, expected in [
            ("Work Experience", "experience"),
            ("PROFESSIONAL EXPERIENCE", "experience"),
            ("Technical Skills:", "skills"),
            ("Personal Projects", "projects"),
            ("Lab Experience", "research experience"),
        ]:
            assert DemoProvider._classify_header(raw) == expected

    def test_classify_header_returns_none_for_non_header(self):
        assert DemoProvider._classify_header("This is body content of a section.") is None
        assert DemoProvider._classify_header("") is None

    def test_header_section_with_decoration(self):
        # Underline-style + caps decoration should still classify.
        assert DemoProvider._classify_header("── EDUCATION ──") == "education"


# ── Skills lexicon ──────────────────────────────────────────────────────────


class TestSkillsFromText:
    def test_finds_known_keywords(self, provider):
        skills = provider._skills_from_text("I know Verilog, Python, and SPICE.")
        # Display names use canonical capitalization.
        labels = {s.lower() for s in skills}
        assert "verilog" in labels
        assert "python" in labels
        assert "spice" in labels

    def test_missing_keywords_return_empty(self, provider):
        assert provider._skills_from_text("This text mentions nothing technical.") == []

    def test_dedupes_case_insensitive(self, provider):
        skills = provider._skills_from_text("verilog VERILOG Verilog")
        # All three forms collapse to one entry.
        assert sum(1 for s in skills if s.lower() == "verilog") == 1


# ── extract_profile ─────────────────────────────────────────────────────────


class TestExtractProfile:
    def test_returns_required_fields(self, provider, sample_resume_text):
        p = provider.extract_profile(sample_resume_text)
        for k in ("name", "email", "linkedin", "github", "phone", "location",
                  "target_titles", "top_hard_skills", "top_soft_skills",
                  "education", "experience", "projects", "resume_gaps"):
            assert k in p

    def test_extracts_email(self, provider, sample_resume_text):
        p = provider.extract_profile(sample_resume_text)
        assert "@example.com" in p["email"]

    def test_extracts_linkedin(self, provider, sample_resume_text):
        p = provider.extract_profile(sample_resume_text)
        assert "linkedin.com/in/jane-tester" in p["linkedin"]

    def test_extracts_github(self, provider, sample_resume_text):
        p = provider.extract_profile(sample_resume_text)
        assert "github.com/jane-tester" in p["github"]

    def test_finds_known_hard_skills(self, provider, sample_resume_text):
        p = provider.extract_profile(sample_resume_text)
        skills_lower = {s.lower() for s in p["top_hard_skills"]}
        assert "python" in skills_lower
        assert "verilog" in skills_lower
        assert "matlab" in skills_lower

    def test_education_block_parsed(self, provider, sample_resume_text):
        p = provider.extract_profile(sample_resume_text)
        edu = p["education"]
        assert isinstance(edu, list)
        assert len(edu) >= 1
        first = edu[0]
        assert any(k in first.get("institution", "") for k in ("Berkeley", "California"))
        assert "3.85" in first.get("gpa", "")

    def test_preferred_titles_lead_in_target_list(self, provider, sample_resume_text):
        prefs = ["RF Engineering Intern"]
        p = provider.extract_profile(sample_resume_text, preferred_titles=prefs)
        # Caller-supplied titles must appear in the output.
        assert "RF Engineering Intern" in p["target_titles"]


# ── score_job ───────────────────────────────────────────────────────────────


class TestScoreJob:
    def test_passing_job_returns_breakdown(self, provider):
        profile = {
            "top_hard_skills": ["Verilog", "Python", "FPGA"],
            "target_titles": ["FPGA Engineering Intern"],
            "location": "Remote",
        }
        job = {
            "id": "j1", "title": "FPGA Intern", "company": "Acme",
            "location": "Remote", "remote": True,
            "experience_level": "internship",
            "requirements": ["Verilog", "Python"],
            "description": "Build FPGA blocks.",
        }
        result = provider.score_job(job, profile)
        assert result["job_id"] == "j1"
        assert 0 <= result["score"] <= 100
        assert "score_breakdown" in result
        for k in ("required_skills", "industry", "location_seniority"):
            assert k in result["score_breakdown"]
            sub = result["score_breakdown"][k]
            assert 0 <= sub["raw"] <= 1
            assert sub["points"] >= 0

    def test_irrelevant_job_scores_low(self, provider):
        profile = {"top_hard_skills": ["Verilog"], "target_titles": ["FPGA Intern"]}
        job = {"id": "j2", "title": "Marketing Lead", "company": "Acme",
               "location": "Anywhere", "experience_level": "senior",
               "requirements": ["seo", "social media"], "description": ""}
        result = provider.score_job(job, profile)
        # Score must be low; no exact value but well under 60.
        assert result["score"] < 60


# ── tailor_resume ───────────────────────────────────────────────────────────


class TestTailorResume:
    def _flat_skills(self, out):
        return [it["text"] for cat in out.get("skills") or [] for it in cat.get("items") or []]

    def test_reorders_skills_jd_keywords_first(self, provider):
        profile = {"top_hard_skills": ["Python", "Verilog", "MATLAB"]}
        job = {"title": "FPGA Intern", "company": "Acme",
               "requirements": ["verilog", "fpga"]}
        out = provider.tailor_resume(job, profile, "")
        ordered = self._flat_skills(out)
        assert ordered.index("Verilog") < ordered.index("Python")

    def test_ats_keywords_missing_includes_unmatched(self, provider):
        profile = {"top_hard_skills": ["Python"]}
        job = {"title": "Eng", "company": "Co",
               "requirements": ["verilog", "uvm"]}
        out = provider.tailor_resume(job, profile, "")
        missing_lower = {m.lower() for m in out["ats_keywords_missing"]}
        assert "verilog" in missing_lower
        assert "uvm" in missing_lower

    def test_section_order_default(self, provider):
        out = provider.tailor_resume({"requirements": []}, {"top_hard_skills": []}, "")
        assert set(out["section_order"]) == {"Skills", "Projects", "Experience", "Education"}

    def test_returns_v2_schema(self, provider):
        from pipeline.heuristic_tailor import validate_v2_or_none
        profile = {
            "name": "Jane",
            "top_hard_skills": ["Python", "Verilog"],
            "experience": [{"title": "Intern", "company": "Acme", "dates": "2024",
                             "bullets": ["Built it"]}],
            "education": [{"degree": "BS", "institution": "Cal", "year": "2025"}],
        }
        job = {"title": "HW Eng", "company": "X",
               "requirements": ["Verilog", "FPGA verification"]}
        out = provider.tailor_resume(job, profile, "Jane resume",
                                      selected_keywords=["FPGA verification"])
        assert validate_v2_or_none(out) is not None
        assert out["schema_version"] == 2


# ── generate_cover_letter ───────────────────────────────────────────────────


class TestCoverLetter:
    def test_three_paragraph_structure(self, provider):
        profile = {"name": "Jane Tester", "email": "j@x.com",
                   "top_hard_skills": ["Verilog", "Python", "FPGA"],
                   "education": [{"degree": "B.S. EE", "institution": "MIT"}]}
        job = {"title": "FPGA Intern", "company": "Acme",
               "requirements": ["verilog", "python"]}
        letter = provider.generate_cover_letter(job, profile)
        assert "Acme" in letter
        assert "FPGA Intern" in letter
        assert "Jane Tester" in letter
        assert letter.count("\n\n") >= 2  # at least 3 paragraphs


# ── generate_demo_jobs ──────────────────────────────────────────────────────


class TestGenerateDemoJobs:
    def test_returns_canned_jobs(self, provider):
        from pipeline.config import DEMO_JOBS
        out = provider.generate_demo_jobs({}, ["FPGA Intern"], "Remote")
        assert out == DEMO_JOBS


# ── chat ────────────────────────────────────────────────────────────────────


class TestChat:
    def test_returns_helpful_message(self, provider):
        result = provider.chat("system", [{"role": "user", "content": "hi"}])
        assert "demo" in result.lower() or "ollama" in result.lower()


# ── Shared rubric ───────────────────────────────────────────────────────────


class TestRubricResult:
    def test_total_in_zero_to_hundred(self):
        r = _build_rubric_result({"id": "j"}, 1.0, 1.0, 1.0)
        assert r["score"] == 100
        r = _build_rubric_result({"id": "j"}, 0.0, 0.0, 0.0)
        assert r["score"] == 0

    def test_clamps_negative_and_overflow(self):
        r = _build_rubric_result({"id": "j"}, -5, 5, 0.5)
        assert 0 <= r["score"] <= 100

    def test_default_reasoning_synthesised(self):
        r = _build_rubric_result({"id": "j"}, 0.5, 0.5, 0.5)
        assert "%" in r["reasoning"]

    def test_back_compat_reason_field(self):
        r = _build_rubric_result({"id": "j"}, 0.5, 0.5, 0.5,
                                  reasoning="explicit reasoning")
        assert r["reasoning"] == "explicit reasoning"
        assert r["reason"] == "explicit reasoning"


# ── compute_skill_coverage ──────────────────────────────────────────────────


class TestComputeSkillCoverage:
    def test_full_match(self):
        job = {"requirements": ["Python", "Verilog"], "description": ""}
        profile = {"top_hard_skills": ["Python", "Verilog"]}
        cov, matched, missing = compute_skill_coverage(job, profile)
        assert cov == 1.0
        assert set(matched) == {"Python", "Verilog"}
        assert missing == []

    def test_no_skills_returns_zero_coverage(self):
        # Empty top_hard_skills → coverage 0.0, NOT a fake-neutral 0.5.
        # The previous 0.5 default inflated every job to ~50% for blank /
        # template resumes, producing fake "68% match on senior hardware"
        # scores against an empty profile. Killed in commit 6c550d2.
        job = {"requirements": ["Verilog"], "description": ""}
        profile = {"top_hard_skills": []}
        cov, matched, missing = compute_skill_coverage(job, profile)
        assert cov == 0.0
        assert matched == []
        assert missing == []

    def test_caches_on_job_dict(self):
        job = {"requirements": ["Python"], "description": ""}
        profile = {"top_hard_skills": ["Python"]}
        compute_skill_coverage(job, profile)
        assert "_skill_coverage" in job
        # Second call uses cache (mutate skills, result still same).
        profile["top_hard_skills"] = ["Verilog"]
        cov2, _, _ = compute_skill_coverage(job, profile)
        assert cov2 == 1.0  # cached value, not recomputed

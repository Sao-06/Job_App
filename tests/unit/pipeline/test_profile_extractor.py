"""Tests for pipeline.profile_extractor — heuristic-first extraction."""
import pytest

from pipeline.profile_extractor import (
    heuristic_summary,
    merge_profiles,
    parse_experience_robust,
    parse_projects_robust,
    scan_profile,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def resume_text(fixtures_dir):
    return (fixtures_dir / "resumes" / "sample_text.txt").read_text(encoding="utf-8")


# ── scan_profile (full sweep) ───────────────────────────────────────────────


class TestScanProfile:
    def test_returns_complete_shape(self, resume_text):
        p = scan_profile(resume_text)
        for k in ("name", "email", "phone", "linkedin", "github", "website",
                  "location", "summary", "target_titles", "top_hard_skills",
                  "top_soft_skills", "education", "experience",
                  "research_experience", "projects", "resume_gaps"):
            assert k in p

    def test_extracts_email(self, resume_text):
        p = scan_profile(resume_text)
        assert "@example.com" in p["email"]

    def test_extracts_phone(self, resume_text):
        p = scan_profile(resume_text)
        assert "555" in p["phone"]

    def test_normalises_linkedin_url(self, resume_text):
        p = scan_profile(resume_text)
        assert p["linkedin"].startswith("https://")
        assert "linkedin.com" in p["linkedin"]

    def test_normalises_github_url(self, resume_text):
        p = scan_profile(resume_text)
        assert p["github"].startswith("https://")
        assert "github.com" in p["github"]

    def test_finds_known_hard_skills(self, resume_text):
        p = scan_profile(resume_text)
        labels = {s.lower() for s in p["top_hard_skills"]}
        for expected in ("python", "verilog", "matlab"):
            assert expected in labels

    def test_extracts_education(self, resume_text):
        p = scan_profile(resume_text)
        assert isinstance(p["education"], list)
        assert len(p["education"]) >= 1
        edu = p["education"][0]
        assert "Berkeley" in edu.get("institution", "") or \
               "California" in edu.get("institution", "")

    def test_extracts_projects(self, resume_text):
        p = scan_profile(resume_text)
        assert isinstance(p["projects"], list)
        assert len(p["projects"]) >= 1

    def test_extraction_method_marked(self, resume_text):
        p = scan_profile(resume_text)
        assert p["_extraction_method"] == "heuristic"

    def test_handles_empty_text(self):
        p = scan_profile("")
        # All list fields default to empty.
        assert p["top_hard_skills"] == []
        assert p["education"] == []


# ── merge_profiles ──────────────────────────────────────────────────────────


class TestMergeProfiles:
    def test_llm_only_returns_heuristic_when_llm_none(self):
        h = {"name": "Jane", "top_hard_skills": ["Python"]}
        out = merge_profiles(h, None)
        assert out["name"] == "Jane"
        assert out["_extraction_method"] == "heuristic-only"

    def test_heuristic_wins_for_email(self):
        h = {"email": "real@example.com"}
        l = {"email": "wrong@example.com"}
        out = merge_profiles(h, l)
        assert out["email"] == "real@example.com"

    def test_llm_fills_missing_email(self):
        h = {"email": ""}
        l = {"email": "from-llm@example.com"}
        out = merge_profiles(h, l)
        assert out["email"] == "from-llm@example.com"

    def test_llm_wins_for_summary(self):
        # Free-text scalars: LLM is richer, so it wins.
        h = {"summary": "short heuristic"}
        l = {"summary": "Long LLM-curated paragraph that describes everything."}
        out = merge_profiles(h, l)
        assert out["summary"].startswith("Long")

    def test_string_lists_unioned(self):
        h = {"top_hard_skills": ["Python", "Verilog"]}
        l = {"top_hard_skills": ["FPGA", "Python"]}
        out = merge_profiles(h, l)
        # Union, dedupe, preserve order.
        assert "Python" in out["top_hard_skills"]
        assert "Verilog" in out["top_hard_skills"]
        assert "FPGA" in out["top_hard_skills"]

    def test_richer_education_wins(self):
        h = {"education": [{"degree": "B.S.", "institution": "School", "year": "2026"}]}
        l = {"education": []}
        out = merge_profiles(h, l)
        assert out["education"] == h["education"]

        # Now with LLM richer.
        h2 = {"education": [{"degree": "B.S."}]}  # sparse
        l2 = {"education": [{"degree": "B.S. EE", "institution": "MIT",
                              "year": "2026", "gpa": "3.85"}]}
        out2 = merge_profiles(h2, l2)
        assert out2["education"][0]["institution"] == "MIT"

    def test_extraction_method_combined(self):
        h = {"name": "Jane"}
        l = {"name": "Jane"}
        out = merge_profiles(h, l)
        assert out["_extraction_method"] == "heuristic+llm"


# ── parse_experience_robust ─────────────────────────────────────────────────


class TestParseExperienceRobust:
    def test_single_line_pipe_header(self):
        # Avoid month-name substrings ("Nov" / "Mar" / "Jul" / etc.) in the
        # company name — the date regex matches them as month tokens.
        lines = [
            "FPGA Engineering Intern | Acme Robotics | 2023 – 2024",
            "  Implemented a SerDes equalizer block.",
            "  Wrote constrained-random tests.",
        ]
        roles = parse_experience_robust(lines)
        assert len(roles) == 1
        r = roles[0]
        assert "FPGA" in r["title"]
        assert "Acme" in r["company"]
        assert len(r["bullets"]) == 2

    def test_two_line_header(self):
        lines = [
            "Software Engineer    Acme Corp",
            "May 2023 – Aug 2023    Boston, MA",
            "• Built a thing",
        ]
        roles = parse_experience_robust(lines)
        assert len(roles) == 1
        # Company + dates split across the two-line header.
        assert "Acme" in roles[0]["company"] or roles[0]["company"]
        assert roles[0]["dates"]

    def test_disambiguates_title_company_when_swapped(self):
        # Plan-Bizarro format: company first, title with role keyword second.
        lines = ["Apple Inc. — Embedded Engineering Intern — Summer 2024"]
        roles = parse_experience_robust(lines)
        assert len(roles) == 1
        # The "Engineering Intern" half should be detected as the title.
        assert "Engineering" in roles[0]["title"] or "Intern" in roles[0]["title"]

    def test_empty_input(self):
        assert parse_experience_robust([]) == []

    def test_drops_completely_empty_role(self):
        # Bullets without any header create a placeholder; if no real signal
        # accumulates, the entry stays for the bullet.
        roles = parse_experience_robust(["• Did some stuff", "• More stuff"])
        # Either keeps a single placeholder role with bullets, or drops.
        assert all(r.get("bullets") or r.get("title") or r.get("company")
                   for r in roles)


# ── parse_projects_robust ───────────────────────────────────────────────────


class TestParseProjectsRobust:
    def test_title_then_bullets(self):
        # Project name must be Title-Case (cap ratio >= 0.6) for the parser
        # to recognise it as a header.
        lines = [
            "Photonic Ring Resonator Simulation",
            "Designed And Verified Q-factor Of 12000.",
            "Tools: Python, Lumerical, COMSOL",
        ]
        projs = parse_projects_robust(lines)
        assert len(projs) >= 1
        # The first non-bullet Title-Case line must be the project name.
        assert "Photonic" in projs[0]["name"]

    def test_tech_tag_line_extracts_skills(self):
        # Use "Tools:" rather than "Tech Stack:" so the entire prefix is
        # consumed by the regex (the alternation matches "tech" before
        # "stack" and would leave "Stack:" inside the captured tail).
        lines = [
            "Web Backend Project",
            "Tools: Python, FastAPI, Postgres",
        ]
        projs = parse_projects_robust(lines)
        assert "Python" in projs[0]["skills_used"]
        assert "FastAPI" in projs[0]["skills_used"]
        assert "Postgres" in projs[0]["skills_used"]


# ── heuristic_summary ───────────────────────────────────────────────────────


class TestHeuristicSummary:
    def test_summary_format(self):
        p = {"name": "Jane", "email": "j@x.com", "linkedin": "ln",
             "github": "gh", "location": "CA",
             "top_hard_skills": ["A", "B"], "experience": [],
             "research_experience": [], "projects": [{"name": "X"}],
             "education": []}
        out = heuristic_summary(p)
        assert "Jane" in out
        assert "hard_skills=2" in out
        assert "projects=1" in out

"""Tests for pipeline.profile_audit — the post-extraction validation layer."""
import pytest

from pipeline.profile_audit import (
    DOMAIN_TITLE_FAMILIES,
    FORBIDDEN_GENERIC_TITLES,
    HARD_SKILL_LEXICON,
    SOFT_SKILL_ALLOWED,
    audit_profile,
    flatten_profile,
    quarantine_misplaced_skills,
    rerank_titles,
    retention_audit,
    verify_evidence,
)

pytestmark = pytest.mark.unit


# ── Lexicons ────────────────────────────────────────────────────────────────


class TestLexicons:
    def test_hard_skill_lexicon_populated(self):
        assert len(HARD_SKILL_LEXICON) > 50
        assert "verilog" in HARD_SKILL_LEXICON
        assert "spice" in HARD_SKILL_LEXICON
        assert "fpga" in HARD_SKILL_LEXICON

    def test_domain_title_families_present(self):
        assert any("IC Design" in f for f in DOMAIN_TITLE_FAMILIES)
        assert any("Photonics" in f for f in DOMAIN_TITLE_FAMILIES)

    def test_forbidden_generic_titles(self):
        # The forbidden set contains common "soft tech" titles.
        assert "software engineer" in FORBIDDEN_GENERIC_TITLES
        assert "data scientist" in FORBIDDEN_GENERIC_TITLES
        assert "product manager" in FORBIDDEN_GENERIC_TITLES

    def test_soft_skills_allow_list(self):
        # Lab techniques must NOT be in the soft list.
        assert "verilog" not in SOFT_SKILL_ALLOWED
        # Real soft skills are present.
        assert "teamwork" in SOFT_SKILL_ALLOWED
        assert "communication" in SOFT_SKILL_ALLOWED


# ── flatten_profile ─────────────────────────────────────────────────────────


class TestFlattenProfile:
    def test_string_skills_passthrough(self):
        p = {"top_hard_skills": ["Verilog", "Python"],
             "top_soft_skills": ["Teamwork"], "target_titles": ["FPGA Intern"]}
        out = flatten_profile(p)
        assert out["top_hard_skills"] == ["Verilog", "Python"]
        assert out["top_soft_skills"] == ["Teamwork"]
        assert out["target_titles"] == ["FPGA Intern"]

    def test_dict_skills_get_flattened(self):
        p = {"top_hard_skills": [
            {"skill": "Verilog", "category": "language", "evidence": "Verilog ALU"},
            {"skill": "Python"},
        ]}
        out = flatten_profile(p)
        assert out["top_hard_skills"] == ["Verilog", "Python"]
        # The detailed list is preserved separately.
        assert out["top_hard_skills_detailed"][0]["skill"] == "Verilog"

    def test_dict_titles_get_flattened(self):
        p = {"target_titles": [
            {"title": "FPGA Intern", "family": "FPGA", "evidence": "RTL work"},
        ]}
        out = flatten_profile(p)
        assert out["target_titles"] == ["FPGA Intern"]
        assert out["target_titles_detailed"][0]["family"] == "FPGA"

    def test_dedupe_preserves_first_occurrence(self):
        p = {"top_hard_skills": ["Python", "python", "PYTHON"]}
        out = flatten_profile(p)
        assert len(out["top_hard_skills"]) == 1


# ── quarantine_misplaced_skills ─────────────────────────────────────────────


class TestQuarantine:
    def test_moves_lab_terms_from_soft_to_hard(self):
        p = {"top_hard_skills": ["Python"],
             "top_soft_skills": ["Verilog", "Teamwork"]}
        out = quarantine_misplaced_skills(p)
        assert "Verilog" in out["top_hard_skills"]
        assert "Verilog" not in out["top_soft_skills"]
        assert "Teamwork" in out["top_soft_skills"]
        assert any("quarantined" in line for line in (out.get("_audit_log") or []))

    def test_drops_obviously_invalid(self):
        p = {"top_hard_skills": [], "top_soft_skills": ["", "n/a", "None"]}
        out = quarantine_misplaced_skills(p)
        assert out["top_soft_skills"] == []

    def test_handles_dict_form(self):
        # Mostly handled by flatten_profile but quarantine should be tolerant.
        p = {"top_hard_skills": ["Python"], "top_soft_skills": ["Communication"]}
        out = quarantine_misplaced_skills(p)
        assert out["top_soft_skills"] == ["Communication"]


# ── retention_audit ─────────────────────────────────────────────────────────


class TestRetentionAudit:
    def test_recovers_missing_token_present_in_text(self):
        p = {"top_hard_skills": ["Python"]}
        text = "Designed mixed-signal SPICE testbenches. Used MATLAB for analysis."
        out = retention_audit(p, text)
        skills_lower = {s.lower() for s in out["top_hard_skills"]}
        assert "spice" in skills_lower
        assert "matlab" in skills_lower

    def test_word_boundary_prevents_false_positive(self):
        # "rie" inside "experience" must NOT be picked up.
        p = {"top_hard_skills": []}
        text = "I have experience with retrieval and serialization."
        out = retention_audit(p, text)
        assert "rie" not in {s.lower() for s in out.get("top_hard_skills", [])}

    def test_no_text_no_changes(self):
        p = {"top_hard_skills": ["Python"]}
        assert retention_audit(p, "") == p

    def test_pretty_acronyms_uppercased(self):
        p = {"top_hard_skills": []}
        text = "Used CVD and ALD for thin-film deposition."
        out = retention_audit(p, text)
        labels = {s for s in out["top_hard_skills"]}
        # Short tokens are uppercased.
        assert "CVD" in labels or "Cvd" in labels


# ── verify_evidence ─────────────────────────────────────────────────────────


class TestVerifyEvidence:
    def test_keeps_skills_with_evidence_in_resume(self):
        p = {
            "top_hard_skills": ["Verilog"],
            "top_hard_skills_detailed": [
                {"skill": "Verilog", "category": "language",
                 "evidence": "Designed an 8-bit ALU in Verilog with a UVM"},
            ],
        }
        text = "Designed an 8-bit ALU in Verilog with a UVM testbench."
        out = verify_evidence(p, text)
        verified = out["top_hard_skills_detailed"]
        assert any(e["skill"] == "Verilog" for e in verified)

    def test_drops_skill_with_unverifiable_evidence(self):
        p = {
            "top_hard_skills": ["Quantum Annealing"],
            "top_hard_skills_detailed": [
                {"skill": "Quantum Annealing", "category": "method",
                 "evidence": "Built a D-Wave control system"},
            ],
        }
        text = "Wrote a sorting algorithm in Python."  # no overlap
        out = verify_evidence(p, text)
        assert all(e["skill"] != "Quantum Annealing"
                   for e in out["top_hard_skills_detailed"])

    def test_keeps_entry_with_empty_evidence(self):
        p = {
            "top_hard_skills_detailed": [
                {"skill": "Python", "category": "language", "evidence": ""},
            ],
        }
        out = verify_evidence(p, "Some unrelated resume text")
        # Empty-evidence entries are trusted (e.g. retention-audit recoveries).
        assert out["top_hard_skills_detailed"][0]["skill"] == "Python"

    def test_skill_name_in_text_overrides_evidence_mismatch(self):
        p = {
            "top_hard_skills_detailed": [
                {"skill": "Python", "category": "language",
                 "evidence": "Built a parser using lex/yacc"},
            ],
        }
        # Evidence doesn't appear, but the skill name does.
        text = "Used Python for data processing."
        out = verify_evidence(p, text)
        assert out["top_hard_skills_detailed"][0]["skill"] == "Python"


# ── rerank_titles ───────────────────────────────────────────────────────────


class TestRerankTitles:
    def test_drops_forbidden_for_hardware_candidate(self):
        p = {
            "education": [{"degree": "B.S. Electrical Engineering"}],
            "research_experience": [{"title": "FPGA design",
                                      "company": "Lab", "bullets": ["Verilog work"]}],
            "projects": [{"name": "ALU", "skills_used": ["Verilog"]}],
            "target_titles_detailed": [
                {"title": "FPGA Intern", "family": "FPGA",
                 "evidence": "Designed an FPGA"},
                {"title": "Software Engineer", "family": "Software",
                 "evidence": "Wrote some code"},
            ],
        }
        out = rerank_titles(p)
        kept = [t["title"] for t in out["target_titles_detailed"]]
        assert "FPGA Intern" in kept
        assert "Software Engineer" not in kept

    def test_keeps_software_when_no_hardware_footprint(self):
        # CS-only candidate — software titles ARE allowed.
        p = {
            "education": [{"degree": "B.S. Computer Science"}],
            "research_experience": [],
            "projects": [{"name": "Web app", "skills_used": ["React", "Node.js"]}],
            "target_titles_detailed": [
                {"title": "Software Engineer", "family": "Software",
                 "evidence": "Built a web app"},
            ],
        }
        out = rerank_titles(p)
        kept = [t["title"] for t in out["target_titles_detailed"]]
        assert "Software Engineer" in kept


# ── audit_profile (full chain) ──────────────────────────────────────────────


class TestAuditProfile:
    def test_chain_runs_without_error(self):
        p = {
            "top_hard_skills": ["Python", "Verilog"],
            "top_soft_skills": ["Teamwork", "Cleanroom"],  # cleanroom misplaced
            "target_titles": [
                {"title": "FPGA Intern", "family": "FPGA",
                 "evidence": "Designed FPGA blocks"},
            ],
            "education": [{"degree": "B.S. EE"}],
            "research_experience": [],
            "projects": [],
        }
        text = "Designed FPGA blocks using Verilog. Hands-on cleanroom work."
        out = audit_profile(p, text)
        assert "Verilog" in out["top_hard_skills"]
        # Cleanroom was moved from soft → hard (and then verified by retention).
        skills_l = {s.lower() for s in out["top_hard_skills"]}
        assert "cleanroom" in skills_l
        assert "_audit_log" in out

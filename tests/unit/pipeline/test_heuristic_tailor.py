"""Tests for pipeline.heuristic_tailor — the deterministic safety net used
when low-end LLMs return malformed tailoring data.

The most important invariants here are *anti-fabrication*:
  • skills_reordered only ever reorders existing profile skills.
  • experience_bullets only ever reorders existing role bullets.
  • Missing JD keywords are surfaced via ats_keywords_missing — NEVER
    silently merged into the user's skill list.
"""
import pytest

from pipeline.heuristic_tailor import (
    validate_tailoring,
    heuristic_tailor_resume,
    merge_with_heuristic,
)

pytestmark = pytest.mark.unit


# ── validate_tailoring ──────────────────────────────────────────────────────

class TestValidate:
    def test_none_returns_none(self):
        assert validate_tailoring(None) is None

    def test_empty_dict_returns_none(self):
        # No usable signal — caller falls back to heuristic.
        assert validate_tailoring({}) is None

    def test_well_formed_passes_through(self):
        out = validate_tailoring({
            "skills_reordered":   ["Python", "Verilog"],
            "experience_bullets": [{"role": "Eng", "bullets": ["did x"]}],
            "ats_keywords_missing": ["FPGA"],
            "section_order":      ["Skills", "Experience"],
        })
        assert out["skills_reordered"] == ["Python", "Verilog"]
        assert out["experience_bullets"] == [{"role": "Eng", "bullets": ["did x"]}]
        assert out["ats_keywords_missing"] == ["FPGA"]
        assert out["section_order"] == ["Skills", "Experience"]

    def test_skills_as_dicts_coerced_to_strings(self):
        # Some local LLMs return [{"skill":"Python"}, {"name":"Verilog"}].
        out = validate_tailoring({
            "skills_reordered": [{"skill": "Python"}, {"name": "Verilog"}, "FPGA"],
            "experience_bullets": [],
        })
        assert out["skills_reordered"] == ["Python", "Verilog", "FPGA"]

    def test_string_bullet_blob_is_split(self):
        # Some local LLMs return bullets as a single newline-joined string.
        out = validate_tailoring({
            "skills_reordered": ["Python"],
            "experience_bullets": [{"role": "Eng",
                                     "bullets": "first thing\nsecond thing"}],
        })
        assert out["experience_bullets"] == [
            {"role": "Eng", "bullets": ["first thing", "second thing"]}
        ]

    def test_wrong_type_for_top_level_is_rejected(self):
        # skills_reordered must be a list, not a string.
        assert validate_tailoring({"skills_reordered": "Python"}) is None

    def test_garbage_bullet_entry_is_dropped_not_failed(self):
        out = validate_tailoring({
            "skills_reordered": ["Python"],
            "experience_bullets": [
                {"role": "ok", "bullets": ["good"]},
                "not a dict",
                {"role": None, "bullets": None},
            ],
        })
        assert out["experience_bullets"] == [{"role": "ok", "bullets": ["good"]}]

    def test_drops_only_when_everything_unusable(self):
        # If every field is junk, validation rejects the whole thing.
        assert validate_tailoring({
            "skills_reordered":   "not a list",
            "experience_bullets": "also not a list",
        }) is None


# ── heuristic_tailor_resume ─────────────────────────────────────────────────

class TestHeuristic:
    def test_skills_reordered_no_fabrication(self):
        """Anti-fabrication invariant: missing JD skills never appear in
        skills_reordered."""
        job = {"requirements": ["FPGA", "Verilog", "Python"]}
        profile = {"top_hard_skills": ["Python", "MATLAB", "C++"]}
        out = heuristic_tailor_resume(job, profile, "")
        # Output is a permutation of the user's skills, nothing more.
        assert sorted(out["skills_reordered"]) == sorted(profile["top_hard_skills"])
        # FPGA / Verilog (asked for, not on resume) live in ats_keywords_missing.
        missing_lower = {m.lower() for m in out["ats_keywords_missing"]}
        assert "fpga" in missing_lower
        assert "verilog" in missing_lower

    def test_skills_matching_jd_surface_first(self):
        job = {"requirements": ["Verilog", "FPGA"]}
        profile = {"top_hard_skills": ["Python", "Verilog", "C++", "FPGA"]}
        out = heuristic_tailor_resume(job, profile, "")
        # Verilog and FPGA should both be ahead of Python and C++.
        idx = {s: i for i, s in enumerate(out["skills_reordered"])}
        assert idx["Verilog"] < idx["Python"]
        assert idx["FPGA"]    < idx["C++"]

    def test_bullets_reordered_within_role_no_invention(self):
        job = {"requirements": ["FPGA Verilog timing"]}
        profile = {
            "top_hard_skills": ["Verilog"],
            "experience": [{
                "title": "RA",
                "bullets": [
                    "Cleaned datasets in Python",
                    "Implemented FPGA Verilog timing closure",
                    "Wrote weekly reports",
                ],
            }],
        }
        out = heuristic_tailor_resume(job, profile, "")
        assert len(out["experience_bullets"]) == 1
        bullets = out["experience_bullets"][0]["bullets"]
        # Same set of bullets, just re-ordered: most JD-relevant first.
        original = set(profile["experience"][0]["bullets"])
        assert set(bullets) == original
        assert bullets[0].startswith("Implemented FPGA Verilog")

    def test_role_with_no_jd_overlap_not_emitted(self):
        # If a role has zero overlap with the JD AND reordering produced no
        # change, we skip emitting it — saves the renderer from showing
        # identical role blocks for unrelated jobs.
        job = {"requirements": ["FPGA", "Verilog"]}
        profile = {
            "top_hard_skills": [],
            "experience": [{
                "title": "Barista",
                "bullets": ["Made coffee", "Served customers"],
            }],
        }
        out = heuristic_tailor_resume(job, profile, "")
        assert out["experience_bullets"] == []

    def test_missing_keywords_consider_resume_text_too(self):
        # If the resume text mentions something that wasn't extracted as a
        # hard skill, it still counts as "present" for ats_keywords_missing.
        job = {"requirements": ["Kubernetes"]}
        profile = {"top_hard_skills": ["Python"]}
        text = "Built CI/CD on Kubernetes for the data team"
        out = heuristic_tailor_resume(job, profile, text)
        # Kubernetes is in resume_text → not flagged as missing.
        assert "Kubernetes" not in out["ats_keywords_missing"]


# ── merge_with_heuristic ────────────────────────────────────────────────────

class TestMerge:
    def test_llm_fills_provided_heuristic_fills_gaps(self):
        heuristic = {
            "skills_reordered":     ["A", "B"],
            "experience_bullets":   [{"role": "X", "bullets": ["heur bullet"]}],
            "ats_keywords_missing": ["Z"],
            "section_order":        ["Skills"],
        }
        llm = {
            "skills_reordered":     ["LLM-A"],
            "experience_bullets":   [],   # empty → heuristic wins
        }
        out = merge_with_heuristic(llm, heuristic)
        assert out["skills_reordered"]   == ["LLM-A"]              # LLM
        assert out["experience_bullets"] == heuristic["experience_bullets"]  # heuristic
        assert out["ats_keywords_missing"] == ["Z"]                # heuristic

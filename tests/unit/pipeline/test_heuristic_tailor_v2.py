"""Unit tests for heuristic_tailor_resume_v2 + validate_v2_or_none + merge_with_heuristic_v2."""
import pytest

from pipeline.heuristic_tailor import (
    heuristic_tailor_resume_v2, merge_with_heuristic_v2, validate_v2_or_none,
)
from pipeline.tailored_schema import SCHEMA_VERSION

pytestmark = pytest.mark.unit


def _profile():
    return {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "top_hard_skills": ["Python", "Verilog", "FPGA"],
        "experience": [{"title": "Intern", "company": "Acme", "dates": "2024",
                        "bullets": ["Built a thing", "Tested another"]}],
        "education": [{"degree": "BS EE", "institution": "Cal", "year": "2025"}],
    }


def _job():
    return {
        "title": "Hardware Engineer",
        "company": "FooCo",
        "requirements": ["Verilog", "AXI4", "FPGA verification"],
    }


def test_heuristic_v2_returns_complete_schema():
    out = heuristic_tailor_resume_v2(_job(), _profile(), "Jane Doe resume...")
    assert out["schema_version"] == SCHEMA_VERSION
    assert out["name"] == "Jane Doe"
    skill_texts = [it["text"] for cat in out["skills"] for it in cat["items"]]
    assert "Verilog" in skill_texts
    assert out["experience"][0]["bullets"]
    assert "AXI4" in out["ats_keywords_missing"]


def test_heuristic_v2_marks_added_when_keyword_selected():
    out = heuristic_tailor_resume_v2(
        _job(), _profile(), "resume text",
        selected_keywords=["AXI4"],
    )
    flat = [it["text"] for cat in out["skills"] for it in cat["items"]]
    assert "AXI4" in flat
    added = [it for cat in out["skills"] for it in cat["items"] if it["diff"] == "added"]
    assert any(it["text"] == "AXI4" for it in added)
    # AXI4 should NOT remain in ats_keywords_missing if user selected it
    assert "AXI4" not in out["ats_keywords_missing"]


def test_heuristic_v2_does_not_fabricate_bullets():
    out = heuristic_tailor_resume_v2(_job(), _profile(), "resume text")
    for role in out["experience"]:
        for b in role["bullets"]:
            assert b["diff"] == "unchanged"


def test_validate_v2_or_none_rejects_garbage():
    assert validate_v2_or_none(None) is None
    assert validate_v2_or_none({}) is None
    assert validate_v2_or_none({"name": "X"}) is None  # no body sections


def test_merge_with_heuristic_v2_keeps_llm_bullets():
    heuristic = heuristic_tailor_resume_v2(_job(), _profile(), "")
    llm = dict(heuristic)
    llm["experience"] = [{
        "title": "Intern", "company": "Acme", "dates": "2024", "location": "",
        "bullets": [{"text": "Modified bullet", "diff": "modified", "original": "Built a thing"}],
    }]
    merged = merge_with_heuristic_v2(llm, heuristic)
    assert merged["experience"][0]["bullets"][0]["text"] == "Modified bullet"
    assert merged["experience"][0]["bullets"][0]["diff"] == "modified"

"""Unit tests for pipeline.tailored_schema (TailoredResume v2)."""
import pytest

from pipeline.tailored_schema import (
    SCHEMA_VERSION, default_v2, legacy_to_v2, validate_v2,
)

pytestmark = pytest.mark.unit


def test_validate_v2_accepts_minimal():
    d = {
        "schema_version": SCHEMA_VERSION,
        "name": "Jane Doe",
        "skills": [{"name": "", "items": [{"text": "Python", "diff": "unchanged"}]}],
        "experience": [],
        "section_order": ["Skills"],
    }
    assert validate_v2(d) is not None


def test_validate_v2_rejects_missing_name():
    d = {"schema_version": SCHEMA_VERSION, "skills": [], "experience": []}
    assert validate_v2(d) is None


def test_validate_v2_rejects_unknown_diff_marker():
    d = {
        "schema_version": SCHEMA_VERSION,
        "name": "X",
        "skills": [{"name": "", "items": [{"text": "Foo", "diff": "garbage"}]}],
        "experience": [],
    }
    out = validate_v2(d)
    # Unknown diff coerced to "unchanged" (don't reject the whole resume for one bad marker)
    assert out is not None
    assert out["skills"][0]["items"][0]["diff"] == "unchanged"


def test_validate_v2_coerces_missing_diff_to_unchanged():
    d = {
        "schema_version": SCHEMA_VERSION,
        "name": "X",
        "skills": [{"name": "", "items": [{"text": "Foo"}]}],
        "experience": [],
    }
    out = validate_v2(d)
    assert out is not None
    assert out["skills"][0]["items"][0]["diff"] == "unchanged"


def test_legacy_to_v2_carries_skills_and_bullets():
    legacy = {
        "skills_reordered": ["Python", "FPGA"],
        "experience_bullets": [
            {"role": "Intern", "bullets": ["Built a thing", "Tested another"]},
        ],
        "ats_keywords_missing": ["AXI4"],
        "ats_score_before": 60,
        "ats_score_after": 78,
    }
    profile = {
        "name": "Jane",
        "experience": [{"title": "Intern", "company": "Acme", "dates": "2024", "bullets": []}],
        "education": [{"degree": "BS EE", "institution": "Cal", "year": "2025"}],
    }
    v2 = legacy_to_v2(legacy, profile)
    assert v2["schema_version"] == SCHEMA_VERSION
    assert v2["name"] == "Jane"
    assert len(v2["skills"]) >= 1
    assert v2["skills"][0]["items"][0]["text"] == "Python"
    assert v2["experience"][0]["bullets"][0]["text"] == "Built a thing"
    assert v2["ats_keywords_missing"] == ["AXI4"]
    assert v2["ats_score_before"] == 60
    assert v2["ats_score_after"] == 78


def test_default_v2_from_profile_has_every_section():
    profile = {
        "name": "Jane",
        "email": "j@example.com",
        "top_hard_skills": ["Python", "Verilog"],
        "experience": [{"title": "Intern", "company": "Acme", "dates": "2024",
                         "bullets": ["Did stuff"]}],
        "education": [{"degree": "BS", "institution": "Cal", "year": "2025"}],
        "projects": [{"name": "Foo", "description": "Did Foo"}],
    }
    v2 = default_v2(profile)
    assert v2["name"] == "Jane"
    assert v2["email"] == "j@example.com"
    assert v2["skills"][0]["items"][0]["text"] == "Python"
    assert v2["experience"][0]["bullets"][0]["diff"] == "unchanged"
    assert v2["section_order"] == ["Skills", "Experience", "Projects", "Education"]

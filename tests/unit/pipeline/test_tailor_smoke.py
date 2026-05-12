"""End-to-end smoke tests for the v2 tailoring pipeline.

Each test exercises the full _save_tailored_resume dispatch for a different
source format, asserting the full structured content survives the round-trip.
"""
from pathlib import Path

import pytest

from pipeline.heuristic_tailor import heuristic_tailor_resume_v2
from pipeline.resume import _save_tailored_resume

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "resumes"


def _profile_jane():
    return {
        "name": "Jane Doe", "email": "jane@example.com",
        "top_hard_skills": ["Python", "Verilog", "C++", "MATLAB"],
        "experience": [{"title": "Intern", "company": "Acme Corp", "dates": "2024",
                         "bullets": ["Built a thing for the team", "Tested another thing"]}],
        "education": [{"degree": "B.S. EE", "institution": "Cal", "year": "2025"}],
    }


def _job_hw():
    return {
        "company": "FooCo",
        "title": "HW Eng",
        "requirements": ["Verilog", "FPGA verification", "AXI4"],
    }


def test_smoke_tex_in_place(tmp_path):
    src = (FIXTURES / "jake_classic.tex").read_text(encoding="utf-8")
    profile = _profile_jane()
    tailored = heuristic_tailor_resume_v2(
        _job_hw(), profile, "",
        selected_keywords=["FPGA verification"],
    )
    out = _save_tailored_resume(
        _job_hw(), tailored, profile,
        latex_source=src, output_dir=tmp_path,
        source_format="tex",
    )
    assert out["tex"] is not None
    text = (tmp_path / out["tex"]).read_text(encoding="utf-8")
    assert "FPGA verification" in text
    assert "textcolor" in text  # diff highlight wrap
    assert out["template_id"] == "in_place_latex"


def test_smoke_docx_in_place(tmp_path):
    profile = _profile_jane()
    tailored = heuristic_tailor_resume_v2(
        _job_hw(), profile, "",
        selected_keywords=["AXI4"],
    )
    out = _save_tailored_resume(
        _job_hw(), tailored, profile,
        output_dir=tmp_path, source_format="docx",
        source_bytes_path=FIXTURES / "modern_sans.docx",
    )
    assert out["docx"] is not None
    docx_path = tmp_path / out["docx"]
    assert docx_path.exists()
    assert out["template_id"] == "in_place_docx"


def test_smoke_default_template_pdf(tmp_path):
    profile = _profile_jane()
    tailored = heuristic_tailor_resume_v2(_job_hw(), profile, "")
    out = _save_tailored_resume(
        _job_hw(), tailored, profile,
        output_dir=tmp_path, source_format="pdf",
        format_profile={"columns": 1, "body_font_size": 10},
    )
    assert out["html_preview"] is not None
    assert out["template_id"] in (
        "single_column_classic", "single_column_modern",
        "two_column_left", "two_column_right",
        "compact_tech", "academic_multipage",
    )
    html = (tmp_path / out["html_preview"]).read_text(encoding="utf-8")
    assert "Jane Doe" in html
    assert "Verilog" in html


def test_smoke_every_section_renders_in_html(tmp_path):
    """v2's biggest win: extra sections (Awards, Publications, etc.) survive
    the round-trip, where the legacy renderer silently dropped them."""
    profile = _profile_jane()
    tailored = heuristic_tailor_resume_v2(_job_hw(), profile, "")
    tailored["awards"] = [{
        "title":  {"text": "Dean's List", "diff": "unchanged"},
        "detail": {"text": "2024", "diff": "unchanged"},
        "bullets": [],
    }]
    tailored["publications"] = [{
        "title":  {"text": "FPGA Verification at Scale", "diff": "unchanged"},
        "detail": {"text": "IEEE ICCAD 2024", "diff": "unchanged"},
        "bullets": [],
    }]
    tailored["section_order"] = [
        "Skills", "Experience", "Awards", "Publications", "Education",
    ]
    out = _save_tailored_resume(
        _job_hw(), tailored, profile,
        output_dir=tmp_path, source_format="pdf",
    )
    html = (tmp_path / out["html_preview"]).read_text(encoding="utf-8")
    assert "Dean's List" in html
    assert "FPGA Verification at Scale" in html
    assert "Awards" in html
    assert "Publications" in html


def test_smoke_legacy_dict_falls_through(tmp_path):
    """Legacy v1 dicts (no schema_version) must keep rendering — the dispatch
    runs them through legacy_to_v2 and the default template."""
    profile = _profile_jane()
    legacy = {
        "skills_reordered": ["Verilog", "Python"],
        "experience_bullets": [
            {"role": "Intern", "bullets": ["Built", "Tested"]},
        ],
        "ats_keywords_missing": ["AXI4"],
        "section_order": ["Skills", "Experience"],
    }
    out = _save_tailored_resume(
        _job_hw(), legacy, profile,
        output_dir=tmp_path, source_format=None,
    )
    assert out["html_preview"] is not None
    html = (tmp_path / out["html_preview"]).read_text(encoding="utf-8")
    assert "Verilog" in html

"""Unit tests for pipeline.docx_tailor."""
from pathlib import Path

import pytest

from pipeline.docx_tailor import tailor_docx_in_place
from pipeline.tailored_schema import default_v2

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "resumes" / "modern_sans.docx"
)


def _v2():
    profile = {
        "name": "Jane Doe",
        "top_hard_skills": ["Python", "Verilog", "C++", "MATLAB"],
        "experience": [
            {"title": "Intern", "company": "Acme Corp", "dates": "2024",
             "bullets": ["Built a thing for the team", "Tested another thing"]},
            {"title": "Research Assistant", "company": "Cal Photonics Lab", "dates": "2023",
             "bullets": ["Aligned an interferometer"]},
        ],
        "education": [{"degree": "B.S. Electrical Engineering",
                        "institution": "University of California, Berkeley", "year": "2025"}],
    }
    v2 = default_v2(profile)
    v2["experience"][0]["bullets"][0] = {
        "text": "Built a Verilog testbench for the team",
        "original": "Built a thing for the team",
        "diff": "modified",
    }
    v2["skills"][0]["items"].append({"text": "FPGA verification", "diff": "added"})
    v2["experience"][0]["bullets"].append({
        "text": "Wrote AXI4 transaction generators",
        "original": "",
        "diff": "added",
    })
    return v2


def test_docx_in_place_replaces_modified_bullet(tmp_path):
    out = tailor_docx_in_place(_v2(), source_path=FIXTURE, base="resume", out_dir=tmp_path)
    assert out["docx"] == "resume.docx"
    docx_path = tmp_path / "resume.docx"
    assert docx_path.exists()
    from docx import Document
    doc = Document(str(docx_path))
    texts = [p.text for p in doc.paragraphs]
    assert any("Verilog testbench" in t for t in texts)
    assert any("Aligned an interferometer" in t for t in texts)


def test_docx_in_place_appends_added_bullet(tmp_path):
    tailor_docx_in_place(_v2(), source_path=FIXTURE, base="resume", out_dir=tmp_path)
    from docx import Document
    doc = Document(str(tmp_path / "resume.docx"))
    texts = [p.text for p in doc.paragraphs]
    assert any("AXI4 transaction generators" in t for t in texts)


def test_docx_in_place_returns_html_preview(tmp_path):
    out = tailor_docx_in_place(_v2(), source_path=FIXTURE, base="resume", out_dir=tmp_path)
    assert out["html_preview"] is not None
    assert (tmp_path / out["html_preview"]).exists()


def test_docx_in_place_template_id_is_in_place_docx(tmp_path):
    out = tailor_docx_in_place(_v2(), source_path=FIXTURE, base="resume", out_dir=tmp_path)
    assert out["template_id"] == "in_place_docx"
    assert out["template_confidence"] == 1.0

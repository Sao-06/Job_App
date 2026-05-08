"""Unit tests for pipeline.latex_tailor."""
from pathlib import Path

import pytest

from pipeline.latex_tailor import tailor_latex_in_place
from pipeline.tailored_schema import default_v2

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "resumes" / "jake_classic.tex"
)


def _v2():
    profile = {
        "name": "Jane Doe", "email": "jane@example.com",
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
    return v2


def test_in_place_writes_tex_with_modified_bullet(tmp_path):
    src = FIXTURE.read_text(encoding="utf-8")
    out = tailor_latex_in_place(_v2(), latex_source=src, base="resume", out_dir=tmp_path)
    assert out["tex"] == "resume.tex"
    text = (tmp_path / "resume.tex").read_text(encoding="utf-8")
    assert "Verilog testbench" in text
    assert "textcolor" in text
    assert r"\usepackage{xcolor}" in text


def test_in_place_preserves_unchanged_bullet(tmp_path):
    src = FIXTURE.read_text(encoding="utf-8")
    tailor_latex_in_place(_v2(), latex_source=src, base="resume", out_dir=tmp_path)
    text = (tmp_path / "resume.tex").read_text(encoding="utf-8")
    assert "Aligned an interferometer" in text


def test_in_place_appends_added_skill(tmp_path):
    src = FIXTURE.read_text(encoding="utf-8")
    tailor_latex_in_place(_v2(), latex_source=src, base="resume", out_dir=tmp_path)
    text = (tmp_path / "resume.tex").read_text(encoding="utf-8")
    assert "FPGA verification" in text


def test_in_place_returns_html_preview(tmp_path):
    src = FIXTURE.read_text(encoding="utf-8")
    out = tailor_latex_in_place(_v2(), latex_source=src, base="resume", out_dir=tmp_path)
    assert out["html_preview"] is not None
    assert (tmp_path / out["html_preview"]).exists()


def test_in_place_template_id_is_in_place_latex(tmp_path):
    src = FIXTURE.read_text(encoding="utf-8")
    out = tailor_latex_in_place(_v2(), latex_source=src, base="resume", out_dir=tmp_path)
    assert out["template_id"] == "in_place_latex"
    assert out["template_confidence"] == 1.0

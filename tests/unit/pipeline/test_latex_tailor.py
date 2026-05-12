"""Unit tests for pipeline.latex_tailor."""
from pathlib import Path

import pytest

from pipeline.latex_tailor import tailor_latex_in_place
from tests.fakes import jane_doe_tailored_v2 as _v2

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "resumes" / "jake_classic.tex"
)


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


def test_in_place_clean_latex_has_no_textcolor(tmp_path):
    """The {base}_final.tex (clean variant) must NOT contain the green
    \\textcolor wrappers — that file is what compiles to the all-black PDF
    the user attaches to applications. Diff highlights belong only in the
    in-page preview, not the downloaded artifact."""
    src = FIXTURE.read_text(encoding="utf-8")
    out = tailor_latex_in_place(_v2(), latex_source=src, base="resume", out_dir=tmp_path)
    # Clean variant should be present + unmodified-bullet-text-included
    assert out.get("tex_final") == "resume_final.tex"
    final_text = (tmp_path / out["tex_final"]).read_text(encoding="utf-8")
    assert "Verilog testbench" in final_text       # modified bullet still substituted
    assert "FPGA verification" in final_text       # added skill still appended
    assert "textcolor" not in final_text           # but no green wrapping
    assert r"\usepackage{xcolor}" not in final_text  # and no xcolor injection

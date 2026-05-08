"""Unit tests for pipeline.template_render."""
import pytest

from pipeline.tailored_schema import default_v2
from pipeline.template_render import list_templates, render_html, render_pdf

pytestmark = pytest.mark.unit


def _profile():
    return {
        "name": "Jane Doe",
        "email": "j@example.com",
        "top_hard_skills": ["Python", "Verilog"],
        "experience": [{"title": "Intern", "company": "Acme", "dates": "2024",
                         "bullets": ["Built it", "Tested it"]}],
        "education": [{"degree": "BS EE", "institution": "Cal", "year": "2025"}],
    }


def test_list_templates_returns_six():
    ids = list_templates()
    assert len(ids) == 6
    assert "single_column_classic" in ids


def test_render_html_contains_name_and_section():
    t = default_v2(_profile())
    html = render_html(t, "single_column_classic", format_profile={})
    assert "Jane Doe" in html
    assert "Skills" in html
    assert "Experience" in html


def test_render_html_emits_diff_marks_when_present():
    t = default_v2(_profile())
    t["skills"] = [{"name": "", "items": [
        {"text": "Python", "diff": "unchanged"},
        {"text": "Rust", "diff": "added"},
    ]}]
    html = render_html(t, "single_column_classic", format_profile={})
    assert 'class="diff-add"' in html


def test_render_html_unknown_template_falls_back():
    t = default_v2(_profile())
    html = render_html(t, "nonexistent_template", format_profile={})
    assert "Jane Doe" in html


def test_render_pdf_writes_a_file(tmp_path):
    t = default_v2(_profile())
    html = render_html(t, "single_column_classic", format_profile={})
    out = tmp_path / "resume.pdf"
    ok = render_pdf(html, out)
    if not ok:
        pytest.skip("No PDF backend available (WeasyPrint + reportlab fallback both failed)")
    assert out.exists()
    assert out.stat().st_size > 500

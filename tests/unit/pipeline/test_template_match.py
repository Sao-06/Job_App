"""Unit tests for pipeline.template_match."""
import pytest

from pipeline.template_match import pick_template

pytestmark = pytest.mark.unit


def test_pick_template_two_column_when_columns_2():
    tid, conf = pick_template({"columns": 2, "body_font_size": 10}, "")
    assert tid in ("two_column_left", "two_column_right")
    assert conf > 0.4


def test_pick_template_compact_when_small_font():
    tid, _ = pick_template({"columns": 1, "body_font_size": 8.5}, "")
    assert tid == "compact_tech"


def test_pick_template_academic_when_publications_dense():
    text = ("DOI: 10.1234/abc " * 10) + "et al. " * 8
    tid, _ = pick_template({"columns": 1, "body_font_size": 10}, text)
    assert tid == "academic_multipage"


def test_pick_template_classic_default():
    tid, _ = pick_template({}, "")
    assert tid == "single_column_classic"


def test_pick_template_modern_when_accent_chromatic():
    tid, _ = pick_template(
        {"columns": 1, "accent_color": "#5e6ad2", "body_font_size": 10.5}, "",
    )
    assert tid == "single_column_modern"


def test_pick_template_handles_None_format_profile():
    tid, conf = pick_template(None, "")
    assert tid == "single_column_classic"
    assert 0 <= conf <= 1

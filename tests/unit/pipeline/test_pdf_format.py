"""Unit tests for the layout-fingerprint helpers in pipeline.pdf_format.

The pdfplumber pass that produces real fingerprints needs an on-disk PDF
and is exercised end-to-end via integration testing.  These tests target
the pure helpers (_normalize_color, _detect_columns) where it's cheap to
pin behaviour and lock in the heuristic thresholds against accidental
regression.
"""
import pytest

from pipeline.pdf_format import _normalize_color, _detect_columns

pytestmark = pytest.mark.unit


class TestNormalizeColor:
    def test_none_returns_none(self):
        assert _normalize_color(None) is None

    def test_near_black_dropped(self):
        # PDF body text is almost always near-black; we don't want it
        # nominated as the "accent" color.
        assert _normalize_color((0.0, 0.0, 0.0)) is None
        assert _normalize_color((0.05, 0.05, 0.05)) is None

    def test_near_white_dropped(self):
        assert _normalize_color((1.0, 1.0, 1.0)) is None
        assert _normalize_color((0.97, 0.97, 0.97)) is None

    def test_grayscale_dropped(self):
        # Mid-grey is still chromatically neutral — accent must have hue.
        assert _normalize_color((0.5, 0.5, 0.5)) is None

    def test_chromatic_color_kept(self):
        out = _normalize_color((0.12, 0.31, 0.47))   # navy
        assert isinstance(out, str)
        assert out.startswith("#")
        assert len(out) == 7

    def test_int_rgb_input(self):
        # Some pdfplumber backends hand back ints already in 0..255.
        out = _normalize_color((124, 92, 255))
        assert out == "#7c5cff"


class TestDetectColumns:
    def test_no_chars_is_one_column(self):
        assert _detect_columns([], 0) == 1

    def test_below_minimum_chars_is_one_column(self):
        # Even if the small set has two clusters, we don't trust it.
        xs = [50.0] * 10 + [400.0] * 10
        assert _detect_columns(xs, 20) == 1

    def test_single_cluster_is_one_column(self):
        xs = [50.0 + (i % 3) for i in range(200)]
        assert _detect_columns(xs, 200) == 1

    def test_two_clusters_with_gap_and_share(self):
        # Two well-separated clusters with the smaller one holding ~30%
        # of the characters → two-column layout.
        left  = [50.0  + (i % 5) for i in range(140)]
        right = [400.0 + (i % 5) for i in range(60)]
        xs = left + right
        assert _detect_columns(xs, len(xs)) == 2

    def test_indented_block_does_not_become_column(self):
        # A dozen indented bullets at x=120 alongside body at x=50 — the
        # secondary cluster's share is below the threshold so we stay
        # single-column.
        body    = [50.0]  * 200
        indents = [120.0] * 12
        xs = body + indents
        assert _detect_columns(xs, len(xs)) == 1

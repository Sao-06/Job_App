"""
pipeline/template_match.py
──────────────────────────
Score a `format_profile` (and resume_text) against the 6 HTML/CSS
templates. Returns the best (template_id, confidence) pair.

The format_profile dict comes from pipeline.pdf_format.detect_format_profile
and is also produced when the user uploads non-PDF formats with reasonable
defaults. Recognized keys:
  columns:           1 | 2
  body_font_size:    float
  header_font_size:  float
  accent_color:      "#rrggbb"
"""

from __future__ import annotations

import re

_TEMPLATE_IDS = [
    "single_column_classic",
    "single_column_modern",
    "two_column_left",
    "two_column_right",
    "compact_tech",
    "academic_multipage",
]

_DOI_RE = re.compile(r"\bdoi[:\s]\s*10\.\d{4,9}/", re.I)
_ETAL_RE = re.compile(r"\bet\s+al\.\b", re.I)


def _is_chromatic(hex_color: str | None) -> bool:
    if not isinstance(hex_color, str) or not hex_color.startswith("#") or len(hex_color) != 7:
        return False
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
    except ValueError:
        return False
    return (max(r, g, b) - min(r, g, b)) > 30 and max(r, g, b) > 60


def pick_template(
    format_profile: dict | None, resume_text: str = "",
) -> tuple[str, float]:
    """Score templates against a format fingerprint. Returns
    ``(template_id, confidence in [0,1])``. Confidence < 0.5 → caller
    should surface a "best-effort match" hint to the user."""
    fp = format_profile or {}
    text = resume_text or ""

    columns = int(fp.get("columns") or 1)
    body_size = float(fp.get("body_font_size") or 10)
    accent = fp.get("accent_color")
    chromatic = _is_chromatic(accent if isinstance(accent, str) else None)

    pubs = len(_DOI_RE.findall(text)) + len(_ETAL_RE.findall(text))
    is_academic = pubs >= 6 and columns == 1

    scores: dict[str, float] = {tid: 0.0 for tid in _TEMPLATE_IDS}

    # Column dimension dominates
    if columns == 2:
        scores["two_column_left"] += 0.50
        scores["two_column_right"] += 0.45
    else:
        scores["single_column_classic"] += 0.30
        scores["single_column_modern"] += 0.25
        scores["academic_multipage"] += 0.20
        scores["compact_tech"] += 0.20

    # Compact when body font is small
    if body_size < 9.5:
        scores["compact_tech"] += 0.45

    # Modern when there's a chromatic accent
    if chromatic:
        scores["single_column_modern"] += 0.30
        scores["compact_tech"] += 0.05

    # Classic when no accent and serif-y body size, single-column
    if not chromatic and 9.5 <= body_size <= 11.5 and columns == 1:
        scores["single_column_classic"] += 0.20

    # Academic when publications + DOIs are dense
    if is_academic:
        scores["academic_multipage"] += 0.55

    best_id = max(scores, key=scores.get)
    return best_id, min(1.0, scores[best_id])

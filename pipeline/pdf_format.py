"""
pipeline/pdf_format.py
──────────────────────
Lightweight layout-fingerprint extractor.

When the user uploads a PDF resume, we run a single ``pdfplumber`` pass at
upload time and cache four hints on the resume record:

  columns:           1 | 2  — column count detected from x-position clusters
  body_font_size:    int    — modal font size (px) of body text
  header_font_size:  int    — typical font size of section headings
  accent_color:      "#rrggbb" or None — dominant non-black text color

These hints are passed to ``_render_resume_pdf_reportlab`` so the
generated preview / tailored output mirrors the source's *visual
aesthetic* — same column count, scale, accent — without trying to
byte-edit the original PDF (which is brittle and font-fragile).

Failure mode: every step is wrapped in a try/except so a malformed PDF
never breaks upload.  Returns ``{}`` instead of raising — the renderer
treats missing keys as "use defaults".
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path


# Heuristic thresholds.  Numbers chosen by eyeballing typical resume PDFs;
# tune cautiously — over-tightening produces "1 column" everywhere; loose
# values flag every two-line address as a second column.
_TWO_COLUMN_GAP_PX = 60.0   # min px gap between left edges of two clusters
_TWO_COLUMN_MIN_RATIO = 0.18  # min share of text the second cluster must hold
_BODY_SIZE_MIN = 7.0
_BODY_SIZE_MAX = 14.0
_HEADER_SIZE_MIN_RATIO = 1.15  # header must be ≥ 1.15× body to count


def _normalize_color(c) -> str | None:
    """pdfplumber returns colors in a variety of shapes — RGB tuple, single
    grayscale float, or sometimes ``None``.  Reduce to ``#rrggbb`` or
    ``None`` (when the color is effectively black / near-black)."""
    if c is None:
        return None
    if isinstance(c, (int, float)):
        # Grayscale 0..1; keep as None unless it's clearly mid-tone.
        v = max(0.0, min(1.0, float(c)))
        if 0.05 < v < 0.95:
            n = int(v * 255)
            return f"#{n:02x}{n:02x}{n:02x}"
        return None
    try:
        if len(c) >= 3:
            r, g, b = c[0], c[1], c[2]
            # Convert 0..1 floats → 0..255 ints; tolerate already-int.
            def _to_int(x):
                return int(round(float(x) * 255)) if 0 <= float(x) <= 1 else int(x)
            ri, gi, bi = _to_int(r), _to_int(g), _to_int(b)
            ri = max(0, min(255, ri))
            gi = max(0, min(255, gi))
            bi = max(0, min(255, bi))
            # Skip near-black & near-white — those aren't accents.
            mx = max(ri, gi, bi)
            mn = min(ri, gi, bi)
            if mx <= 30:                # near-black
                return None
            if mn >= 225 and mx >= 240: # near-white
                return None
            # Skip near-grayscale: an accent has hue.
            if mx - mn < 20:
                return None
            return f"#{ri:02x}{gi:02x}{bi:02x}"
    except (TypeError, ValueError, IndexError):
        return None
    return None


def _detect_columns(x_lefts: list[float], total_chars: int) -> int:
    """Return 1 or 2 based on x-coordinate clustering of character left
    edges.  Two columns require: (a) a noticeable gap between the two
    densest clusters, and (b) the smaller cluster holds enough characters
    that we're sure it isn't just an indented block."""
    if not x_lefts or total_chars < 40:
        return 1
    # Bucket left edges into 10-pixel bins.
    buckets: Counter[int] = Counter()
    for x in x_lefts:
        buckets[int(x // 10) * 10] += 1
    if len(buckets) < 2:
        return 1
    # Find the two most-populated bin centers (must be at least
    # _TWO_COLUMN_GAP_PX apart) and check their share.
    items = sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)
    primary_x, primary_n = items[0]
    secondary = next(
        (it for it in items[1:] if abs(it[0] - primary_x) >= _TWO_COLUMN_GAP_PX),
        None,
    )
    if secondary is None:
        return 1
    secondary_x, secondary_n = secondary
    if secondary_n / total_chars < _TWO_COLUMN_MIN_RATIO:
        return 1
    return 2


def detect_format_profile(pdf_path: Path) -> dict:
    """Best-effort layout fingerprint of a PDF resume.

    Returns at most these four keys (all optional):
      ``columns``, ``body_font_size``, ``header_font_size``, ``accent_color``.

    Always returns a dict, even on failure — never raises.
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        return {}

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            sizes: list[float] = []
            x_lefts: list[float] = []
            colors: Counter[str] = Counter()
            char_total = 0
            for page in pdf.pages[:3]:   # cap pages — resumes are usually 1-2
                # ``page.chars`` is the per-character record list; richer than
                # extract_words() for our purposes (we want sizes and colors).
                for ch in (page.chars or []):
                    char_total += 1
                    sz = ch.get("size")
                    if isinstance(sz, (int, float)) and _BODY_SIZE_MIN <= sz <= 60:
                        sizes.append(float(sz))
                    x0 = ch.get("x0")
                    if isinstance(x0, (int, float)):
                        x_lefts.append(float(x0))
                    color = _normalize_color(ch.get("non_stroking_color"))
                    if color:
                        colors[color] += 1
                if char_total > 4000:
                    break

        out: dict = {}

        # ── Body font size: modal value among "body-ish" sizes ────────────
        if sizes:
            body_pool = [s for s in sizes if _BODY_SIZE_MIN <= s <= _BODY_SIZE_MAX]
            if body_pool:
                # Round to nearest 0.5pt then take mode for stability.
                rounded = [round(s * 2) / 2 for s in body_pool]
                modal_body = Counter(rounded).most_common(1)[0][0]
                out["body_font_size"] = float(modal_body)

                # ── Header font size: median of sizes ≥ 1.15× body, capped 24pt
                header_pool = [
                    s for s in sizes
                    if s >= modal_body * _HEADER_SIZE_MIN_RATIO and s <= 24
                ]
                if header_pool:
                    header_pool.sort()
                    median = header_pool[len(header_pool) // 2]
                    out["header_font_size"] = float(round(median, 1))

        # ── Columns ───────────────────────────────────────────────────────
        out["columns"] = _detect_columns(x_lefts, char_total)

        # ── Accent color: most-common chromatic text color ───────────────
        if colors:
            top_color, _ = colors.most_common(1)[0]
            out["accent_color"] = top_color

        return out

    except Exception:
        # Any pdfplumber/PDF parse failure → silently fall back to defaults.
        return {}

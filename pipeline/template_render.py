"""
pipeline/template_render.py
───────────────────────────
Renders a TailoredResume v2 → HTML (via Jinja2) → PDF (WeasyPrint, with
reportlab fallback). The HTML produced is also served back to the SPA
preview so the in-page green highlights match the downloaded PDF.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import console

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_SHARED = _TEMPLATES_DIR / "_shared"

_TEMPLATE_IDS = [
    "single_column_classic",
    "single_column_modern",
    "two_column_left",
    "two_column_right",
    "compact_tech",
    "academic_multipage",
]


def list_templates() -> list[str]:
    """Public list of template ids the matcher can return."""
    return list(_TEMPLATE_IDS)


def _load_jinja_env():
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError as e:
        raise RuntimeError("jinja2 not installed. pip install jinja2.") from e
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "htm", "xml"]),
        trim_blocks=False, lstrip_blocks=False,
    )


def _read_shared_css() -> tuple[str, str]:
    base = (_SHARED / "base.css").read_text(encoding="utf-8")
    diff = (_SHARED / "diff.css").read_text(encoding="utf-8")
    return base, diff


def render_html(
    tailored: dict,
    template_id: str,
    format_profile: dict | None = None,
) -> str:
    """Render the TailoredResume v2 into a self-contained HTML string."""
    if template_id not in _TEMPLATE_IDS:
        template_id = "single_column_classic"
    env = _load_jinja_env()
    tmpl = env.get_template(f"{template_id}.html.j2")
    base_css, diff_css = _read_shared_css()
    fp = format_profile or {}
    body_size = float(fp.get("body_font_size") or 10)
    body_size = max(8.5, min(12.5, body_size))
    header_size = float(fp.get("header_font_size") or (body_size + 1.5))
    header_size = max(body_size + 0.5, min(14.0, header_size))
    name_size = round(min(24, header_size + 7.5))
    accent = fp.get("accent_color")
    if not (isinstance(accent, str) and accent.startswith("#") and len(accent) == 7):
        accent = None
    return tmpl.render(
        t=tailored,
        base_css=base_css,
        diff_css=diff_css,
        body_size=body_size,
        header_size=header_size,
        name_size=name_size,
        accent=accent,
    )


def render_pdf(html: str, output_path: Path) -> bool:
    """Render an HTML string to a PDF file. Tries WeasyPrint first, then
    falls back to a reportlab-based plaintext render. Returns True on success."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if _render_pdf_weasyprint(html, output_path):
        return True
    console.print("  [yellow]WeasyPrint unavailable — falling back to reportlab text render.[/yellow]")
    return _render_pdf_reportlab_fallback(html, output_path)


def _render_pdf_weasyprint(html: str, output_path: Path) -> bool:
    try:
        from weasyprint import HTML  # type: ignore
    except Exception as e:  # ImportError, OSError (Windows GTK)
        console.print(f"  [yellow]WeasyPrint not available: {type(e).__name__}: {e}[/yellow]")
        return False
    try:
        HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf(target=str(output_path))
        return output_path.exists()
    except Exception as e:
        console.print(f"  [yellow]WeasyPrint render error: {e}[/yellow]")
        return False


def _render_pdf_reportlab_fallback(html: str, output_path: Path) -> bool:
    """Last-ditch: strip HTML and render the plaintext via reportlab. The
    user gets a usable PDF even if WeasyPrint can't install. Loses layout
    polish but green highlights survive as colored runs."""
    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError:
        return False

    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<head[^>]*>.*?</head>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<mark class="diff-add">(.*?)</mark>',
                  r'<font color="#0a662c"><b>\1</b></font>', text, flags=re.DOTALL)
    text = re.sub(r'<mark class="diff-mod">(.*?)</mark>',
                  r'<font color="#0a662c">\1</font>', text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "<br/>", text)
    text = re.sub(r"</?(html|body|div|main|aside|section|header|footer)[^>]*>",
                  "", text, flags=re.IGNORECASE)
    text = re.sub(r"<h1[^>]*>(.*?)</h1>", r"\n<H1MARK>\1</H1MARK>\n", text, flags=re.DOTALL)
    text = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n<H2MARK>\1</H2MARK>\n", text, flags=re.DOTALL)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"  • \1<br/>", text, flags=re.DOTALL)
    text = re.sub(r"</?(ul|ol)[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(em|i|b|strong)[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<(?!/?(?:font|br))[^>]+>", "", text)
    text = text.replace("<H1MARK>", "").replace("</H1MARK>", "")
    text = text.replace("<H2MARK>", "").replace("</H2MARK>", "")

    styles = getSampleStyleSheet()
    body_style = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=12)
    story = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        try:
            story.append(Paragraph(para, body_style))
            story.append(Spacer(1, 2))
        except Exception:
            continue
    try:
        SimpleDocTemplate(
            str(output_path), pagesize=LETTER,
            leftMargin=0.7 * inch, rightMargin=0.7 * inch,
            topMargin=0.55 * inch, bottomMargin=0.55 * inch,
        ).build(story)
        return output_path.exists()
    except Exception as e:
        console.print(f"  [yellow]reportlab fallback error: {e}[/yellow]")
        return False

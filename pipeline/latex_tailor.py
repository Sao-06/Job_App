"""
pipeline/latex_tailor.py
────────────────────────
In-place LaTeX rewriter. Preserves the user's original .tex template
verbatim; only swaps text inside Skills, bullet items, and adds new bullets
for diff='added' nodes.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import console
from .latex import _sanitize_latex_source, compile_latex_to_pdf
from .template_render import render_html


def _latex_escape(s: str) -> str:
    if not s:
        return ""
    return (
        str(s)
        .replace("\\", r"\textbackslash{}")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("$", r"\$")
        .replace("#", r"\#")
        .replace("_", r"\_")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("~", r"\textasciitilde{}")
        .replace("^", r"\textasciicircum{}")
    )


def _wrap_diff(text: str, diff: str, *, clean: bool = False) -> str:
    """Escape text and (when not clean) wrap added/modified runs in a green
    \\textcolor macro for visual diffing. ``clean=True`` skips the color
    wrap so the produced .tex compiles to an all-black "final" PDF."""
    esc = _latex_escape(text)
    if clean:
        return esc
    if diff in ("added", "modified"):
        return r"\textcolor{green!50!black}{" + esc + "}"
    return esc


def _ensure_xcolor(latex: str) -> str:
    if r"\usepackage{xcolor}" in latex:
        return latex
    if "xcolor" in latex and r"\usepackage[" in latex:
        return latex
    if r"\begin{document}" in latex:
        return latex.replace(
            r"\begin{document}",
            "\\usepackage{xcolor}\n\\begin{document}",
        )
    return "\\usepackage{xcolor}\n" + latex


_SECTION_RE = re.compile(
    r"(\\section\*?\{(?P<head>[^}]+)\})(?P<body>.*?)(?=\\section\*?\{|\\end\{document\})",
    re.DOTALL,
)


def _replace_skills_section(latex: str, tailored: dict, *, clean: bool = False) -> str:
    cats = tailored.get("skills") or []
    if not cats:
        return latex
    flat: list[tuple[str, str]] = []
    for cat in cats:
        for it in cat.get("items", []):
            flat.append((it.get("text") or "", it.get("diff") or "unchanged"))
    if not flat:
        return latex
    rendered = ", ".join(_wrap_diff(t, d, clean=clean) for t, d in flat)

    def _sub(m):
        head_text = (m.group("head") or "").lower()
        if any(k in head_text for k in ("skill", "competenc")):
            return m.group(1) + "\n" + rendered + "\n\n"
        return m.group(0)

    return _SECTION_RE.sub(_sub, latex)


_ITEMIZE_RE = re.compile(
    r"(\\begin\{itemize\})(?P<body>.*?)(\\end\{itemize\})",
    re.DOTALL,
)


def _replace_experience_bullets(latex: str, tailored: dict, *, clean: bool = False) -> str:
    """Match each itemize block to a role by the preceding \\textbf{...}
    header. Replace its bullets with the role's tailored bullets in order.
    Roles whose header isn't found are ignored (we don't add new role blocks)."""
    roles = tailored.get("experience") or []
    if not roles:
        return latex

    def _matches_role(header_text: str, role: dict) -> bool:
        ht = header_text.lower()
        title = (role.get("title") or "").lower()
        company = (role.get("company") or "").lower()
        return bool((title and title in ht) or (company and company in ht))

    out_chunks: list[str] = []
    cursor = 0
    for m in _ITEMIZE_RE.finditer(latex):
        out_chunks.append(latex[cursor:m.start()])
        prefix = latex[max(0, m.start() - 600):m.start()]
        # Find the LAST \textbf{...} before this itemize block
        all_headers = list(re.finditer(r"\\textbf\{([^}]+)\}", prefix))
        header_text = all_headers[-1].group(1) if all_headers else ""
        role = next((r for r in roles if _matches_role(header_text, r)), None)
        if role:
            new_items: list[str] = []
            for b in role.get("bullets") or []:
                wrapped = _wrap_diff(
                    b.get("text") or "", b.get("diff") or "unchanged", clean=clean,
                )
                new_items.append(f"\\item {wrapped}")
            new_body = "\n" + "\n".join(new_items) + "\n"
            out_chunks.append("\\begin{itemize}" + new_body + "\\end{itemize}")
        else:
            out_chunks.append(m.group(0))
        cursor = m.end()
    out_chunks.append(latex[cursor:])
    return "".join(out_chunks)


def _strip_summary(latex: str) -> str:
    pattern = (
        r"\\section\*?\{(?:Summary|Objective|Professional Summary|Career Objective)\}"
        r".*?(?=\\section|\\end\{document\})"
    )
    return re.sub(pattern, "", latex, flags=re.IGNORECASE | re.DOTALL)


def _build_latex(latex_source: str, tailored: dict, *, clean: bool) -> str:
    """Apply tailoring to a LaTeX source. ``clean=True`` skips the green
    \\textcolor wrapping (and the matching xcolor injection) so the produced
    .tex compiles to all-black body text — used for the final-version PDF."""
    src = latex_source if clean else _ensure_xcolor(latex_source)
    src = _strip_summary(src)
    src = _replace_skills_section(src, tailored, clean=clean)
    src = _replace_experience_bullets(src, tailored, clean=clean)
    return _sanitize_latex_source(src)


def tailor_latex_in_place(
    tailored: dict, latex_source: str, base: str, out_dir: Path,
    format_profile: dict | None = None,
) -> dict:
    """Apply tailoring to a LaTeX source in place.

    Outputs:
      {base}.tex             — rewritten source with green diff wrappers
      {base}.pdf             — pdflatex render of the diff source (when available)
      {base}_final.tex       — clean source (no \\textcolor wrappers)
      {base}_final.pdf       — pdflatex render of the clean source
      {base}_preview.html    — HTML preview for the SPA iframe
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    diff_src = _build_latex(latex_source, tailored, clean=False)
    tex_path = out_dir / (base + ".tex")
    tex_path.write_text(diff_src, encoding="utf-8")
    console.print(f"  [cyan]LaTeX saved (in-place) -> {tex_path.name}[/cyan]")
    pdf_path = out_dir / (base + ".pdf")
    pdf_ok = compile_latex_to_pdf(diff_src, pdf_path)

    clean_src = _build_latex(latex_source, tailored, clean=True)
    final_tex_path = out_dir / (base + "_final.tex")
    final_tex_path.write_text(clean_src, encoding="utf-8")
    final_pdf_path = out_dir / (base + "_final.pdf")
    final_pdf_ok = compile_latex_to_pdf(clean_src, final_pdf_path)
    if final_pdf_ok:
        console.print(f"  [green]Clean LaTeX PDF saved -> {final_pdf_path.name}[/green]")

    html = render_html(tailored, "single_column_classic", format_profile=format_profile)
    html_path = out_dir / (base + "_preview.html")
    html_path.write_text(html, encoding="utf-8")

    return {
        "tex": tex_path.name,
        "tex_final": final_tex_path.name,
        "pdf": pdf_path.name if pdf_ok else None,
        "pdf_final": final_pdf_path.name if final_pdf_ok else None,
        "docx": None,
        "html_preview": html_path.name,
        "base": base,
        "template_id": "in_place_latex",
        "template_confidence": 1.0,
    }

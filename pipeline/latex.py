"""
pipeline/latex.py
─────────────────
LaTeX detection, plain-text conversion, resume tailoring, and PDF compilation.
No dependencies on other pipeline modules.
"""

import re
import subprocess
import shutil
import tempfile
from pathlib import Path

from .config import console


def detect_latex(text: str) -> bool:
    """Return True if *text* appears to be LaTeX source."""
    if any(text.lstrip().startswith(prefix) for prefix in (
        r"\documentclass", r"\begin{document}"
    )):
        return True
    indicators = [
        r"\section{", r"\subsection{", r"\textbf{",
        r"\cventry", r"\resumeItem", r"\resumeSubheading",
    ]
    if any(ind in text for ind in indicators):
        return True
    if text.count("\\") >= 3:
        return True
    return False


def latex_to_plaintext(latex: str) -> str:
    """Strip LaTeX markup and return clean plain text suitable for LLM parsing."""
    text = latex

    # Remove preamble
    doc_start = text.find(r"\begin{document}")
    if doc_start != -1:
        text = text[doc_start + len(r"\begin{document}"):]
    text = text.replace(r"\end{document}", "")

    # Remove comments
    text = re.sub(r"%.*", "", text)

    # Unwrap formatting commands: \textbf{X} → X
    for cmd in ("textbf", "textit", "emph", "underline", "textsc",
                "textrm", "textsf", "texttt", "small", "large",
                "Large", "huge", "Huge", "normalsize"):
        text = re.sub(rf"\\{cmd}\{{([^}}]*)\}}", r"\1", text)

    # \href{url}{text} → text
    text = re.sub(r"\\href\{[^}]*\}\{([^}]*)\}", r"\1", text)

    # \url{...} → removed
    text = re.sub(r"\\url\{[^}]*\}", "", text)

    # Section headings → uppercase plain text
    text = re.sub(r"\\section\*?\{([^}]*)\}", lambda m: m.group(1).upper(), text)
    text = re.sub(r"\\subsection\*?\{([^}]*)\}", lambda m: m.group(1).upper(), text)

    # \cventry{dates}{title}{company}{location}{grade}{desc}
    def _cventry(m):
        parts = [p.strip() for p in m.group(1).split("}{")]
        return " | ".join(p for p in parts if p and p != "{}") if parts else ""
    text = re.sub(r"\\cventry\{(.*?)\}\s*(?=\\|\n|$)", _cventry, text, flags=re.DOTALL)

    # \resumeSubheading{title}{dates}{company}{location}
    def _subheading(m):
        parts = [p.strip() for p in m.group(1).split("}{")]
        return " | ".join(p for p in parts if p)
    text = re.sub(r"\\resumeSubheading\{(.*?)\}\s*(?=\\|\n|$)", _subheading,
                  text, flags=re.DOTALL)

    # \resumeItem{text}
    text = re.sub(r"\\resumeItem\{([^}]*)\}", r"- \1", text)

    # Strip environments
    text = re.sub(r"\\begin\{[^}]+\}", "", text)
    text = re.sub(r"\\end\{[^}]+\}", "", text)

    # \cmd{text} → text
    text = re.sub(r"\\[a-zA-Z]+\*?\{([^}]*)\}", r"\1", text)

    # \cmd → space
    text = re.sub(r"\\[a-zA-Z]+\*?", " ", text)
    text = re.sub(r"\\.", " ", text)

    text = text.replace("{", "").replace("}", "")
    text = text.replace("~", " ").replace("&", " | ")

    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(lines).strip()


def remove_summary_section(latex: str) -> str:
    """Strip summary/objective \\section and its content from a LaTeX resume."""
    pattern = (
        r"\\section\*?\{(?:Summary|Objective|Professional Summary|Career Objective)"
        r"\}.*?(?=\\section|\\end\{document\})"
    )
    return re.sub(pattern, "", latex, flags=re.IGNORECASE | re.DOTALL)


def apply_tailoring_to_latex(latex_source: str, tailored: dict, job: dict) -> str:  # noqa: ARG001
    """Inject ATS-reordered skills into a LaTeX resume and remove the summary section."""
    latex = remove_summary_section(latex_source)

    skills = tailored.get("skills_reordered")
    if skills:
        def _replace_skills(m):
            header = m.group(1)
            new_content = f"\n{', '.join(skills)}\n\n"
            return header + new_content

        latex = re.sub(
            r"(\\section\*?\{(?:Skills|Technical Skills|Core Competencies)[^}]*\})"
            r"(.*?)(?=\\section|\\end\{document\})",
            _replace_skills,
            latex,
            flags=re.IGNORECASE | re.DOTALL,
        )

    missing = tailored.get("ats_keywords_missing")
    if missing:
        latex = latex.rstrip()
        if r"\end{document}" in latex:
            latex = latex.replace(
                r"\end{document}",
                f"% ATS gaps: {', '.join(missing)}\n\\end{{document}}"
            )

    return latex


def compile_latex_to_pdf(latex_source: str, output_path: Path) -> bool:
    """Compile *latex_source* to a PDF via pdflatex.  Returns True on success."""
    if not shutil.which("pdflatex"):
        console.print(
            "  [yellow]pdflatex not found — install TeX Live or MiKTeX to compile PDFs.[/yellow]"
        )
        return False

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "resume.tex").write_text(latex_source, encoding="utf-8")
        try:
            result = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "resume.tex"],
                cwd=tmpdir, capture_output=True, text=True, timeout=60,
            )
            pdf_file = tmp / "resume.pdf"
            if pdf_file.exists():
                shutil.copy(str(pdf_file), str(output_path))
                return True
            console.print(
                f"  [yellow]pdflatex ran but no PDF produced. "
                f"Stdout: {result.stdout[-500:]}[/yellow]"
            )
            return False
        except subprocess.TimeoutExpired:
            console.print("  [yellow]pdflatex timed out after 60s.[/yellow]")
            return False
        except Exception as e:
            console.print(f"  [yellow]pdflatex error: {e}[/yellow]")
            return False

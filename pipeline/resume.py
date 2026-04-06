"""
pipeline/resume.py
──────────────────
Resume I/O helpers: building the demo resume, reading any supported format,
and saving tailored output (plain-text or LaTeX/PDF).

Key API change vs. the old agent.py monolith:
  _read_resume(path) → (text: str, latex_source: str | None)
The caller is responsible for storing the latex_source; there is no global.
"""

import re
from pathlib import Path

from .config import console, OWNER_NAME, OUTPUT_DIR
from .latex import detect_latex, latex_to_plaintext, apply_tailoring_to_latex, compile_latex_to_pdf


# ── Demo resume ────────────────────────────────────────────────────────────────

def _build_demo_resume() -> str:
    return f"""{OWNER_NAME}
Email: sao.sithisack@ou.edu
LinkedIn: www.linkedin.com/in/saoaphisithsithisack
University: University of Oklahoma

OBJECTIVE
Electrical & Computer Engineering sophomore (Spring 2026) seeking summer
internship in IC design, photonics, or hardware engineering.

EDUCATION
University of Oklahoma | B.S. Electrical & Computer Engineering | Expected 2028

TECHNICAL SKILLS
Pulsed Laser Deposition | Photolithography | Cleanroom Processes
MATLAB | Python | Java | LaTeX
CAD: OnShape | Fusion360 | SolidWorks
FPGA | Verilog/VHDL | SPICE Simulation

PROJECTS
Photonics Thin-Film Device
  Deposited thin films using PLD; characterized optical properties with MATLAB.
  Performed photolithography and cleanroom fabrication steps.

IC Prototyping
  Designed and simulated mixed-signal circuits in SPICE.
  Fabricated prototype in university cleanroom.

FPGA Digital Design
  Implemented combinational and sequential logic in Verilog on Xilinx board.

EXPERIENCE
Physics Research Assistant | University of Oklahoma | 2024–Present
  Operated PLD system for thin-film material studies.
  Analyzed experimental data using MATLAB and Python.
  Contributed to lab reports with LaTeX documentation.

STEM Tutor & Mentor (CRLA Level 2) | University of Oklahoma | 2024–Present
  Tutored 20+ students in Physics, Calculus, and EE fundamentals.
  Improved student exam averages by 15% through structured sessions.

INTERESTS
Integrated circuits · Chip design · Photonics · Nanoelectronics · Device physics
"""


# ── Resume reader ──────────────────────────────────────────────────────────────

def _read_resume(path: Path) -> tuple[str, str | None]:
    """Read *path* and return ``(plaintext, latex_source_or_None)``.

    If the file is LaTeX (or contains LaTeX markup), *latex_source* holds the
    original source so callers can later produce a compiled PDF output.
    Falls back to the built-in demo resume on any read failure.
    """
    if not path.exists():
        console.print(f"  [yellow]File not found: {path} — using demo resume.[/yellow]")
        return _build_demo_resume(), None

    suffix = path.suffix.lower()

    if suffix == ".tex":
        raw = path.read_text(encoding="utf-8")
        console.print("  [cyan]LaTeX resume detected — converting to plain text for parsing.[/cyan]")
        return latex_to_plaintext(raw), raw

    if suffix in (".txt", ".md"):
        raw = path.read_text(encoding="utf-8")
        if detect_latex(raw):
            console.print(
                "  [cyan]LaTeX content detected — converting to plain text for parsing.[/cyan]"
            )
            return latex_to_plaintext(raw), raw
        return raw, None

    if suffix == ".pdf":
        try:
            import pdfplumber
        except ImportError:
            console.print("  [yellow]pdfplumber missing — pip install pdfplumber[/yellow]")
            return _build_demo_resume(), None
        try:
            with pdfplumber.open(str(path)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            if text.strip():
                return text, None
            console.print(
                "  [yellow]PDF opened but no text extracted — "
                "the file may be a scanned/image-only PDF.[/yellow]"
            )
            return _build_demo_resume(), None
        except Exception as e:
            console.print(f"  [yellow]PDF parse error: {e} — using demo resume.[/yellow]")
            return _build_demo_resume(), None

    if suffix == ".docx":
        try:
            from docx import Document
            text = "\n".join(p.text for p in Document(str(path)).paragraphs if p.text.strip())
            return text, None
        except ImportError:
            console.print("  [yellow]python-docx missing — pip install python-docx[/yellow]")
            return _build_demo_resume(), None

    # Generic text fallback
    try:
        return path.read_text(encoding="utf-8"), None
    except Exception:
        console.print(f"  [yellow]Cannot read {path} — using demo resume.[/yellow]")
        return _build_demo_resume(), None


# ── Tailored resume output ─────────────────────────────────────────────────────

def _save_tailored_resume(job: dict, tailored: dict, latex_source: str = None) -> str:
    """Write the tailored resume to OUTPUT_DIR and return the filename.

    If *latex_source* is provided, tries to produce a compiled PDF; falls back
    to saving a .tex file.  Otherwise writes a plain .txt file.
    """
    safe = lambda s: re.sub(r"[^a-zA-Z0-9_\-]", "_", s)
    base = (
        f"{safe(OWNER_NAME)}_Resume_{safe(job.get('company', ''))}"
        f"_{safe(job.get('title', ''))}"
    )

    # ── LaTeX / PDF path ──────────────────────────────────────────────────────
    if latex_source:
        tailored_latex = apply_tailoring_to_latex(latex_source, tailored, job)
        tex_path = OUTPUT_DIR / (base + ".tex")
        tex_path.write_text(tailored_latex, encoding="utf-8")
        pdf_path = OUTPUT_DIR / (base + ".pdf")
        if compile_latex_to_pdf(tailored_latex, pdf_path):
            console.print(f"  [green]PDF saved → {pdf_path.name}[/green]")
            return base + ".pdf"
        console.print(f"  [cyan]LaTeX source saved → {tex_path.name}[/cyan]")
        return base + ".tex"

    # ── Plain-text path ───────────────────────────────────────────────────────
    filename = base + ".txt"
    order = tailored.get("section_order") or ["Skills", "Projects", "Experience", "Education"]
    with open(OUTPUT_DIR / filename, "w", encoding="utf-8") as f:
        f.write(
            f"TAILORED RESUME\nRole: {job['title']} @ {job['company']}\n"
            f"Score: {job.get('score', 'N/A')}\n{'=' * 60}\n\n"
        )
        for section in order:
            if section == "Skills" and tailored.get("skills_reordered"):
                f.write(f"SKILLS\n{' | '.join(tailored['skills_reordered'])}\n\n")
            elif section == "Experience" and tailored.get("experience_bullets"):
                f.write("EXPERIENCE (tailored)\n")
                for role in tailored["experience_bullets"]:
                    f.write(f"\n{role.get('role', '')}\n")
                    for b in role.get("bullets", []):
                        f.write(f"  • {b}\n")
                f.write("\n")
        if tailored.get("ats_keywords_missing"):
            f.write(f"ATS GAPS\n{', '.join(tailored['ats_keywords_missing'])}\n\n")
        if tailored.get("cover_letter"):
            f.write(f"\n{'─' * 60}\nCOVER LETTER\n\n{tailored['cover_letter']}\n")
    return filename

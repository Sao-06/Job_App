"""Tests for pipeline.latex — detection, plaintext conversion, tailoring, sanitisation."""
from pathlib import Path

import pytest

from pipeline.latex import (
    apply_tailoring_to_latex,
    compile_latex_to_pdf,
    detect_latex,
    latex_to_plaintext,
    remove_summary_section,
    _sanitize_latex_source,
)

pytestmark = pytest.mark.unit


# ── detect_latex ────────────────────────────────────────────────────────────


class TestDetectLatex:
    def test_documentclass_prefix(self):
        assert detect_latex(r"\documentclass{article}\begin{document}hi\end{document}") is True

    def test_begin_document_prefix(self):
        assert detect_latex(r"\begin{document}hi\end{document}") is True

    def test_section_indicator(self):
        assert detect_latex(r"some text \section{Education} more text") is True

    def test_resume_command(self):
        assert detect_latex(r"\resumeSubheading{title}{dates}") is True

    def test_three_backslashes(self):
        assert detect_latex(r"\foo\bar\baz") is True

    def test_plain_text_returns_false(self):
        assert detect_latex("Just a plain text resume with no markup") is False


# ── latex_to_plaintext ──────────────────────────────────────────────────────


class TestLatexToPlaintext:
    def test_strips_preamble_and_document_wrapper(self):
        src = r"""
        \documentclass{article}
        \usepackage{geometry}
        \begin{document}
        Real content
        \end{document}
        """
        out = latex_to_plaintext(src)
        assert "documentclass" not in out
        assert "Real content" in out

    def test_unwraps_textbf(self):
        out = latex_to_plaintext(r"\begin{document}\textbf{Bold!}\end{document}")
        assert "Bold!" in out
        assert "textbf" not in out

    def test_section_uppercased(self):
        out = latex_to_plaintext(
            r"\begin{document}\section{Education}content\end{document}"
        )
        assert "EDUCATION" in out

    def test_href_keeps_label(self):
        out = latex_to_plaintext(
            r"\begin{document}\href{https://example.com}{Click me}\end{document}"
        )
        assert "Click me" in out
        assert "https://example.com" not in out

    def test_url_removed_completely(self):
        out = latex_to_plaintext(
            r"\begin{document}Visit \url{https://example.com}\end{document}"
        )
        assert "https://example.com" not in out

    def test_resume_item_keeps_dash_prefix(self):
        out = latex_to_plaintext(
            r"\begin{document}\resumeItem{built a thing}\end{document}"
        )
        assert "- built a thing" in out

    def test_collapses_blank_lines(self):
        src = r"\begin{document}line1\\" + "\n\n\n\n" + r"line2\end{document}"
        out = latex_to_plaintext(src)
        # 3+ blank lines should collapse to a single double newline at most.
        assert "\n\n\n" not in out


# ── remove_summary_section ──────────────────────────────────────────────────


class TestRemoveSummarySection:
    def test_strips_summary(self):
        src = (
            r"\section{Summary}I am great\section{Education}Where I went"
        )
        out = remove_summary_section(src)
        assert "Summary" not in out
        assert "I am great" not in out
        assert "Education" in out

    def test_strips_objective_too(self):
        src = r"\section{Objective}Goals\section{Skills}Stuff"
        out = remove_summary_section(src)
        assert "Goals" not in out
        assert "Skills" in out

    def test_case_insensitive(self):
        src = r"\section{SUMMARY}stuff\section{Education}edu"
        out = remove_summary_section(src)
        assert "stuff" not in out

    def test_no_summary_passthrough(self):
        src = r"\section{Education}edu\section{Skills}stuff"
        assert remove_summary_section(src) == src


# ── apply_tailoring_to_latex ────────────────────────────────────────────────


class TestApplyTailoring:
    def test_replaces_skills_section(self):
        src = (
            r"\begin{document}"
            r"\section{Skills}Old skill list"
            r"\section{Education}edu"
            r"\end{document}"
        )
        out = apply_tailoring_to_latex(
            src, {"skills_reordered": ["Verilog", "Python", "FPGA"]}, {"company": "Acme"}
        )
        assert "Verilog, Python, FPGA" in out
        assert "Old skill list" not in out

    def test_appends_ats_gap_comment(self):
        src = r"\begin{document}\section{Skills}existing\end{document}"
        out = apply_tailoring_to_latex(
            src, {"skills_reordered": ["X"], "ats_keywords_missing": ["Y", "Z"]}, {}
        )
        assert "ATS gaps: Y, Z" in out

    def test_removes_summary_section_first(self):
        src = (
            r"\begin{document}"
            r"\section{Summary}old summary"
            r"\section{Skills}old skills"
            r"\end{document}"
        )
        out = apply_tailoring_to_latex(src, {"skills_reordered": ["Verilog"]}, {})
        assert "old summary" not in out
        assert "Verilog" in out


# ── _sanitize_latex_source ──────────────────────────────────────────────────


class TestSanitiseLatex:
    @pytest.mark.parametrize("directive", [
        r"\input{/etc/passwd}",
        r"\include{secret.tex}",
        r"\openin\foo",
        r"\openout\foo",
        r"\write18{ls}",
        r"\immediate\write",
        r"\catcode`\@=11",
        r"\directlua{os.execute('rm -rf /')}",
        r"\luaexec{os.execute('foo')}",
    ])
    def test_dangerous_directives_neutralised(self, directive):
        src = r"\begin{document}before " + directive + r" after\end{document}"
        out = _sanitize_latex_source(src)
        # After sanitisation, the original raw directive must be gone.
        assert directive not in out
        # But the rest of the text is preserved.
        assert "before" in out and "after" in out

    def test_preserves_safe_content(self):
        src = r"\textbf{Hello}\section{Skills}Verilog"
        assert _sanitize_latex_source(src) == src


# ── compile_latex_to_pdf ────────────────────────────────────────────────────


class TestCompileLatex:
    def test_returns_false_when_pdflatex_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr("pipeline.latex.shutil.which", lambda _: None)
        out = compile_latex_to_pdf("ignored", tmp_path / "out.pdf")
        assert out is False

    def test_handles_subprocess_failure(self, monkeypatch, tmp_path):
        # Pretend pdflatex is on PATH but the run fails (no PDF produced).
        monkeypatch.setattr("pipeline.latex.shutil.which", lambda _: "/usr/bin/pdflatex")
        class _FakeResult:
            stdout = "no PDF produced"
            stderr = ""
        def _fake_run(*a, **k):
            return _FakeResult()
        monkeypatch.setattr("pipeline.latex.subprocess.run", _fake_run)
        out = compile_latex_to_pdf(r"\documentclass{article}\begin{document}hi\end{document}",
                                    tmp_path / "out.pdf")
        assert out is False

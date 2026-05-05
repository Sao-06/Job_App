"""
pipeline/sources/github_readme.py
─────────────────────────────────
Aggregator-style GitHub READMEs (SimplifyJobs, jobright-ai, speedyapply,
pittcsc, ouckah, vanshb03, etc.) that list jobs as either Markdown pipe
tables or HTML <tr>/<td> tables. One generic parser handles both
formats; the ``REPOS`` table at the bottom of this file picks the
parser per repo.

Each repo registers itself as a separate :class:`JobSource` with a
stable name like ``"gh:simplify/new-grad"`` so the dev page can show
per-repo health.
"""

from __future__ import annotations

import re
import urllib.request
from datetime import datetime
from typing import Iterator, List, Tuple

from .base import RawJob, is_remote_location
from .registry import register


# ── Network ──────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": "JobsAI/1.0 (+https://github.com/Sao-06/Job_App)",
    "Accept": "text/markdown, text/plain, */*",
}


def _fetch(url: str, timeout: int = 15) -> str | None:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


# ── Shared helpers ───────────────────────────────────────────────────────────

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_EMOJI_RE    = re.compile(r"[\U00010000-\U0010ffff]")
_MD_LINK_RE  = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_HTML_HREF_RE = re.compile(r'<a[^>]+href="(https?://[^"]+)"', re.IGNORECASE)


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", ", ", text, flags=re.I)
    text = _HTML_TAG_RE.sub("", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&nbsp;", " "))
    return re.sub(r",\s*,", ",", text).strip(", ").strip()


def _clean_cell(text: str) -> str:
    return _EMOJI_RE.sub("", _strip_html(text)).strip()


def _md_link_text_url(cell: str) -> Tuple[str, str]:
    m = _MD_LINK_RE.search(cell)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cell).strip(), ""


def _first_url(text: str) -> str:
    """First absolute http(s) URL in *text*. Strip trailing `)` from MD links
    and any HTML attribute terminator (``"`` / ``'``)."""
    m = re.search(r"https?://\S+", text)
    if not m:
        return ""
    url = m.group(0)
    # Bare URL might end with `">...</a>` (HTML embed). Cut at the first
    # quote/angle-bracket if present.
    for terminator in ('"', "'", "<", ">"):
        if terminator in url:
            url = url.split(terminator, 1)[0]
            break
    return url.rstrip("),.;")


def _href_from_cell(cell: str) -> str:
    """Pull the first ``<a href="URL">`` value out of a cell. Returns ""
    if there's no anchor; this is what speedyapply / many SimplifyJobs
    rows use instead of Markdown links."""
    if not cell:
        return ""
    m = _HTML_HREF_RE.search(cell)
    return m.group(1) if m else ""


def _split_md_row(line: str) -> list[str]:
    """Split a markdown table row on ``|`` and strip the leading/trailing
    empty cells produced by the wrapping pipes. Used for BOTH header
    detection and data rows so column indices stay aligned.
    """
    parts = [p.strip() for p in line.split("|")]
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


# ── Parsers ──────────────────────────────────────────────────────────────────

def _parse_html_tr_table(text: str, *, source: str, default_exp: str | None,
                         platform: str) -> Iterator[RawJob]:
    """Pittcsc/SimplifyJobs format: ``<tr><td>...</td></tr>`` rows.

    Column convention: company, title, location, link (Apply / 🔒 / closed).
    A ``↳`` company cell inherits the previous row's company (the
    "multiple postings under one company" pattern).
    """
    last_company = ""
    for block in re.findall(r"<tr>(.*?)</tr>", text, re.DOTALL | re.I):
        cells = re.findall(r"<td>(.*?)</td>", block, re.DOTALL | re.I)
        if len(cells) < 4:
            continue
        c0 = cells[0].strip()
        if c0 in ("↳", "") or "↳" in c0:
            company = last_company
        else:
            company = _clean_cell(c0)
            last_company = company
        title    = _clean_cell(cells[1])
        location = _clean_cell(cells[2])
        link_cell = cells[3].strip()
        if "🔒" in link_cell or "closed" in link_cell.lower():
            continue
        urls = re.findall(r'href="(https?://[^"]+)"', link_cell)
        # Skip simplify.jobs intermediaries when a real ATS link is present.
        non_simplify = [u for u in urls if "simplify.jobs" not in u]
        url = non_simplify[0] if non_simplify else (urls[0] if urls else _first_url(link_cell))
        if not (company and title and url):
            continue
        out: RawJob = {
            "application_url": url,
            "company": company,
            "title": title,
            "location": location,
            "remote": is_remote_location(location),
            "platform": platform,
            "source": source,
        }
        if default_exp:
            out["description"] = f"[seed] {default_exp} role"
        yield out


def _parse_md_table(text: str, *, source: str, default_exp: str | None,
                    platform: str) -> Iterator[RawJob]:
    """jobright-ai / speedyapply format: Markdown pipe tables.

    Column positions are detected from the header row (first line that
    starts with ``|`` and contains a "company" / "title" / "position" /
    "role" cell). Data rows and the header are both split via
    :func:`_split_md_row` so column indices stay aligned (a previous bug
    indexed into the unstripped split, off-by-one against the data rows).

    Apply-URL extraction prefers the dedicated apply column (header named
    "apply" / "link" / "posting" / "url") and pulls the first ``<a href="">``
    inside that cell. Falls back to the last URL on the row, then the
    first URL — both with attribute terminators stripped.
    """
    lines = text.splitlines()
    header_idx = -1
    col_company = col_title = col_location = col_date = col_apply = -1

    for i, raw in enumerate(lines):
        if "|" not in raw or not raw.lstrip().startswith("|"):
            continue
        header_cells = _split_md_row(raw)
        cols_lower = [c.lower() for c in header_cells]
        if not any(("company" in c) or ("title" in c) or ("position" in c) or ("role" in c)
                   for c in cols_lower):
            continue
        header_idx = i
        for j, c in enumerate(cols_lower):
            if "company" in c and col_company < 0:
                col_company = j
            if (("title" in c) or ("position" in c) or ("role" in c)) and col_title < 0:
                col_title = j
            if (("location" in c) or ("city" in c)) and col_location < 0:
                col_location = j
            if ("date" in c) and col_date < 0:
                col_date = j
            if (("apply" in c) or ("link" in c) or ("posting" in c) or ("url" in c)) and col_apply < 0:
                col_apply = j
        break
    if header_idx == -1:
        return

    for raw in lines[header_idx + 2:]:  # skip the |---|---| separator row
        line = raw.strip()
        if not line.startswith("|"):
            continue
        parts = _split_md_row(line)
        if len(parts) < 3:
            continue

        def _cell(idx: int, fallback: int = -1) -> str:
            if 0 <= idx < len(parts):
                return parts[idx]
            if 0 <= fallback < len(parts):
                return parts[fallback]
            return ""

        company_raw  = _cell(col_company, 0)
        title_raw    = _cell(col_title, 1)
        location_raw = _cell(col_location, 2)
        date_raw     = _cell(col_date, len(parts) - 1)
        apply_raw    = _cell(col_apply, -1)

        # Try in order: explicit Markdown link in the title cell,
        # <a href="..."> in the dedicated apply cell, last URL on the
        # row (apply links usually live in the rightmost link column),
        # then the first URL as a final fallback.
        _title_text, apply_url = _md_link_text_url(title_raw)
        if not apply_url and apply_raw:
            apply_url = _href_from_cell(apply_raw) or _first_url(apply_raw)
        if not apply_url:
            urls = re.findall(r"https?://\S+", line)
            if urls:
                apply_url = _first_url(urls[-1])
        if not apply_url:
            apply_url = _first_url(line)

        # Resolve company: HTML <a><strong>NAME</strong></a> or
        # Markdown [NAME](URL) or plain text. Always strip HTML at the end.
        company_text, _ = _md_link_text_url(company_raw)
        company = _clean_cell(company_text)

        # Title may itself be wrapped in <a href>...</a>. Strip the link.
        title = _clean_cell(_title_text or title_raw)
        location = re.sub(r"\s+", " ", _clean_cell(location_raw))
        posted   = (date_raw.strip()[:10] or "").strip() if date_raw else ""

        if not (company and title and apply_url):
            continue
        if company.lower() in ("company", "---") or title.lower() in ("title", "position", "---"):
            continue
        # If the parser still managed to put the same string in both fields
        # (truly malformed row), drop it rather than polluting the index.
        if company.lower() == title.lower():
            continue

        out: RawJob = {
            "application_url": apply_url,
            "company": company,
            "title": title,
            "location": location,
            "remote": is_remote_location(location),
            "platform": platform,
            "source": source,
        }
        if posted:
            out["posted_date"] = posted
        if default_exp:
            # Hint for inference if the source has no description text:
            out["description"] = f"[seed] {default_exp} role at {company}"
        yield out


# ── Source class — one instance per (repo, parser) ────────────────────────────

class GithubReadmeSource:
    cadence_seconds = 90 * 60        # 90 min
    timeout_seconds = 25

    def __init__(self, *, name: str, repo: str, branches: List[str],
                 parser: str, default_exp: str | None,
                 platform: str, section_keywords: List[str] | None = None):
        self.name = name
        self.repo = repo
        self.branches = branches
        self.parser = parser              # "md_table" | "html_tr"
        self.default_exp = default_exp    # "internship" | "entry-level" | None
        self.platform = platform
        self.section_keywords = section_keywords or []

    def _readme(self) -> str | None:
        for br in self.branches:
            url = f"https://raw.githubusercontent.com/{self.repo}/{br}/README.md"
            text = _fetch(url, timeout=self.timeout_seconds)
            if text:
                return text
        return None

    def _slice_section(self, text: str) -> str:
        """If section_keywords is set, narrow the text to that markdown section.
        Otherwise return the whole document."""
        if not self.section_keywords:
            return text
        lines = text.splitlines()
        start = -1
        for kw in self.section_keywords:
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("#") and kw in stripped.lower():
                    start = i
                    break
            if start != -1:
                break
        if start == -1:
            return text
        out: list = []
        for line in lines[start + 1:]:
            if line.strip().startswith("##"):
                break
            out.append(line)
        return "\n".join(out)

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        text = self._readme()
        if not text:
            return iter(())
        body = self._slice_section(text)
        if self.parser == "md_table":
            return _parse_md_table(body, source=self.name,
                                   default_exp=self.default_exp,
                                   platform=self.platform)
        # default "html_tr"
        return _parse_html_tr_table(body, source=self.name,
                                    default_exp=self.default_exp,
                                    platform=self.platform)


# ── Repo registry ─────────────────────────────────────────────────────────────

REPOS = [
    # SimplifyJobs (HTML <tr> tables)
    dict(name="gh:simplify/summer2026-internships",
         repo="SimplifyJobs/Summer2026-Internships",
         branches=["dev", "main"], parser="html_tr",
         default_exp="internship", platform="SimplifyJobs/GitHub"),
    dict(name="gh:simplify/new-grad",
         repo="SimplifyJobs/New-Grad-Positions",
         branches=["dev", "main"], parser="html_tr",
         default_exp="entry-level", platform="SimplifyJobs/GitHub"),

    # jobright-ai (Markdown pipe tables)
    dict(name="gh:jobright/2026-tech-internship",
         repo="jobright-ai/2026-Tech-Internship",
         branches=["main"], parser="md_table",
         default_exp="internship", platform="Jobright/GitHub"),
    dict(name="gh:jobright/2025-tech-internship",
         repo="jobright-ai/2025-Tech-Internship",
         branches=["main"], parser="md_table",
         default_exp="internship", platform="Jobright/GitHub"),
    dict(name="gh:jobright/2026-tech-new-grad",
         repo="jobright-ai/2026-Tech-New-Grad",
         branches=["main"], parser="md_table",
         default_exp="entry-level", platform="Jobright/GitHub"),
    dict(name="gh:jobright/2026-hardware-internship",
         repo="jobright-ai/2026-Hardware-Engineer-Internship",
         branches=["main"], parser="md_table",
         default_exp="internship", platform="Jobright/GitHub"),
    dict(name="gh:jobright/2025-hardware-internship",
         repo="jobright-ai/2025-Hardware-Engineer-Internship",
         branches=["main"], parser="md_table",
         default_exp="internship", platform="Jobright/GitHub"),

    # speedyapply
    dict(name="gh:speedyapply/2025-swe-college-jobs",
         repo="speedyapply/2025-SWE-College-Jobs",
         branches=["main"], parser="md_table",
         default_exp="entry-level", platform="SpeedyApply/GitHub"),
    dict(name="gh:speedyapply/2025-aiml-internships",
         repo="speedyapply/2025-AI-ML-Internships",
         branches=["main"], parser="md_table",
         default_exp="internship", platform="SpeedyApply/GitHub"),

    # Pittcsc / Vanshb03 / Ouckah (HTML <tr> tables)
    dict(name="gh:pittcsc/summer2026-internships",
         repo="SimplifyJobs/Summer2025-Internships",   # pittcsc handed off to SimplifyJobs in 2024
         branches=["dev", "main"], parser="html_tr",
         default_exp="internship", platform="Pittcsc/GitHub"),
    dict(name="gh:vanshb03/summer2026-internships",
         repo="vanshb03/Summer2026-Internships",
         branches=["main"], parser="html_tr",
         default_exp="internship", platform="Vanshb03/GitHub"),
    dict(name="gh:ouckah/summer2025-internships",
         repo="ouckah/Summer2025-Internships",
         branches=["main"], parser="html_tr",
         default_exp="internship", platform="Ouckah/GitHub"),
]


# ── Self-register all repos ──────────────────────────────────────────────────

for spec in REPOS:
    register(GithubReadmeSource(**spec))

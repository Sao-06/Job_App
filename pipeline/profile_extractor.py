"""
pipeline/profile_extractor.py
─────────────────────────────
Heuristic-first profile extraction with optional LLM verification.

The flow:
  1. `scan_profile(resume_text)` runs deterministic regex + section parsing on
     the resume preview text and produces a complete baseline profile —
     name, contacts, links, education, experience, projects, skills,
     target titles, and resume gaps. Every field is populated to its real
     shape (empty string / empty list when nothing was found) so the
     downstream merger can rely on the structure.
  2. The active LLM provider is then asked to verify and correct the
     baseline. The provider receives the heuristic as part of the prompt
     (via the `heuristic_hint` kwarg on `BaseProvider.extract_profile`) so
     it has something concrete to check rather than re-deriving from
     scratch — this is what eliminates the "boxes left blank" failure mode.
  3. `merge_profiles(heuristic, llm)` combines the two:
       - Strong-heuristic scalars (email/phone/linkedin/github/name/location)
         keep the regex result if non-empty; the LLM only fills gaps.
       - Free-text scalars (summary, critical_analysis) prefer the LLM.
       - String lists (skills, target titles, gaps) are unioned, deduped.
       - Structured lists (experience/education/projects) prefer whichever
         side has richer entries.
"""

from __future__ import annotations

import re
from typing import Any

from .config import console


# ── Public API ────────────────────────────────────────────────────────────────

def scan_profile(resume_text: str) -> dict:
    """Pure heuristic extraction. Returns the full profile shape with empty
    strings/lists where nothing was found. Safe to call standalone or as the
    first stage of a heuristic-then-LLM pipeline.
    """
    # Imported lazily to avoid a hard cycle: providers.py imports from this
    # module too once we wire the heuristic into the LLM prompt.
    from .providers import (
        DemoProvider, _extract_name_from_text, _extract_location_from_text,
    )

    text = resume_text or ""

    # ── Contact regex extraction ─────────────────────────────────────────────
    email = _first(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}", text)
    phone = _extract_phone(text)
    linkedin = _normalize_url(_first(
        r"(?:https?://)?(?:[\w]+\.)?linkedin\.com/(?:in|pub)/[\w\-/]+", text, re.I,
    ))
    github = _normalize_url(_first(
        r"(?:https?://)?(?:[\w]+\.)?github\.com/[\w\-_./]+", text, re.I,
    ))
    website = _first(
        r"https?://(?!(?:www\.)?(?:linkedin|github|gitlab|bitbucket|google|gmail|yahoo|outlook|hotmail)\.)[\w\-./]+\.[a-z]{2,}[\w\-./]*",
        text, re.I,
    )

    name = _extract_name_from_text(text)
    location = _extract_location_from_text(text)

    # ── Section-aware parsing — reuse DemoProvider's tuned helpers ──────────
    sections = DemoProvider._split_sections(text)

    def grab(*keys: str) -> list[str]:
        for k in keys:
            if k in sections and sections[k]:
                return sections[k]
        return []

    education = (
        DemoProvider._parse_education_block(grab("education")) if grab("education") else []
    )

    # Layered experience extraction:
    #   1. Run the new robust block parser on the section if it exists.
    #   2. If that returns nothing (or no section), run a full-text scan
    #      anchored on date-range patterns — that catches resumes whose
    #      section header didn't match any alias.
    experience_raw = grab("experience")
    experience = parse_experience_robust(experience_raw) if experience_raw else []
    if not experience:
        # Try the older parser as a secondary, then full-text fallback.
        experience = (
            DemoProvider._parse_experience_block(experience_raw)
            if experience_raw else []
        )
    if not experience:
        experience = parse_experience_fulltext(text, sections)

    research_raw = grab("research experience")
    research = parse_experience_robust(research_raw) if research_raw else []
    if not research and research_raw:
        research = DemoProvider._parse_experience_block(research_raw)

    projects_raw = grab("projects")
    projects = parse_projects_robust(projects_raw) if projects_raw else []
    if not projects:
        projects = (
            DemoProvider._parse_projects_block(projects_raw)
            if projects_raw else []
        )
    if not projects:
        projects = parse_projects_fulltext(text, sections)

    # ── Skills (lexicon-driven) ──────────────────────────────────────────────
    hard_skills = _scan_hard_skills(text)
    soft_skills = _scan_soft_skills(text)

    # ── Target titles inferred from skill footprint ─────────────────────────
    target_titles = _scan_target_titles(text, hard_skills)

    # ── Summary — first paragraph of an explicit summary section ───────────
    summary = _condense_summary(grab("summary", "objective"))

    # ── Resume gap heuristics ────────────────────────────────────────────────
    gaps = _detect_gaps(text, email=email, linkedin=linkedin, github=github)

    return {
        "name": name or "",
        "email": email or "",
        "phone": phone or "",
        "linkedin": linkedin or "",
        "github": github or "",
        "website": website or "",
        "location": location or "",
        "summary": summary,
        "target_titles": target_titles,
        "top_hard_skills": hard_skills,
        "top_soft_skills": soft_skills,
        "education": education,
        "experience": experience,
        "research_experience": research,
        # `work_experience` is the back-compat alias used by some downstream paths.
        "work_experience": experience,
        "projects": projects,
        "resume_gaps": gaps,
        "work_authorization": "",
        "target_salary": "",
        "_extraction_method": "heuristic",
    }


def merge_profiles(heuristic: dict, llm: dict | None) -> dict:
    """Merge heuristic and LLM-extracted profiles.

    Per-field strategy is documented at the top of this module.
    """
    if not llm:
        out = dict(heuristic or {})
        out["_extraction_method"] = "heuristic-only"
        return out

    out: dict[str, Any] = dict(heuristic or {})

    # Scalars where regex is more reliable than LLMs.
    for k in ("email", "phone", "linkedin", "github", "website", "name", "location"):
        h_val = (out.get(k) or "").strip() if isinstance(out.get(k), str) else out.get(k)
        l_val = llm.get(k)
        if not h_val and l_val:
            out[k] = l_val if isinstance(l_val, str) else str(l_val)

    # Free-text scalars where the LLM tends to be richer.
    for k in ("summary", "critical_analysis", "work_authorization", "target_salary"):
        l_val = llm.get(k)
        if isinstance(l_val, str) and l_val.strip():
            out[k] = l_val.strip()

    # String lists: union, dedupe, LLM order first.
    for k in ("target_titles", "top_hard_skills", "top_soft_skills", "resume_gaps"):
        out[k] = _merge_string_lists(out.get(k) or [], llm.get(k) or [])

    # Structured lists: pick whichever side has more / richer entries.
    for k in ("education", "experience", "research_experience", "work_experience", "projects"):
        h_list, l_list = out.get(k) or [], llm.get(k) or []
        if not l_list:
            out[k] = h_list
            continue
        if not h_list:
            out[k] = l_list
            continue
        h_score = sum(_richness(e) for e in h_list)
        l_score = sum(_richness(e) for e in l_list)
        out[k] = l_list if l_score >= h_score else h_list

    # Make sure 'experience' / 'work_experience' stay aligned for back-compat.
    if not out.get("experience") and out.get("work_experience"):
        out["experience"] = out["work_experience"]

    out["_extraction_method"] = "heuristic+llm"
    if isinstance(llm.get("_audit_log"), list):
        out["_audit_log"] = list(out.get("_audit_log") or []) + llm["_audit_log"]
    return out


# ── Internal helpers ──────────────────────────────────────────────────────────

def _first(pattern: str, text: str, flags: int = 0) -> str:
    if not text:
        return ""
    m = re.search(pattern, text, flags)
    return m.group(0) if m else ""


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    s = url.strip().rstrip(",.;:)")
    if not s:
        return ""
    if not s.lower().startswith(("http://", "https://")):
        s = "https://" + s
    return s


_PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s.\-]?)?"            # optional country code
    r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}"
)


def _extract_phone(text: str) -> str:
    if not text:
        return ""
    for m in _PHONE_RE.finditer(text):
        raw = m.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        # 10 or 11 (with country) digits is a real US phone.
        if len(digits) in (10, 11):
            return raw
    return ""


def _scan_hard_skills(text: str) -> list[str]:
    """Scan the resume for lexicon hard-skill tokens, preserving the casing
    used in the resume itself (so 'Verilog' stays 'Verilog', not 'verilog').

    Uses word-boundary matching so short tokens don't false-positive inside
    larger words ('git' must not match inside 'github', 'mbe' must not match
    inside 'embedded', 'rie' must not match inside 'enterprise'). Multi-word
    tokens like 'thin film' work naturally because the embedded spaces
    already enforce token edges.
    """
    from .profile_audit import HARD_SKILL_LEXICON

    if not text:
        return []
    found: list[tuple[str, int]] = []
    for token in HARD_SKILL_LEXICON:
        token_s = token.strip()
        if not token_s or len(token_s) < 2:
            continue
        # Word-boundary match. `\b` does the right thing for alphanumeric
        # token edges; we add explicit lookarounds for tokens that contain
        # special characters (e.g. 'c++', 'c#') so they are not skipped.
        if re.search(r"\W", token_s):
            # Token contains a non-word char; \b on the alpha edges only.
            esc = re.escape(token_s)
            pattern = rf"(?<!\w){esc}(?!\w)"
        else:
            pattern = rf"\b{re.escape(token_s)}\b"
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        idx = m.start()
        actual = text[idx:idx + len(token_s)]
        # Always preserve the resume's own casing — that's the user's intent.
        # Only fall back to title-case when the source text was all lowercase
        # (rare; usually means the parser produced a lowercase variant).
        if actual.isupper() or actual.islower() and len(actual) <= 4:
            # Pure-caps in the resume → keep caps; pure-lower acronym → caps.
            display = actual.upper() if actual.isupper() else actual.upper()
        elif actual.islower():
            display = actual.title()
        else:
            display = actual
        display = display.strip()
        found.append((display, idx))

    seen: set[str] = set()
    ordered: list[str] = []
    for display, _idx in sorted(found, key=lambda kv: kv[1]):
        key = display.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(display)
    return ordered[:30]


_SOFT_SKILL_VOCAB = (
    ("teamwork",                "Teamwork"),
    ("collaboration",           "Collaboration"),
    ("communication",           "Communication"),
    ("leadership",              "Leadership"),
    ("problem solving",         "Problem-solving"),
    ("problem-solving",         "Problem-solving"),
    ("critical thinking",       "Critical thinking"),
    ("project management",      "Project management"),
    ("time management",         "Time management"),
    ("attention to detail",     "Attention to detail"),
    ("adaptability",            "Adaptability"),
    ("creativity",              "Creativity"),
    ("mentoring",               "Mentoring"),
    ("presentation",            "Presentation"),
    ("technical writing",       "Technical writing"),
    ("cross-functional",        "Cross-functional collaboration"),
    ("initiative",              "Initiative"),
    ("analytical",              "Analytical thinking"),
    ("documentation",           "Documentation"),
)


def _scan_soft_skills(text: str) -> list[str]:
    if not text:
        return []
    text_l = text.lower()
    seen: set[str] = set()
    out: list[str] = []
    for token, display in _SOFT_SKILL_VOCAB:
        if token in text_l and display.lower() not in seen:
            seen.add(display.lower())
            out.append(display)
    return out[:8]


# Title-inference rules: each rule is (set_of_keywords, suggested_title).
# Match if *any* keyword appears in the resume's hard-skill set OR raw text.
_TITLE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("verilog", "systemverilog", "spice", "cadence virtuoso", "synopsys", "vlsi", "asic"),
        "IC Design / VLSI Engineering Intern"),
    (("fpga", "vivado", "quartus", "xilinx", "altera", "rtl"),
        "FPGA / Digital Design Engineering Intern"),
    (("photolithography", "cleanroom", "thin film", "lumerical", "photonics", "optoelectronics"),
        "Photonics / Optoelectronics Engineering Intern"),
    (("pcb design", "altium", "kicad", "schematic", "circuit"),
        "Hardware Engineering Intern"),
    (("microcontroller", "arduino", "stm32", "embedded", "rtos"),
        "Embedded Systems / Firmware Intern"),
    (("comsol", "ansys", "hfss", "device simulation"),
        "Semiconductor Process / Device Engineering Intern"),
    (("rf ", "vector network analyzer", "antenna"),
        "RF Engineering Intern"),
    (("pytorch", "tensorflow", "machine learning", "deep learning"),
        "Machine Learning Engineering Intern"),
    (("react", "typescript", "next.js", "node.js"),
        "Software Engineering Intern"),
]


def _scan_target_titles(text: str, hard_skills: list[str]) -> list[str]:
    if not text:
        return []
    skills_l = {s.lower() for s in (hard_skills or [])}
    text_l = text.lower()
    inferred: list[str] = []
    for keywords, title in _TITLE_RULES:
        if any(k in skills_l or k in text_l for k in keywords):
            if title not in inferred:
                inferred.append(title)
    return inferred[:5]


def _condense_summary(lines: list[str]) -> str:
    """Take the first ~3 sentences of a SUMMARY/OBJECTIVE section."""
    flat = " ".join(l.strip() for l in (lines or []) if l and l.strip())
    if not flat:
        return ""
    # Trim multiple spaces; cap to ~400 chars.
    flat = re.sub(r"\s+", " ", flat).strip()
    return flat[:420]


def _detect_gaps(text: str, *, email: str, linkedin: str, github: str) -> list[str]:
    gaps: list[str] = []
    if not text:
        return gaps
    lower = text.lower()
    has_summary = any(k in lower for k in ("summary\n", "objective\n", "profile\n",
                                            "summary:", "objective:", "profile:",
                                            "summary ", "objective "))
    if not has_summary:
        gaps.append("No summary or objective section detected")
    if not re.search(r"\d+\s*%|\$\d|\d+\s*(?:users?|projects?|students?|hours?|teams?)", text, re.I):
        gaps.append("Few quantified outcomes — add metrics to bullets")
    if not email:
        gaps.append("Email address not detected on the contact line")
    if not linkedin:
        gaps.append("LinkedIn URL missing — recruiters expect one to verify identity")
    if not github and any(k in lower for k in ("python", "java", "c++", "verilog", "react")):
        gaps.append("No GitHub/portfolio link — let reviewers verify project claims")
    return gaps


def _merge_string_lists(a_list, b_list) -> list[str]:
    """Union two string lists, preserving the order of first appearance.
    Items can be plain strings or `{skill, ...}` / `{title, ...}` dicts (LLMs
    sometimes return the detailed object form).
    """
    out: list[str] = []
    seen: set[str] = set()
    # b first → LLM (corrected) values lead; heuristic fills any gaps.
    for src in (b_list, a_list):
        for item in src or []:
            if isinstance(item, dict):
                s = item.get("skill") or item.get("title") or ""
            else:
                s = str(item)
            s = s.strip()
            if not s:
                continue
            k = s.lower()
            if k not in seen:
                seen.add(k)
                out.append(s)
    return out


def _richness(entry) -> int:
    """Heuristic richness score for one structured entry (experience / project / education)."""
    if not isinstance(entry, dict):
        return 0
    score = 0
    for k in ("title", "company", "name", "degree", "institution", "year", "dates"):
        if entry.get(k):
            score += 1
    bullets = entry.get("bullets") or []
    if isinstance(bullets, list):
        score += min(5, len(bullets))
    desc = entry.get("description")
    if isinstance(desc, str) and desc.strip():
        score += 1
    skills = entry.get("skills_used") or []
    if isinstance(skills, list):
        score += min(3, len(skills))
    return score


def heuristic_summary(profile: dict) -> str:
    """Compact one-line console summary of what the heuristic found."""
    p = profile or {}
    name = p.get("name") or "?"
    return (
        f"name='{name}', email={'✓' if p.get('email') else '✗'}, "
        f"linkedin={'✓' if p.get('linkedin') else '✗'}, "
        f"github={'✓' if p.get('github') else '✗'}, "
        f"location='{p.get('location','')}', "
        f"hard_skills={len(p.get('top_hard_skills') or [])}, "
        f"experience={len(p.get('experience') or [])}, "
        f"research={len(p.get('research_experience') or [])}, "
        f"projects={len(p.get('projects') or [])}, "
        f"education={len(p.get('education') or [])}"
    )


# ── Robust experience / projects parsers ─────────────────────────────────────

# Date patterns that anchor a role header — covers month-year, season-year,
# year ranges, "Present"/"Current", and bare 4-digit years.
_DATE_RE = re.compile(
    r"\b(?:"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s*\d{0,4}"
    r"\s*(?:[-–—to]+\s*)?"
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s*\d{0,4}"
    r"|Present|Current|Now|\d{4})?"
    r"|(?:Spring|Summer|Fall|Autumn|Winter)\s+\d{4}"
    r"|(?:19|20)\d{2}\s*(?:[-–—to]+\s*)?(?:Present|Current|Now|(?:19|20)\d{2})?"
    r"|\d{1,2}/\d{4}\s*(?:[-–—to]+\s*)?(?:\d{1,2}/\d{4}|Present|Current)?"
    r")\b",
    re.IGNORECASE,
)

_BULLET_RE = re.compile(r"^\s*[•\-*–·▪◦►]\s+")
_HEADER_SEP_RE = re.compile(r"\s+[|·•—–@]\s+|\s+-\s+(?=[A-Z])")


def _has_date(line: str) -> bool:
    return bool(_DATE_RE.search(line))


def _strip_bullet(raw: str) -> str:
    return re.sub(r"^\s*[•\-*–·▪◦►]\s+", "", raw).strip()


# Keywords that strongly indicate one part of a header is the job title rather
# than the company. Used to fix swaps like "Apple — Embedded Engineering Intern".
_TITLE_HINTS = re.compile(
    r"\b(?:engineer|developer|intern|analyst|scientist|researcher|"
    r"manager|architect|consultant|designer|specialist|associate|"
    r"officer|director|coordinator|technician|fellow|assistant|"
    r"co-?op|trainee)\b",
    re.IGNORECASE,
)


def _disambiguate_title_company(a: str, b: str) -> tuple[str, str]:
    """Given two header parts, pick which is the title and which is the company.
    Title = whichever side contains a job-role keyword. If neither or both do,
    keep the original order.
    """
    if not a:
        return a, b
    if not b:
        return a, b
    a_is_title = bool(_TITLE_HINTS.search(a))
    b_is_title = bool(_TITLE_HINTS.search(b))
    if a_is_title and not b_is_title:
        return a, b
    if b_is_title and not a_is_title:
        return b, a
    return a, b


def _split_header_segments(s: str) -> list[str]:
    """Split a single header line into parts using the most likely separator."""
    if " | " in s or "|" in s:
        return [p.strip() for p in re.split(r"\s*\|\s*", s) if p.strip()]
    for sep in (" — ", " – ", " · ", " • ", " @ ", " at ", " - "):
        if sep in s:
            return [p.strip() for p in s.split(sep) if p.strip()]
    if "  " in s:
        # 2-or-more-space columns from collapsed LaTeX/Word layouts.
        return [p.strip() for p in re.split(r" {2,}", s) if p.strip()]
    return []


def _classify_header_parts(line: str) -> tuple[str, str, str]:
    """Split a single role-header line into (title, company, dates)."""
    s = re.sub(r"\s+", " ", line).strip()
    date_match = _DATE_RE.search(s)
    dates = date_match.group(0).strip() if date_match else ""
    if dates:
        s_no_date = (s[:date_match.start()] + s[date_match.end():]).strip(" ,|·•—–-()")
    else:
        s_no_date = s

    parts = _split_header_segments(s_no_date)
    if parts:
        title = parts[0]
        company = parts[1] if len(parts) >= 2 else ""
        title, company = _disambiguate_title_company(title, company)
        return title, company, dates
    return s_no_date, "", dates


def _classify_two_line_header(line1: str, line2: str) -> tuple[str, str, str]:
    """Merge a two-line header where line 1 has title+company and line 2 has
    dates+location. Common in LaTeX 2-column resume templates.

    The split MUST happen before space-collapsing because multi-space runs
    are the only signal that a 2-column line had distinct columns.
    """
    line1_raw = line1.rstrip()
    line2_raw = line2.rstrip()

    # Get dates from either line — usually line 2.
    full = line1_raw + "  " + line2_raw
    date_match = _DATE_RE.search(full)
    dates = date_match.group(0).strip() if date_match else ""

    # Strip dates out of line 1 (rare — dates usually live on line 2).
    line1_for_split = line1_raw
    if dates and dates in line1_raw:
        line1_for_split = line1_raw.replace(dates, "").rstrip()

    # Split BEFORE normalizing spaces so column boundaries survive.
    parts = _split_header_segments(line1_for_split)
    if not parts:
        # No multi-space columns — fall back to single-segment then collapse.
        parts = [re.sub(r"\s+", " ", line1_for_split).strip()]

    title = parts[0]
    company = parts[1] if len(parts) >= 2 else ""
    title = re.sub(r"\s+", " ", title).strip(" ,|·•—–-()")
    company = re.sub(r"\s+", " ", company).strip(" ,|·•—–-()")
    title, company = _disambiguate_title_company(title, company)
    return title, company, dates


def parse_experience_robust(lines: list[str]) -> list[dict]:
    """Parse a section's lines into role entries.

    Handles three common header layouts:
      • Single-line:   ``Title | Company | Dates``  /  ``Title @ Company  Dates``
      • Two-line:      Title-Company line followed by Dates-Location line
                       (typical LaTeX/Word two-column templates)
      • Continuation:  short company-only line under a title with no separator.

    Bullet lines (any of •, -, *, –, ·, ▪, ◦, ►) accumulate under the most
    recently opened role.
    """
    if not lines:
        return []

    roles: list[dict] = []
    cur: dict | None = None
    pending: str | None = None  # title-company line waiting for its date line

    def _commit_pending_solo() -> None:
        """If a pending header never got its date partner, commit it alone."""
        nonlocal cur, pending
        if pending is None:
            return
        title, company, dates = _classify_header_parts(pending)
        cur = {"title": title, "company": company, "dates": dates, "bullets": []}
        roles.append(cur)
        pending = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if _BULLET_RE.match(raw):
            text = _strip_bullet(raw)
            if not text:
                continue
            _commit_pending_solo()
            if cur is None:
                cur = {"title": "", "company": "", "dates": "", "bullets": []}
                roles.append(cur)
            cur["bullets"].append(text)
            continue

        line_has_date = _has_date(line)
        line_has_sep = bool(_HEADER_SEP_RE.search(line)) or "|" in line
        line_has_2col = bool(re.search(r"\S {2,}\S", line))

        # Two-line header merge: pending title+company line followed by a
        # line that contains a date.
        if pending is not None and line_has_date:
            title, company, dates = _classify_two_line_header(pending, line)
            cur = {"title": title, "company": company,
                   "dates": dates, "bullets": []}
            roles.append(cur)
            pending = None
            continue

        if line_has_date or line_has_sep:
            _commit_pending_solo()
            title, company, dates = _classify_header_parts(line)
            cur = {"title": title, "company": company,
                   "dates": dates, "bullets": []}
            roles.append(cur)
        elif line_has_2col:
            # Title+company in 2-col format with no date yet — defer.
            _commit_pending_solo()
            pending = line
        else:
            # Continuation: short non-date line. Treat as company filler if
            # the current role is missing one, else as a description bullet.
            if cur and not cur.get("company") and len(line) < 80:
                cur["company"] = line
            elif cur:
                cur["bullets"].append(line)
            else:
                # No open role yet — could be a single-token role title.
                pending = line

    _commit_pending_solo()
    return [r for r in roles if r.get("title") or r.get("company") or r.get("bullets")]


def parse_projects_robust(lines: list[str]) -> list[dict]:
    """Parse a PROJECTS section. Each Title-Case (or all-caps) non-bullet
    line is a new project; following bullets / sentences populate it.
    """
    if not lines:
        return []
    projects: list[dict] = []
    cur: dict | None = None

    def _new(name: str) -> dict:
        return {"name": name.strip(), "description": "", "bullets": [],
                "skills_used": [], "dates": "", "url": ""}

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        is_bullet = bool(_BULLET_RE.match(raw))
        if is_bullet:
            text = _strip_bullet(raw)
            if not text:
                continue
            if cur is None:
                cur = _new("")
                projects.append(cur)
            cur["bullets"].append(text)
            continue

        # Tech-tag line: "Tools: …" / "Tech Stack: …"
        m = re.match(r"^\s*(?:tech(?:nologies)?|stack|tools|skills|languages|built\s+with)[\s:]+(.+)$",
                      line, re.IGNORECASE)
        if m and cur is not None:
            for tok in re.split(r"[,/;]+", m.group(1)):
                tok = tok.strip(" ()")
                if tok:
                    cur["skills_used"].append(tok)
            continue

        # Otherwise: Title-Case / all-caps line → new project.
        is_titley = (
            line == line.upper()
            or sum(1 for w in line.split() if w[:1].isupper()) >= max(1, len(line.split()) - 1)
        )
        if is_titley and len(line) <= 100:
            cur = _new(line)
            projects.append(cur)
        elif cur is not None:
            if not cur["description"]:
                cur["description"] = line
            else:
                cur["bullets"].append(line)

    return [p for p in projects if p.get("name") or p.get("bullets") or p.get("description")]


def parse_experience_fulltext(text: str, sections: dict) -> list[dict]:
    """Last-resort: scan the entire resume for date-anchored role blocks.

    Triggers when no recognized "experience" section was detected. For every
    line that contains a date range (real role headers almost always do),
    we open a new role and capture following bullet lines.
    """
    if not text:
        return []
    # Avoid double-counting lines that already lived inside a recognized
    # education / projects / skills block.
    skip_buckets = {"education", "skills", "projects", "summary", "objective",
                    "publications", "certifications", "awards", "interests",
                    "coursework", "research experience"}
    forbidden_lines: set[str] = set()
    for k, lines in (sections or {}).items():
        if k in skip_buckets:
            for ln in lines:
                if ln.strip():
                    forbidden_lines.add(ln.strip())

    roles: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line in forbidden_lines:
            cur = None
            continue
        # Headers like "EXPERIENCE" alone close any open role.
        if line.isupper() and len(line.split()) <= 4:
            cur = None
            continue

        is_bullet = bool(_BULLET_RE.match(raw))
        if is_bullet and cur is not None:
            text_l = _strip_bullet(raw)
            if text_l:
                cur["bullets"].append(text_l)
            continue
        if is_bullet:
            continue   # bullet without an open role — ignore

        if _has_date(line):
            title, company, dates = _classify_header_parts(line)
            cur = {"title": title, "company": company, "dates": dates,
                   "bullets": []}
            roles.append(cur)

    # Filter: keep only roles that had at least one bullet OR a clear company.
    cleaned = [
        r for r in roles
        if r.get("bullets") or (r.get("company") and r.get("title"))
    ]
    return cleaned[:8]


def parse_projects_fulltext(text: str, sections: dict) -> list[dict]:
    """Last-resort: catch projects when no recognized PROJECTS section exists.
    Looks for short Title-Case lines followed by bullets, anywhere in the text.
    """
    if not text:
        return []
    # Skip lines we already used for other buckets.
    skip_buckets = {"education", "skills", "experience", "summary", "objective",
                    "publications", "certifications", "awards", "interests",
                    "coursework", "research experience"}
    forbidden_lines: set[str] = set()
    for k, lines in (sections or {}).items():
        if k in skip_buckets:
            for ln in lines:
                if ln.strip():
                    forbidden_lines.add(ln.strip())

    candidates: list[dict] = []
    cur: dict | None = None
    saw_bullet = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line in forbidden_lines:
            cur = None
            saw_bullet = False
            continue
        # Skip very long lines — projects always have short titles.
        is_bullet = bool(_BULLET_RE.match(raw))
        if is_bullet and cur is not None:
            text_l = _strip_bullet(raw)
            if text_l:
                cur["bullets"].append(text_l)
                saw_bullet = True
            continue
        if is_bullet:
            continue
        if 3 <= len(line) <= 80 and not _has_date(line) and not _HEADER_SEP_RE.search(line):
            words = line.split()
            cap_ratio = sum(1 for w in words if w[:1].isupper()) / max(1, len(words))
            if cap_ratio >= 0.6 and len(words) <= 8:
                # Open a new candidate.
                cur = {"name": line, "description": "", "bullets": [],
                       "skills_used": [], "dates": "", "url": ""}
                candidates.append(cur)
                saw_bullet = False
                continue
        # Otherwise treat as description if a project was just opened.
        if cur is not None and not cur["description"] and not saw_bullet:
            cur["description"] = line

    # Only keep candidates that had at least one bullet — otherwise too noisy.
    return [c for c in candidates if c["bullets"]][:6]

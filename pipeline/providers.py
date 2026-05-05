"""
pipeline/providers.py
─────────────────────
LLM provider abstraction: base class, three concrete implementations
(Anthropic, Demo, Ollama), and the factory function.
"""

import json
import re
from datetime import date
from pathlib import Path

from .config import console, OWNER_NAME, DEMO_JOBS


# ── Base ───────────────────────────────────────────────────────────────────────

class BaseProvider:
    """Abstract base — all providers must implement these methods."""

    def extract_profile(self, resume_text: str, preferred_titles: list = None) -> dict:
        raise NotImplementedError

    def score_job(self, job: dict, profile: dict) -> dict:
        raise NotImplementedError

    def tailor_resume(self, job: dict, profile: dict, resume_text: str) -> dict:
        raise NotImplementedError

    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        raise NotImplementedError

    def generate_report(self, summary_data: dict) -> str:
        raise NotImplementedError

    def generate_demo_jobs(self, profile: dict, titles: list, location: str) -> list:
        raise NotImplementedError


# ── Shared rubric scorer (Phase 3) ─────────────────────────────────────────────
# Weighted categories: Required Skills 50 / Industry 30 / Location+Seniority 20.

RUBRIC_WEIGHTS = {"required_skills": 50, "industry": 30, "location_seniority": 20}


def _build_rubric_result(job: dict, req_raw: float, industry_raw: float,
                          loc_seniority_raw: float, *,
                          matched: list = None, missing: list = None,
                          reasoning: str = "") -> dict:
    """Clamp sub-scores, compute weighted total, and assemble the standard
    rubric result dict consumed by Phase 3 and the tracker."""
    def _clamp(x: float) -> float:
        try:
            return max(0.0, min(1.0, float(x)))
        except (TypeError, ValueError):
            return 0.0
    req_raw = _clamp(req_raw)
    industry_raw = _clamp(industry_raw)
    loc_seniority_raw = _clamp(loc_seniority_raw)
    pts_skills = round(req_raw * RUBRIC_WEIGHTS["required_skills"])
    pts_ind    = round(industry_raw * RUBRIC_WEIGHTS["industry"])
    pts_loc    = round(loc_seniority_raw * RUBRIC_WEIGHTS["location_seniority"])
    total = max(0, min(100, pts_skills + pts_ind + pts_loc))
    if not reasoning:
        reasoning = (
            f"Skills {int(req_raw*100)}%, industry {int(industry_raw*100)}%, "
            f"location/seniority {int(loc_seniority_raw*100)}%."
        )
    return {
        "job_id": job.get("id", ""),
        "score": total,
        "score_breakdown": {
            "required_skills":    {"raw": req_raw,          "weight": 50, "points": pts_skills},
            "industry":           {"raw": industry_raw,     "weight": 30, "points": pts_ind},
            "location_seniority": {"raw": loc_seniority_raw,"weight": 20, "points": pts_loc},
        },
        "reasoning": reasoning,
        "matching_skills": (matched or [])[:6],
        "missing_skills":  (missing or [])[:6],
        "reason": reasoning,  # back-compat
    }


# ── 1. Anthropic (Claude) ──────────────────────────────────────────────────────

class AnthropicProvider(BaseProvider):
    """Uses Claude Opus 4.6 via the Anthropic SDK."""

    def __init__(self, api_key: str = None):
        import anthropic as _anthropic
        self.client = _anthropic.Anthropic(api_key=api_key)
        self.model = "claude-opus-4-6"

    def _tool_call(self, tool_def: dict, prompt: str,
                   max_tokens: int = 4096, thinking: bool = False) -> dict:
        kwargs = dict(
            model=self.model, max_tokens=max_tokens,
            tools=[tool_def],
            tool_choice={"type": "tool", "name": tool_def["name"]},
            messages=[{"role": "user", "content": prompt}],
        )
        if thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        resp = self.client.messages.create(**kwargs)
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        return {}

    def extract_profile(self, resume_text: str, preferred_titles: list = None) -> dict:
        from .profile_audit import DOMAIN_TITLE_FAMILIES, FORBIDDEN_GENERIC_TITLES

        tool = {
            "name": "save_profile",
            "description": "Save the extracted resume profile as structured data.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name":     {"type": "string"},
                    "email":    {"type": "string"},
                    "linkedin": {"type": "string"},
                    "github":   {"type": "string"},
                    "phone":    {"type": "string"},
                    "location": {"type": "string"},
                    "target_titles": {
                        "type": "array",
                        "description": (
                            "5–8 domain-specific job titles drawn ONLY from the "
                            "hardware / semiconductor / photonics / embedded title "
                            "families listed in the prompt. Every title MUST include "
                            "an 'evidence' line quoted from the resume's Education or "
                            "Research Experience section justifying it."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "title":    {"type": "string"},
                                "family":   {"type": "string",
                                             "description": "Which title family from the whitelist"},
                                "evidence": {"type": "string",
                                             "description": "Exact line from Education or Research Experience that justifies this title"},
                            },
                            "required": ["title", "family", "evidence"],
                        },
                    },
                    "top_hard_skills": {
                        "type": "array",
                        "description": (
                            "TECHNICAL NOUNS ONLY: programming languages, software "
                            "tools, simulation environments, lab equipment, "
                            "fabrication processes, measurement techniques, hardware "
                            "platforms, named methodologies. NEVER include "
                            "interpersonal traits. Scan EVERY section — coursework, "
                            "research bullets, projects, skills list — and extract "
                            "every technical noun you see. Completeness over brevity."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "skill":    {"type": "string"},
                                "category": {
                                    "type": "string",
                                    "enum": [
                                        "programming_language",
                                        "software_tool",
                                        "simulation_environment",
                                        "fab_process",
                                        "lab_instrument",
                                        "measurement_technique",
                                        "hardware_platform",
                                        "methodology",
                                    ],
                                },
                                "evidence": {
                                    "type": "string",
                                    "description": "Exact substring from the resume where this skill appears.",
                                },
                            },
                            "required": ["skill", "category", "evidence"],
                        },
                    },
                    "top_soft_skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Behavioral/interpersonal traits ONLY (e.g. Teamwork, "
                            "Technical Writing, Project Management). NEVER include "
                            "lab techniques, instruments, software, or languages."
                        ),
                    },
                    "education": {
                        "type": "array",
                        "items": {"type": "object", "properties": {
                            "degree": {"type": "string"}, "institution": {"type": "string"},
                            "year":   {"type": "string"}, "gpa":         {"type": "string"},
                        }},
                    },
                    "research_experience": {
                        "type": "array",
                        "description": "Academic / lab / research roles — anything with a PI, lab, or research group. Keep SEPARATE from work_experience.",
                        "items": {"type": "object", "properties": {
                            "title":   {"type": "string"}, "company": {"type": "string"},
                            "dates":   {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                        }},
                    },
                    "work_experience": {
                        "type": "array",
                        "description": "Industry / internship / part-time jobs (non-research).",
                        "items": {"type": "object", "properties": {
                            "title":   {"type": "string"}, "company": {"type": "string"},
                            "dates":   {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                        }},
                    },
                    "experience": {
                        "type": "array",
                        "description": "Back-compat: union of research_experience + work_experience.",
                        "items": {"type": "object", "properties": {
                            "title":   {"type": "string"}, "company": {"type": "string"},
                            "dates":   {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                        }},
                    },
                    "projects": {
                        "type": "array",
                        "items": {"type": "object", "properties": {
                            "name":        {"type": "string"},
                            "description": {"type": "string"},
                            "skills_used": {"type": "array", "items": {"type": "string"}},
                        }},
                    },
                    "resume_gaps": {"type": "array", "items": {"type": "string"}},
                    "critical_analysis": {
                        "type": "string",
                        "description": "A 3-4 paragraph brutally honest and detailed critique of the resume. Analyze: 1. Impact & Quantified Achievements (or lack thereof), 2. Skill Density vs. Industry Standards, 3. Structural Clarity for ATS and Human Reviewers, 4. Specific high-value action items to land top-tier roles."
                    },
                },
                "required": ["name", "top_hard_skills", "top_soft_skills", "target_titles", "critical_analysis"],
            },
        }

        pref_hint = ""
        if preferred_titles:
            pref_hint = (
                f"\nThe candidate's stated preferences are: {', '.join(preferred_titles)}. "
                "Use these as a TIEBREAKER only — they do NOT override the domain whitelist."
            )

        prompt = (
            "Parse this resume in THREE ORDERED PASSES. Do not skip passes.\n\n"
            "PASS 1 — Section map:\n"
            "Identify and label Education, Research Experience, Work Experience, "
            "Projects, Skills, and Publications. Separate research roles (lab / PI / "
            "research group) from industry roles.\n\n"
            "PASS 2 — Hard-skill extraction (STRICT taxonomy):\n"
            "Hard skills = TECHNICAL NOUNS ONLY: tools, software, programming "
            "languages, lab equipment, fabrication processes, measurement techniques, "
            "simulation environments, named methodologies.\n"
            "Soft skills = BEHAVIORAL / INTERPERSONAL traits ONLY (teamwork, "
            "communication, project management). NEVER place lab techniques, "
            "instruments, or software packages under soft skills.\n"
            "Scan the ENTIRE resume — every bullet under Research Experience, "
            "Projects, Coursework, and the Skills list. List EVERY technical noun. "
            "Completeness > brevity. For each hard skill, include the exact "
            "substring from the resume as `evidence`.\n\n"
            "PASS 3 — Target titles:\n"
            "Suggest 5–8 titles DRAWN ONLY from this whitelist of "
            "hardware / semiconductor / photonics / embedded role families:\n"
            f"  {chr(10).join('  - ' + f for f in DOMAIN_TITLE_FAMILIES)}\n\n"
            "FORBIDDEN titles (do NOT suggest unless the candidate's primary Education "
            "is Computer Science AND there is ZERO lab/fab/device research experience):\n"
            f"  {', '.join(sorted(FORBIDDEN_GENERIC_TITLES))}\n\n"
            "Every suggested title MUST be justified by a specific line from the "
            "Education or Research Experience section — put that line in the "
            "`evidence` field. Weight Education and Research Experience much more "
            "heavily than Work Experience when choosing titles."
            f"{pref_hint}\n\n"
            f"Resume:\n{resume_text}"
        )
        return self._tool_call(tool, prompt, thinking=True)

    def score_job(self, job: dict, profile: dict) -> dict:
        tool = {
            "name": "score_job",
            "description": "Rubric-score a job vs. the candidate using three weighted categories.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "required_skills":    {"type": "number", "minimum": 0, "maximum": 1,
                                           "description": "Fraction of job requirements covered by the candidate's skills (0-1)."},
                    "industry":           {"type": "number", "minimum": 0, "maximum": 1,
                                           "description": "Alignment of job's industry/domain with candidate's target field (0-1)."},
                    "location_seniority": {"type": "number", "minimum": 0, "maximum": 1,
                                           "description": "Combined fit of location and seniority level (0-1)."},
                    "reasoning":          {"type": "string",
                                           "description": "EXACTLY one sentence explaining the overall score."},
                    "matching_skills":    {"type": "array", "items": {"type": "string"}},
                    "missing_skills":     {"type": "array", "items": {"type": "string"}},
                },
                "required": ["required_skills", "industry", "location_seniority", "reasoning"],
            },
        }
        profile_summary = json.dumps({
            "skills":    profile.get("top_hard_skills", []),
            "education": profile.get("education", []),
            "targets":   profile.get("target_titles", []),
        })
        prompt = (
            "Score this job vs. the candidate using EXACTLY three categories, each 0.0-1.0:\n"
            "  - required_skills (weight 50%): fraction of job requirements the candidate covers.\n"
            "  - industry (weight 30%): alignment of job's domain with candidate's target field.\n"
            "  - location_seniority (weight 20%): combined location + seniority fit.\n"
            "Also provide a ONE-sentence 'reasoning' string explaining the overall score.\n\n"
            f"Candidate:\n{profile_summary}\n\n"
            f"Job Title: {job.get('title')}\nCompany: {job.get('company')}\n"
            f"Requirements: {', '.join(job.get('requirements', []))}\n"
            f"Description: {job.get('description', '')}\n"
            f"Location: {job.get('location')} (Remote: {job.get('remote', False)})"
        )
        raw = self._tool_call(tool, prompt, max_tokens=1024)
        return _build_rubric_result(
            job,
            raw.get("required_skills", 0),
            raw.get("industry", 0),
            raw.get("location_seniority", 0),
            matched=raw.get("matching_skills") or [],
            missing=raw.get("missing_skills") or [],
            reasoning=raw.get("reasoning", ""),
        )

    def tailor_resume(self, job: dict, profile: dict, resume_text: str) -> dict:
        tool = {
            "name": "tailored_resume",
            "description": "Return tailored resume sections for this specific job.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "skills_reordered":   {"type": "array", "items": {"type": "string"}},
                    "experience_bullets": {
                        "type": "array",
                        "items": {"type": "object", "properties": {
                            "role":    {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                        }},
                    },
                    "ats_keywords_missing": {"type": "array", "items": {"type": "string"}},
                    "section_order":        {"type": "array", "items": {"type": "string"}},
                },
                "required": ["skills_reordered", "ats_keywords_missing"],
            },
        }
        prompt = (
            f"Tailor this resume for: {job['title']} at {job['company']}.\n"
            "Rules: reorder skills to front-load JD keywords, rephrase bullets naturally — "
            "NEVER fabricate, never change dates/titles/companies. "
            "Do NOT include a summary or objective section.\n"
            "ATS check: flag top JD keywords missing from resume.\n\n"
            f"JD Requirements: {', '.join(job.get('requirements', []))}\n"
            f"JD Description: {job.get('description', '')}\n\n"
            f"Candidate Skills: {', '.join(profile.get('top_hard_skills', []))}\n\n"
            f"Current Resume:\n{resume_text}"
        )
        return self._tool_call(tool, prompt, max_tokens=4096, thinking=True)

    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        name = profile.get("name") or OWNER_NAME
        resp = self.client.messages.create(
            model=self.model, max_tokens=1024,
            messages=[{"role": "user", "content": (
                f"Write a 3-paragraph cover letter for {name} applying to "
                f"{job['title']} at {job['company']}.\n"
                "Para 1: Hook + role name. Para 2: Top 2-3 achievements mapped to JD. "
                "Para 3: Enthusiasm + call to action.\n"
                f"Candidate name: {name}\n"
                f"Candidate skills: {', '.join(profile.get('top_hard_skills', [])[:5])}\n"
                f"Candidate education: "
                f"{(profile.get('education') or [{}])[0].get('degree', '')} at "
                f"{(profile.get('education') or [{}])[0].get('institution', '')}\n"
                f"JD requirements: {', '.join(job.get('requirements', [])[:5])}"
            )}],
        )
        return next(b.text for b in resp.content if b.type == "text")

    def generate_report(self, summary_data: dict) -> str:
        resp = self.client.messages.create(
            model=self.model, max_tokens=1024,
            messages=[{"role": "user", "content": (
                "Generate a concise job application run summary.\n\n"
                f"Data:\n{json.dumps(summary_data, indent=2)}\n\n"
                "Include: overall stats, top 3 applied jobs, manual items, "
                "2-3 recommended next steps. Plain text only."
            )}],
        )
        return next(b.text for b in resp.content if b.type == "text")

    def generate_demo_jobs(self, profile: dict, titles: list, location: str) -> list:
        skills = ", ".join(profile.get("top_hard_skills", [])[:5])
        resp = self.client.messages.create(
            model=self.model, max_tokens=4096,
            messages=[{"role": "user", "content": (
                "Generate 12 realistic internship job postings.\n"
                f"Titles: {', '.join(titles)}\nLocation: {location} or Remote\n"
                f"Key skills: {skills}\n\n"
                "Return a JSON array only (no markdown). Each object: "
                "id, title, company, location, remote (bool), "
                f"posted_date (ISO, last 14 days from {date.today().isoformat()}), "
                "description (2-3 sentences), requirements (array 5-8 strings), "
                "salary_range (string or null), application_url, "
                "platform (LinkedIn|Indeed|Glassdoor|Handshake|Company Site).\n"
                "Focus on IC design, photonics, FPGA, hardware at top EE companies."
            )}],
        )
        raw = next(b.text for b in resp.content if b.type == "text")
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        return json.loads(m.group()) if m else []


# ── 2. Demo (regex / template, no API) ────────────────────────────────────────

# ── PDF / text extraction helpers (shared by all providers) ──────────────────

def _extract_name_from_text(text: str) -> str:
    """Three-tier name extraction from raw resume text.

    Tier 1 — spaCy NER (highest accuracy, optional):
        Uses the 'en_core_web_sm' model to find PERSON entities in the first
        200 characters. Skipped gracefully if spaCy is not installed.

    Tier 2 — First-line heuristic:
        The very first non-empty, non-contact line of a well-structured resume
        is almost always the candidate's name.  Applies strict sanity guards
        (no @, no digits, no URLs, no section-header words, 2–4 tokens).

    Tier 3 — Scored window scan:
        Scans the first 30 lines with a confidence model:
        - higher score for lines appearing earlier
        - bonus for title-case or ALL-CAPS formatting (common in PDF headers)
        - must match a name-safe character pattern
        Best-scoring candidate wins.

    Falls back to OWNER_NAME placeholder if all three tiers fail.
    """
    from .config import OWNER_NAME as _placeholder

    _SECTION_HEADERS = {
        "education", "experience", "skills", "projects", "objective",
        "summary", "profile", "about", "interests", "certifications",
        "publications", "awards", "references", "contact",
        "technical skills", "core competencies", "work experience",
        "professional experience", "research experience",
        "volunteer", "activities", "languages", "coursework",
    }
    _BAD_RE = re.compile(
        r'[@/\\]|https?://|www\.|\.com|\.edu|\.org|\.io|\.net|'
        r'\d{3}[\s.\-]\d{3,4}|'          # phone fragments
        r'\b(?:university|college|institute|school|department|'
        r'gpa|grade|phone|tel|fax|email)\b',
        re.I,
    )
    # Name-safe: each word must match one of:
    #   Title-case word          — Jane, Smith, Van
    #   Irish/hyphenated         — O'Brien, D'Angelo, Jean-Paul
    #   ALL-CAPS word            — JOHN, WILLIAMS
    #   Single initial (± dot)   — A, J, M.
    _WORD_RE = re.compile(
        r"^(?:"
        r"[A-Z][a-z\-]+"                   # plain title-case: Jane, Smith
        r"|[A-Z]['\u2019][A-Z][a-z]+"      # O'Brien / O\u2019Brien
        r"|[A-Z][a-z]*\-[A-Z][a-z]+"       # hyphenated: Jean-Paul
        r"|[A-Z]{2,}"                       # ALL-CAPS: JOHN, WILLIAMS
        r"|[A-Z]\.?"                        # single initial: A or A.
        r")$"
    )

    def _name_re_match(line: str) -> bool:
        words = line.split()
        if not (2 <= len(words) <= 4):
            return False
        return all(_WORD_RE.match(w) for w in words)

    _NAME_RE = _name_re_match  # callable, same interface as re.match

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return _placeholder

    # ── Tier 1: spaCy NER ────────────────────────────────────────────────────
    try:
        import spacy  # noqa: PLC0415
        try:
            nlp = spacy.load("en_core_web_sm")
        except OSError:
            nlp = None
        if nlp is not None:
            # Only run NER on the first ~300 chars; the name is always near the top.
            snippet = " ".join(lines[:10])[:300]
            doc = nlp(snippet)
            for ent in doc.ents:
                if ent.label_ == "PERSON" and len(ent.text.split()) >= 2:
                    # Confirm the token passes basic sanity (no @, no digits).
                    candidate = ent.text.strip()
                    if not _BAD_RE.search(candidate) and _NAME_RE(candidate):
                        return candidate
    except ImportError:
        pass  # spaCy optional — move to next tier

    # ── Tier 2: first-line heuristic ─────────────────────────────────────────
    # The first non-empty line of a resume is the name ~80% of the time.
    first = lines[0]
    if (
        not _BAD_RE.search(first)
        and first.lower().rstrip(":.") not in _SECTION_HEADERS
        and _NAME_RE(first)
    ):
        return first

    # ── Tier 3: scored window scan ────────────────────────────────────────────
    candidates: list[tuple[int, str]] = []

    for i, line in enumerate(lines[:30]):
        if len(line) > 55:          # long lines are addresses / bullets
            continue
        if _BAD_RE.search(line):
            continue
        low = line.lower().rstrip(":,.")
        if low in _SECTION_HEADERS:
            continue
        words = line.split()
        if not (2 <= len(words) <= 4):
            continue
        if not _NAME_RE(line):
            continue

        score = max(0, 15 - i)                              # earlier → higher
        if line == line.title():
            score += 6                                       # Title Case bonus
        elif line == line.upper():
            score += 4                                       # ALL CAPS bonus
        if len(words) == 3:
            score += 2                                       # first middle last
        candidates.append((score, line))

    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    return _placeholder


def _extract_location_from_text(text: str) -> str:
    """Best-effort location extraction from raw resume text.

    Detects locations in order of confidence:
      1. Inline contact separator — "City, ST  |  email  |  phone"
      2. US "City, ST" / "City, State" pattern (2- and 3-word cities).
      3. International "City, Country" pattern.
      4. Standalone US state name in the first 20 lines.
      5. Empty string — never injects a hardcoded location.
    """
    _US_STATES = {
        "Alabama","Alaska","Arizona","Arkansas","California","Colorado",
        "Connecticut","Delaware","Florida","Georgia","Hawaii","Idaho",
        "Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana","Maine",
        "Maryland","Massachusetts","Michigan","Minnesota","Mississippi",
        "Missouri","Montana","Nebraska","Nevada","New Hampshire",
        "New Jersey","New Mexico","New York","North Carolina","North Dakota",
        "Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
        "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont",
        "Virginia","Washington","West Virginia","Wisconsin","Wyoming",
        "District of Columbia",
    }
    _US_STATE_RE = (
        r'(?:' + '|'.join(re.escape(s) for s in _US_STATES) + r'|[A-Z]{2})'
    )
    # Pattern 1: inline separator bar — grab first segment that looks like a location.
    # e.g. "Austin, TX  |  john@email.com  |  (512) 555-1234"
    bar_line_re = re.compile(
        r'\b([A-Z][a-z]+(?:[\s-][A-Z][a-z]+){0,2}),\s*' + _US_STATE_RE + r'\b'
    )
    # Pattern 2: International — "City, Country" where country is title-case.
    intl_re = re.compile(
        r'\b([A-Z][a-z]+(?:[\s-][A-Z][a-z]+){0,2}),\s*'
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\b'
    )

    header = "\n".join(text.splitlines()[:25])   # location always near the top

    m = bar_line_re.search(header)
    if m:
        return m.group(0)

    # Pattern 3: international match (lower confidence — only use if no US match).
    intl_m = intl_re.search(header)

    # Pattern 4: standalone state name scan.
    for line in text.splitlines()[:20]:
        for state in _US_STATES:
            if re.search(rf'\b{re.escape(state)}\b', line, re.I):
                # Prefer "City, State" over bare state.
                m2 = bar_line_re.search(line)
                if m2:
                    return m2.group(0)
                return state

    if intl_m:
        return intl_m.group(0)

    return ""


# ── 2. Demo (regex / template, no API) ────────────────────────────────────────

class DemoProvider(BaseProvider):
    """Template/regex-based provider.  Zero cost, zero setup, fully offline."""

    _DEFAULT_KEYWORDS = [
        "verilog", "vhdl", "fpga", "spice", "matlab", "python", "java", "latex",
        "photolithography", "cleanroom", "pld", "cmos", "pcb", "ltspice",
        "onshape", "fusion360", "solidworks", "cad", "linux", "c++",
        "pulsed laser deposition", "thin film", "sem", "afm",
        "digital design", "analog design", "mixed-signal", "rtl", "synthesis",
    ]

    def __init__(self):
        self.SKILL_KEYWORDS = self._load_keywords()

    @staticmethod
    def _load_keywords() -> list:
        yaml_path = Path("config/skill_keywords.yaml")
        try:
            import yaml
            with open(yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            keywords = []
            for group in data.values():
                if isinstance(group, list):
                    keywords.extend(group)
            return keywords
        except Exception:
            return list(DemoProvider._DEFAULT_KEYWORDS)

    @staticmethod
    def _split_sections(resume_text: str) -> dict:
        """Split a plain-text resume into {section_name_lower: lines[]}.

        Section headers are detected as lines that are mostly uppercase or match
        common resume headings.  Everything before the first header lands in
        the synthetic 'header' bucket.
        """
        known = {
            "experience", "work experience", "professional experience",
            "research experience", "research", "lab experience",
            "projects", "personal projects", "academic projects",
            "education", "skills", "technical skills", "core competencies",
            "coursework", "relevant coursework", "publications",
            "objective", "summary", "profile", "interests",
            "certifications", "awards",
        }
        sections: dict = {"header": []}
        current = "header"
        for raw in resume_text.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                sections.setdefault(current, []).append("")
                continue
            low = stripped.lower().rstrip(":")
            is_header = (
                low in known
                or (len(stripped) <= 40 and stripped == stripped.upper()
                    and any(c.isalpha() for c in stripped) and low in known)
            )
            if is_header:
                current = low
                sections.setdefault(current, [])
            else:
                sections.setdefault(current, []).append(line)
        return sections

    @staticmethod
    def _parse_experience_block(lines: list) -> list:
        """Parse an EXPERIENCE section into [{title, company, dates, bullets}]."""
        roles: list = []
        cur: dict = None
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            indented = raw.startswith(("  ", "\t", "•", "  •", "- "))
            bullet_marker = line.startswith(("•", "-", "*"))
            if not indented and not bullet_marker and ("|" in line or any(
                ch.isdigit() for ch in line
            )):
                # New role header line e.g. "Title | Company | Dates"
                parts = [p.strip() for p in line.split("|")]
                title    = parts[0] if len(parts) > 0 else ""
                company  = parts[1] if len(parts) > 1 else ""
                dates    = parts[2] if len(parts) > 2 else ""
                cur = {"title": title, "company": company, "dates": dates, "bullets": []}
                roles.append(cur)
            else:
                text = line.lstrip("•-* ").strip()
                if not text:
                    continue
                if cur is None:
                    cur = {"title": "", "company": "", "dates": "", "bullets": []}
                    roles.append(cur)
                cur["bullets"].append(text)
        return roles

    @staticmethod
    def _parse_projects_block(lines: list) -> list:
        """Parse a PROJECTS section into [{name, description, skills_used}]."""
        projects: list = []
        cur: dict = None
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            indented = raw.startswith(("  ", "\t"))
            bullet_marker = line.startswith(("•", "-", "*"))
            if not indented and not bullet_marker:
                cur = {"name": line, "description": "", "skills_used": []}
                projects.append(cur)
            else:
                text = line.lstrip("•-* ").strip()
                if cur is None:
                    cur = {"name": "", "description": text, "skills_used": []}
                    projects.append(cur)
                else:
                    cur["description"] = (cur["description"] + " " + text).strip()
        return projects

    @staticmethod
    def _parse_education_block(lines: list) -> list:
        """Parse an EDUCATION section into [{degree, institution, year, gpa}]."""
        entries: list = []
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            year_match = re.search(r"(19|20)\d{2}", line)
            gpa_match  = re.search(r"GPA[:\s]*([\d.]+)", line, re.I)
            entry = {
                "degree":      parts[0] if parts else "",
                "institution": parts[1] if len(parts) > 1 else "",
                "year":        year_match.group() if year_match else (
                    parts[2] if len(parts) > 2 else ""
                ),
                "gpa":         gpa_match.group(1) if gpa_match else "",
            }
            entries.append(entry)
        return entries

    def _skills_from_text(self, text: str, limit: int = 40) -> list:
        text_lower = text.lower()
        found = [s for s in self.SKILL_KEYWORDS if s.lower() in text_lower]
        skill_display = {
            "verilog": "Verilog", "vhdl": "VHDL", "fpga": "FPGA", "spice": "SPICE",
            "matlab": "MATLAB", "python": "Python", "java": "Java", "latex": "LaTeX",
            "photolithography": "Photolithography", "cleanroom": "Cleanroom Processes",
            "pld": "Pulsed Laser Deposition", "cmos": "CMOS", "pcb": "PCB Design",
            "onshape": "OnShape", "fusion360": "Fusion360", "solidworks": "SolidWorks",
            "cad": "CAD", "linux": "Linux", "c++": "C++",
            "pulsed laser deposition": "Pulsed Laser Deposition",
            "thin film": "Thin Film Deposition", "sem": "SEM", "afm": "AFM",
            "digital design": "Digital Design", "analog design": "Analog Design",
            "mixed-signal": "Mixed-Signal",
        }
        out = []
        seen = set()
        for skill in found:
            label = skill_display.get(skill.lower(), skill.title())
            if label.lower() not in seen:
                seen.add(label.lower())
                out.append(label)
        return out[:limit]

    @staticmethod
    def _summary_from_profile(name: str, titles: list, skills: list, experience: list, research: list) -> str:
        role = titles[0] if titles else "engineering candidate"
        skill_text = ", ".join(skills[:6])
        count = len(experience or []) + len(research or [])
        base = f"{name} is a {role}"
        if skill_text:
            base += f" with hands-on experience across {skill_text}"
        if count:
            base += f" and {count} structured resume role(s) extracted for job matching"
        return base + "."

    def extract_profile(self, resume_text: str, preferred_titles: list = None) -> dict:
        text_lower = resume_text.lower()

        email_match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}', resume_text)
        email = email_match.group() if email_match else ""

        linkedin_match = re.search(r'linkedin\.com/in/[\w-]+', resume_text, re.I)
        linkedin = linkedin_match.group() if linkedin_match else ""

        github_match = re.search(r'github\.com/[\w-]+', resume_text, re.I)
        github = github_match.group() if github_match else ""

        phone_match = re.search(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', resume_text)
        phone = phone_match.group() if phone_match else ""

        hard_skills = self._skills_from_text(resume_text)
        if not hard_skills:
            hard_skills = ["MATLAB", "Python", "Verilog", "FPGA", "SPICE"]

        name = _extract_name_from_text(resume_text)
        location = _extract_location_from_text(resume_text)

        title_map = {
            "fpga":          "FPGA/Hardware Engineering Intern",
            "photolithography": "Photonics Engineering Intern",
            "photonics":     "Photonics Engineering Intern",
            "spice":         "IC Design Engineering Intern",
            "verilog":       "IC Design Engineering Intern",
            "vhdl":          "VLSI Design Engineering Intern",
            "cmos":          "IC Design Engineering Intern",
            "pcb":           "Hardware Engineering Intern",
            "mixed-signal":  "Mixed-Signal Design Intern",
            "semiconductor": "Semiconductor Process Engineering Intern",
            "thin film":     "Thin Film / Materials Engineering Intern",
        }
        inferred = list({title_map[k] for k in title_map if k in text_lower})
        seen: set = set()
        target_titles: list = []
        for t in (preferred_titles or []) + inferred:
            if t not in seen:
                seen.add(t)
                target_titles.append(t)
        if not target_titles:
            target_titles = ["IC Design Engineering Intern", "Hardware Engineering Intern"]

        gaps = []
        if "summary" not in text_lower and "objective" not in text_lower:
            gaps.append("Missing professional summary/objective")
        if not re.search(r'\d+%|\d+ students|\d+ projects', resume_text):
            gaps.append("Few quantified achievements — add metrics")

        # Parse free-form sections out of the resume text.
        sections = self._split_sections(resume_text)

        def _grab(*keys):
            for k in keys:
                if k in sections and sections[k]:
                    return sections[k]
            return []

        experience = self._parse_experience_block(
            _grab("experience", "work experience", "professional experience")
        )
        research = self._parse_experience_block(
            _grab("research experience", "research", "lab experience")
        )
        projects = self._parse_projects_block(
            _grab("projects", "personal projects", "academic projects")
        )
        for project in projects:
            project["skills_used"] = self._skills_from_text(
                f"{project.get('name', '')} {project.get('description', '')}",
                limit=10,
            )
        education_parsed = self._parse_education_block(_grab("education"))
        summary = self._summary_from_profile(name, target_titles, hard_skills, experience, research)
        critical = (
            "Impact: add numeric outcomes to the strongest bullets wherever possible. "
            "Skill density: the parser found the technical keywords listed in hard skills; add any missing tools, instruments, and methods explicitly. "
            "ATS structure: keep Education, Experience, Projects, and Skills as clear headings. "
            "Next actions: add LinkedIn, work authorization, target salary, and target titles to improve matching and autofill."
        )

        return {
            "name": name, "email": email, "linkedin": linkedin, "github": github, "phone": phone,
            "location": location,
            "summary": summary,
            "target_titles": target_titles,
            "top_hard_skills": hard_skills,
            "top_soft_skills": ["Teamwork", "Problem-solving", "Communication",
                                 "Attention to detail", "Time management"],
            "education":  education_parsed,
            "experience": experience,
            "work_experience": experience,
            "research": research,
            "research_experience": research,
            "projects":   projects,
            "resume_gaps": gaps,
            "critical_analysis": critical,
        }

    def score_job(self, job: dict, profile: dict) -> dict:
        skills_lower = {s.lower() for s in profile.get("top_hard_skills", [])}
        reqs = [r.lower() for r in job.get("requirements", [])]
        matched = [r for r in reqs if any(s in r or r in s for s in skills_lower)]
        req_raw = (len(matched) / len(reqs)) if reqs else 0.5

        title_lower = job.get("title", "").lower()
        targets_l = [t.lower() for t in profile.get("target_titles", [])]
        industry_raw = 1.0 if any(
            any(w in title_lower for w in t.split()) for t in targets_l
        ) else 0.5

        loc = job.get("location", "").lower()
        remote_ok = job.get("remote", False)
        # Score location dynamically: remote always passes; "united states"
        # is treated as a broad positive signal. No hardcoded city/state.
        loc_raw = 1.0 if (remote_ok or "united states" in loc or "us" == loc.strip()) else 0.5
        exp_ok = job.get("experience_level", "internship") in ("internship", "entry-level")
        loc_seniority_raw = (loc_raw + (1.0 if exp_ok else 0.5)) / 2

        missing = [r.title() for r in reqs if r not in matched and len(r) > 3][:5]
        return _build_rubric_result(job, req_raw, industry_raw, loc_seniority_raw,
                                    matched=matched, missing=missing)

    def tailor_resume(self, job: dict, profile: dict, resume_text: str) -> dict:  # noqa: ARG002
        jd_keywords  = [r.lower() for r in job.get("requirements", [])]
        skills       = profile.get("top_hard_skills", [])
        skills_lower = {s.lower(): s for s in skills}

        matching         = [skills_lower[k] for k in jd_keywords if k in skills_lower]
        other            = [s for s in skills if s not in matching]
        skills_reordered = matching + other

        missing_kw = [
            r.title() for r in jd_keywords
            if not any(r in s.lower() or s.lower() in r for s in skills)
        ]
        return {
            "skills_reordered":     skills_reordered,
            "experience_bullets":   [],
            "ats_keywords_missing": missing_kw[:5],
            "section_order":        ["Skills", "Projects", "Experience", "Education"],
        }

    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        name       = profile.get("name") or OWNER_NAME
        email      = profile.get("email") or ""
        skills_str = ", ".join(profile.get("top_hard_skills", [])[:3])
        edu        = (profile.get("education") or [{}])[0]
        degree     = edu.get("degree") or "Engineering"
        university = edu.get("institution") or "my university"
        top_reqs   = ", ".join((job.get("requirements") or [])[:3])
        sign_off   = f"{name}" + (f"\n{email}" if email else "")
        return (
            f"Dear {job['company']} Hiring Team,\n\n"
            f"I am writing to express my strong interest in the {job['title']} position "
            f"at {job['company']}. As a {degree} student at {university} with experience "
            f"in {skills_str}, I am eager to contribute to your team.\n\n"
            f"My technical background in {skills_str} maps directly to your listed "
            f"requirements ({top_reqs}). I have applied these skills through coursework, "
            f"research, and hands-on projects and am confident I can add value quickly.\n\n"
            f"I would welcome the opportunity to discuss how my background aligns with "
            f"{job['company']}'s goals. Thank you for your consideration.\n\n"
            f"Sincerely,\n{sign_off}"
        )

    def generate_report(self, summary_data: dict) -> str:
        top3 = summary_data.get("top3_applied", [])
        top3_lines = "\n".join(
            f"  {i+1}. {c} — {t} (score: {s})"
            for i, (c, t, s) in enumerate(top3)
        )
        manual = summary_data.get("manual", 0)
        return (
            f"Run Summary — {date.today().isoformat()}\n\n"
            "Results:\n"
            f"  • Jobs evaluated:       {summary_data.get('total_found', 0)}\n"
            f"  • Applications sent:    {summary_data.get('applied', 0)}\n"
            f"  • Manual review needed: {manual}\n"
            f"  • Skipped (low match):  {summary_data.get('skipped', 0)}\n\n"
            f"Top Jobs Applied To:\n{top3_lines}\n\n"
            + (f"Manual Review ({manual} item(s)):\n"
               + "\n".join(f"  - {r}" for r in summary_data.get("manual_reasons", []))
               + "\n\n" if manual else "")
            + "Recommended Next Steps:\n"
              "  1. Add quantified metrics to resume bullets (e.g., 'reduced error rate by 20%').\n"
              "  2. Follow up on applied jobs in 7 days via LinkedIn or email.\n"
              "  3. Update skills section with any ATS gaps flagged in tailored resumes.\n"
        )

    def generate_demo_jobs(self, profile: dict, titles: list, location: str) -> list:  # noqa: ARG002
        return DEMO_JOBS


# ── 3. Ollama (local LLM) ──────────────────────────────────────────────────────

class OllamaProvider(BaseProvider):
    """Uses a local Ollama model.  Free, no API key, requires Ollama installed."""

    OLLAMA_URL = "http://localhost:11434"

    def __init__(self, model: str = "llama3.2"):
        self.model = model
        self._check_ollama()

    def _check_ollama(self):
        import urllib.request as _ur
        import json as _json

        try:
            resp = _ur.urlopen(f"{self.OLLAMA_URL}/api/tags", timeout=5)
            data = _json.loads(resp.read().decode())
        except Exception as e:
            raise ConnectionError(
                f"Ollama is not reachable at {self.OLLAMA_URL}.\n"
                f"Start it with:  ollama serve\n(error: {e})"
            ) from e

        models      = data.get("models", [])
        local_bases = {m.get("name", "").split(":")[0] for m in models}
        local_full  = {m.get("name", "") for m in models}
        req_base    = self.model.split(":")[0]

        if self.model not in local_full and req_base not in local_bases:
            available = ", ".join(sorted(local_bases)) or "none"
            raise ValueError(
                f"Model '{self.model}' is not pulled in Ollama.\n"
                f"Available: {available}\n"
                f"Fix: ollama pull {self.model}"
            )

    def _chat(self, prompt: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "openai package required for Ollama mode.  Run: pip install openai"
            ) from exc

        import time as _time
        oc = OpenAI(base_url=f"{self.OLLAMA_URL}/v1", api_key="ollama", timeout=120)
        for attempt in range(5):
            try:
                resp = oc.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp.choices[0].message.content or ""
            except Exception as e:
                if "429" in str(e) or "too many concurrent" in str(e).lower():
                    wait = 2 ** attempt
                    console.print(
                        f"  [yellow]⏳ Ollama busy — retrying in {wait}s "
                        f"(attempt {attempt+1}/5)…[/yellow]"
                    )
                    _time.sleep(wait)
                else:
                    raise
        raise RuntimeError("Ollama rate limit: exceeded 5 retry attempts")

    def _parse_json(self, text: str, fallback: dict) -> dict:
        def _try(s: str) -> dict | None:
            try:
                obj = json.loads(s.strip())
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            return None

        def _fix_and_try(s: str) -> dict | None:
            # Fix trailing commas before ] or } — extremely common Ollama mistake.
            fixed = re.sub(r',\s*([}\]])', r'\1', s)
            return _try(fixed)

        candidates = [
            text,
            re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.M),
            # strip everything before the first { and after the last }
            text[text.find('{'):text.rfind('}')+1] if '{' in text else '',
        ]
        for c in candidates:
            if not c:
                continue
            result = _try(c) or _fix_and_try(c)
            if result:
                return result

        # Last-resort: find the largest {...} block
        for m in re.finditer(r'\{', text):
            depth, i = 0, m.start()
            for j, ch in enumerate(text[i:], i):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        blob = text[i:j+1]
                        result = _try(blob) or _fix_and_try(blob)
                        if result:
                            return result
                        break
        return fallback

    def extract_profile(self, resume_text: str, preferred_titles: list = None) -> dict:
        from .profile_audit import DOMAIN_TITLE_FAMILIES

        pref_hint = ""
        if preferred_titles:
            pref_hint = f"\nPreferences: {', '.join(preferred_titles)}"
        prompt = (
            "Extract resume info. Return ONLY a JSON object with these fields:\n"
            '{"name": str, "email": str, "linkedin": str, "github": str, "phone": str, "location": str,\n'
            '"target_titles": [str], "top_hard_skills": [str], "top_soft_skills": [str],\n'
            '"education": [{"degree": str, "institution": str, "year": str, "gpa": str}],\n'
            '"research_experience": [{"title": str, "company": str, "dates": str, "bullets": [str]}],\n'
            '"work_experience": [{"title": str, "company": str, "dates": str, "bullets": [str]}],\n'
            '"projects": [{"name": str, "description": str, "skills_used": [str]}],\n'
            '"resume_gaps": [str],\n'
            '"critical_analysis": str}\n\n'
            "critical_analysis: A 3-4 paragraph brutally honest and detailed critique of the resume focusing on: 1. Impact & Quantified Achievements, 2. Skill Density, 3. Structural Clarity for ATS/Human, 4. Specific high-value action items.\n"
            f"Target title families (extract titles matching these): {', '.join(DOMAIN_TITLE_FAMILIES)}\n"
            "Hard skills: ONLY technical tools, languages, equipment. No soft skills here.\n"
            "Soft skills: ONLY behavioral (communication, teamwork, etc.).\n"
            f"{pref_hint}\n\n"
            f"Resume:\n{resume_text[:3000]}"
        )
        raw = self._chat(prompt)
        result = self._parse_json(raw, {})
        if not result:
            # Ollama returned unparse-able output — fall back to regex extraction
            # so the user's real resume data is never silently replaced with defaults.
            console.print("  [yellow]⚠  Ollama JSON parse failed — using regex extractor as fallback[/yellow]")
            return DemoProvider().extract_profile(resume_text, preferred_titles=preferred_titles)
        return result

    def score_job(self, job: dict, profile: dict) -> dict:
        prompt = (
            "Score this job against the candidate using EXACTLY three categories, "
            "each a float 0.0-1.0:\n"
            "  required_skills (weight 50%): fraction of requirements covered.\n"
            "  industry (weight 30%): domain alignment with candidate targets.\n"
            "  location_seniority (weight 20%): location + seniority fit.\n"
            "Return ONLY a JSON object with keys: required_skills, industry, "
            "location_seniority, reasoning (ONE sentence), "
            "matching_skills (array), missing_skills (array).\n\n"
            f"Candidate skills: {', '.join(profile.get('top_hard_skills', []))}\n"
            f"Target titles: {', '.join(profile.get('target_titles', []))}\n\n"
            f"Job: {job.get('title')} at {job.get('company')}\n"
            f"Requirements: {', '.join(job.get('requirements', []))}\n"
            f"Location: {job.get('location')} (Remote: {job.get('remote', False)})"
        )
        raw = self._chat(prompt)
        parsed = self._parse_json(raw, {
            "required_skills": 0.5, "industry": 0.5, "location_seniority": 0.5,
            "reasoning": "Scored by Ollama (fallback).",
            "matching_skills": [], "missing_skills": [],
        })
        return _build_rubric_result(
            job,
            parsed.get("required_skills", 0),
            parsed.get("industry", 0),
            parsed.get("location_seniority", 0),
            matched=parsed.get("matching_skills") or [],
            missing=parsed.get("missing_skills") or [],
            reasoning=parsed.get("reasoning", ""),
        )

    def tailor_resume(self, job: dict, profile: dict, resume_text: str) -> dict:
        prompt = (
            f"Tailor this resume for '{job['title']}' at '{job['company']}'.\n"
            "Return ONLY a JSON object with:\n"
            "  skills_reordered (array, JD-matching skills first),\n"
            "  experience_bullets (array of {role, bullets[]}),\n"
            "  ats_keywords_missing (array of JD keywords not in resume),\n"
            "  section_order (array of section names, do NOT include Summary or Objective).\n"
            "Do NOT include a summary or objective field. "
            "NEVER fabricate experience. Only rephrase what exists.\n\n"
            f"JD Requirements: {', '.join(job.get('requirements', []))}\n"
            f"Candidate Skills: {', '.join(profile.get('top_hard_skills', []))}\n\n"
            f"Resume (excerpt):\n{resume_text[:2000]}"
        )
        raw = self._chat(prompt)
        return self._parse_json(raw, {
            "skills_reordered":     profile.get("top_hard_skills", []),
            "experience_bullets":   [],
            "ats_keywords_missing": [],
            "section_order":        ["Skills", "Projects", "Experience", "Education"],
        })

    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        prompt = (
            f"Write a 3-paragraph cover letter for {OWNER_NAME} applying to "
            f"{job['title']} at {job['company']}. "
            "Para 1: hook + role name. Para 2: 2-3 achievements mapped to JD. "
            "Para 3: enthusiasm + CTA. Professional and concise.\n"
            f"Candidate skills: {', '.join(profile.get('top_hard_skills', [])[:5])}"
        )
        return self._chat(prompt)

    def generate_report(self, summary_data: dict) -> str:
        prompt = (
            "Write a concise job application run summary (plain text).\n"
            "Include: overall stats, top 3 jobs, manual items, 2-3 next steps.\n\n"
            f"Data:\n{json.dumps(summary_data, indent=2)}"
        )
        return self._chat(prompt)

    def generate_demo_jobs(self, profile: dict, titles: list, location: str) -> list:
        prompt = (
            f"Generate 10 realistic internship job postings for titles: {', '.join(titles)}. "
            f"Location: {location} or Remote. "
            f"Skills focus: {', '.join(profile.get('top_hard_skills', [])[:5])}.\n"
            "Return ONLY a JSON array. Each item must have: "
            "id, title, company, location, remote (bool), "
            f"posted_date (ISO, within last 14 days from {date.today().isoformat()}), "
            "description (2 sentences), requirements (array 5-8 strings), "
            "salary_range, application_url, platform."
        )
        raw = self._chat(prompt)
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return DEMO_JOBS


# ── Factory ────────────────────────────────────────────────────────────────────

def get_provider(args) -> BaseProvider:
    if args.demo:
        console.print("[dim]Mode: Demo (no API key required)[/dim]")
        return DemoProvider()
    if args.ollama:
        console.print(f"[dim]Mode: Ollama local LLM (model: {args.model})[/dim]")
        return OllamaProvider(model=args.model)
    console.print("[dim]Mode: Anthropic Claude Opus 4.6[/dim]")
    return AnthropicProvider()

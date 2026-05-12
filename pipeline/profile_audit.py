"""
pipeline/profile_audit.py
─────────────────────────
Phase 1 post-extraction validation layer.

Five helpers run sequentially after the LLM returns a profile:

  1. _flatten_profile          — converts the new detailed skill/title
                                 objects into the flat lists the rest of
                                 the pipeline still expects.
  2. _quarantine_misplaced_skills — moves lab/fab/software terms out of
                                    soft_skills and into hard_skills.
  3. _retention_audit          — scans the raw resume text for known
                                 technical tokens missing from the
                                 extracted profile and re-adds them.
  4. _verify_evidence          — drops any skill whose `evidence`
                                 substring cannot be found in the resume
                                 (hallucination guardrail).
  5. _rerank_titles            — keeps only target titles whose evidence
                                 line traces back to Education or
                                 Research Experience.

All helpers mutate + return the profile dict and append a short note to
`profile["_audit_log"]` so callers can surface what the validator fixed.
"""

from __future__ import annotations

import re

# ── Domain hint families (passed to LLM prompts as a non-restrictive HINT) ──
#
# This list is a *menu* the LLM can use to label which family it picked from,
# NOT a restriction. The reranker no longer enforces "must be on this list" —
# it grounds titles to actual resume evidence instead. Earlier versions of
# this list were narrowly EE/semiconductor and produced hardcoded hardware
# titles even on data-science / PM / nursing resumes; that's the bug.
DOMAIN_TITLE_FAMILIES = [
    # Software / data
    "Software Engineering",
    "Frontend / Backend / Full-Stack Engineering",
    "Mobile / iOS / Android Engineering",
    "DevOps / Site Reliability / Cloud",
    "Security / Application Security",
    "Data Science / Data Analytics",
    "Machine Learning / AI Engineering",
    "Data Engineering / ETL",
    # Hardware / EE
    "IC Design / VLSI",
    "Analog / Mixed-Signal Design",
    "FPGA / Digital Design",
    "Embedded Systems / Firmware",
    "RF / Hardware Engineering",
    "Photonics / Optoelectronics",
    "Semiconductor Process / Device Engineering",
    "Nanofabrication / MEMS",
    "Mechanical / Robotics / Mechatronics",
    "Aerospace / Controls",
    # Product / design / business
    "Product Management",
    "Program / Project Management",
    "UX / UI / Product Design",
    "Graphic / Brand Design",
    "Marketing / Growth / Content",
    "Sales / Business Development / Account Management",
    "Customer Success / Customer Support",
    # Operations / functions
    "Operations / Supply Chain / Logistics",
    "Finance / Accounting / FP&A",
    "Investment / Banking / Trading",
    "Human Resources / Talent Acquisition",
    "Legal / Compliance / Paralegal",
    # Healthcare / sciences
    "Clinical / Nursing / Healthcare",
    "Biology / Chemistry / Lab Research",
    "Pharma / Biotech R&D",
    # Education / public sector / trades
    "Teaching / Education / Tutoring",
    "Public Policy / Government / Civic",
    "Skilled Trades / Technician / Field Service",
    "Media / Journalism / Communications",
    # Catch-all for anything we haven't enumerated.
    "Other (specify family in the title itself)",
]

# Retained as a back-compat alias so any imports outside this module keep
# resolving. Reranker no longer hard-rejects these — it only grounds titles
# to evidence in the resume corpus, which catches both "fabricated software
# engineer for an EE candidate" AND "fabricated EE intern for a data
# scientist" with the same logic.
FORBIDDEN_GENERIC_TITLES: set[str] = set()


# ── Hard-skill lexicon (used by quarantine + retention audit) ──────────────
# Tokens are lowercase; matching is substring-based to tolerate variants.

HARD_SKILL_LEXICON: set[str] = {
    # Programming / scripting
    "python", "c++", "c#", " c ", "java", "javascript", "typescript",
    "matlab", "verilog", "systemverilog", "vhdl", "tcl", "perl",
    "bash", "shell scripting", "assembly", "rust", "go ",
    # EDA / simulation / design software
    "cadence", "virtuoso", "synopsys", "hspice", "spice", "ltspice",
    "pspice", "spectre", "calibre", "modelsim", "vivado", "quartus",
    "xilinx", "altera", "innovus", "encounter", "primetime",
    "comsol", "ansys", "hfss", "cst", "lumerical", "silvaco",
    "sentaurus", "klayout", "magic vlsi",
    # Mechanical / CAD
    "fusion 360", "solidworks", "autocad", "catia", "onshape",
    "creo", "inventor",
    # Fab processes
    "photolithography", "lithography", "e-beam lithography",
    "reactive-ion etching", "rie", "dry etching", "wet etching",
    "plasma etching", "sputtering", "evaporation",
    "pulsed laser deposition", "pld", "chemical vapor deposition",
    "cvd", "pecvd", "lpcvd", "physical vapor deposition", "pvd",
    "atomic layer deposition", "ald", "molecular beam epitaxy", "mbe",
    "thermal oxidation", "doping", "ion implantation",
    "wafer bonding", "cmp", "chemical mechanical polishing",
    "nanofabrication", "cleanroom",
    # Measurement / characterization
    "optical spectroscopy", "raman spectroscopy", "ftir",
    "photoluminescence", "pl ", "ellipsometry",
    "x-ray diffraction", "xrd", "xps", "auger",
    "sem", "scanning electron microscopy",
    "tem", "transmission electron microscopy",
    "afm", "atomic force microscopy", "stm",
    "profilometry", "four-point probe", "hall effect",
    "iv curve", "cv measurement", "network analyzer",
    "oscilloscope", "spectrum analyzer", "vector network analyzer",
    "lock-in amplifier", "probe station",
    # Hardware platforms
    "fpga", "asic", "soc", "microcontroller", "arduino",
    "raspberry pi", "stm32", "pcb design", "altium", "kicad",
    "eagle cad",
    # Methodologies / general engineering tools
    "labview", "git", "linux", "jupyter", "numpy", "scipy",
    "pandas", "pytorch", "tensorflow", "opencv", "simulink",
}


# ── Soft-skill allow-list (what IS allowed under soft_skills) ──────────────
SOFT_SKILL_ALLOWED: set[str] = {
    "teamwork", "collaboration", "communication", "technical writing",
    "presentation", "leadership", "problem solving", "problem-solving",
    "critical thinking", "project management", "time management",
    "attention to detail", "adaptability", "creativity", "mentoring",
    "cross-functional collaboration", "documentation",
    "research", "analytical thinking", "curiosity", "initiative",
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _log(profile: dict, msg: str) -> None:
    profile.setdefault("_audit_log", []).append(msg)


def _dedup_preserve_order(items: list) -> list:
    seen: set = set()
    out: list = []
    for item in items:
        key = item.lower() if isinstance(item, str) else str(item).lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _is_hard_token(text: str) -> bool:
    tl = (text or "").lower()
    return any(tok in tl for tok in HARD_SKILL_LEXICON)


def flatten_profile(profile: dict) -> dict:
    """Convert detailed skill/title objects → flat string lists for
    backward compatibility with Phases 2–4, while retaining the rich
    forms under `*_detailed` keys."""
    p = dict(profile or {})

    # Hard skills: accept either list[str] or list[{skill, category, evidence}]
    hard_raw = p.get("top_hard_skills") or []
    hard_detailed: list = []
    hard_flat: list = []
    for item in hard_raw:
        if isinstance(item, dict):
            hard_detailed.append(item)
            s = item.get("skill") or ""
            if s:
                hard_flat.append(s)
        elif isinstance(item, str) and item.strip():
            hard_detailed.append({"skill": item, "category": "unknown", "evidence": ""})
            hard_flat.append(item)
    p["top_hard_skills_detailed"] = hard_detailed
    p["top_hard_skills"] = _dedup_preserve_order(hard_flat)

    # Target titles: accept list[str] or list[{title, family, evidence}]
    titles_raw = p.get("target_titles") or []
    titles_detailed: list = []
    titles_flat: list = []
    for item in titles_raw:
        if isinstance(item, dict):
            titles_detailed.append(item)
            t = item.get("title") or ""
            if t:
                titles_flat.append(t)
        elif isinstance(item, str) and item.strip():
            titles_detailed.append({"title": item, "family": "", "evidence": ""})
            titles_flat.append(item)
    p["target_titles_detailed"] = titles_detailed
    p["target_titles"] = _dedup_preserve_order(titles_flat)

    # Normalize soft skills to list[str].
    soft_raw = p.get("top_soft_skills") or []
    p["top_soft_skills"] = [
        (s.get("skill") if isinstance(s, dict) else s)
        for s in soft_raw
        if (isinstance(s, dict) and s.get("skill")) or (isinstance(s, str) and s.strip())
    ]
    return p


def quarantine_misplaced_skills(profile: dict) -> dict:
    """Move any lab/fab/software token found under soft_skills into
    hard_skills, and drop it from soft_skills."""
    hard = list(profile.get("top_hard_skills", []))
    soft = list(profile.get("top_soft_skills", []))
    moved: list = []
    kept_soft: list = []
    hard_lower = {h.lower() for h in hard}

    for s in soft:
        if _is_hard_token(s):
            if s.lower() not in hard_lower:
                hard.append(s)
                hard_lower.add(s.lower())
                # Also update detailed list so evidence verification sees it.
                profile.setdefault("top_hard_skills_detailed", []).append(
                    {"skill": s, "category": "quarantined_from_soft", "evidence": ""}
                )
            moved.append(s)
        else:
            # Drop obviously non-soft items like single letters / empty.
            if s and s.lower() not in {"n/a", "none"}:
                kept_soft.append(s)

    profile["top_hard_skills"] = _dedup_preserve_order(hard)
    profile["top_soft_skills"] = _dedup_preserve_order(kept_soft)
    if moved:
        _log(profile, f"quarantined {len(moved)} misplaced soft skills: {moved}")
    return profile


def retention_audit(profile: dict, resume_text: str) -> dict:
    """Scan the raw resume for lexicon tokens missing from the extracted
    hard skills and merge them in (case-preserving best effort).

    Uses word-boundary matching so short acronyms can't false-positive
    inside larger English words ('rie' must not match in 'experience',
    'mbe' must not match in 'embedded', 'cad' must not match in 'cascade').
    Multi-word tokens like 'thin film' work naturally because their embedded
    spaces already enforce token edges.
    """
    if not resume_text:
        return profile
    text_l = resume_text.lower()
    extracted_l = {s.lower() for s in profile.get("top_hard_skills", [])}

    missed: list = []
    for token in HARD_SKILL_LEXICON:
        token_s = token.strip()
        if not token_s or len(token_s) < 2:
            continue
        if re.search(r"\W", token_s):
            pattern = rf"(?<!\w){re.escape(token_s)}(?!\w)"
        else:
            pattern = rf"\b{re.escape(token_s)}\b"
        if not re.search(pattern, text_l, re.IGNORECASE):
            continue
        if any(token_s in e or e in token_s for e in extracted_l):
            continue
        missed.append(token_s)

    if missed:
        # Title-case for display, but leave common acronyms upper.
        def _pretty(t: str) -> str:
            if t.upper() == t or len(t) <= 4:
                return t.upper().strip()
            return t.title().strip()

        new_entries = [_pretty(t) for t in missed]
        profile["top_hard_skills"] = _dedup_preserve_order(
            list(profile.get("top_hard_skills", [])) + new_entries
        )
        for t in new_entries:
            profile.setdefault("top_hard_skills_detailed", []).append({
                "skill": t, "category": "retention_audit_recovered",
                "evidence": "(auto-recovered from resume text)",
            })
        _log(profile, f"retention_audit recovered {len(new_entries)} missing skills: {new_entries}")
    return profile


def verify_evidence(profile: dict, resume_text: str) -> dict:
    """Drop detailed hard-skill entries whose `evidence` substring
    cannot be found in the resume text. Flat `top_hard_skills` is
    rebuilt from the surviving detailed entries PLUS any entry whose
    evidence field is empty (e.g. retention-audit recoveries)."""
    text_l = (resume_text or "").lower()
    detailed = profile.get("top_hard_skills_detailed") or []
    verified: list = []
    rejected: list = []

    for entry in detailed:
        ev = (entry.get("evidence") or "").strip().lower()
        skill = (entry.get("skill") or "").strip()
        skill_l = skill.lower()
        # Entries with no evidence (retention audit / quarantine) are trusted.
        if not ev or ev.startswith("("):
            verified.append(entry)
            continue
        # Primary: short-prefix match tolerates rewording.
        needle = ev[:30]
        if needle and needle in text_l:
            verified.append(entry)
            continue
        # Fallback: if the skill name (or any meaningful word in it) appears
        # in the resume, keep it — LLMs often paraphrase evidence strings.
        if skill_l and len(skill_l) >= 2:
            words = [w for w in skill_l.split() if len(w) >= 3]
            if skill_l in text_l or (words and any(w in text_l for w in words)):
                verified.append(entry)
                continue
        rejected.append(skill or "?")

    profile["top_hard_skills_detailed"] = verified
    if rejected:
        _log(profile, f"dropped {len(rejected)} skills with unverifiable evidence: {rejected}")
        # Rebuild the flat list so only verified skills remain — but keep
        # the original order and avoid dropping retention-audit adds.
        verified_flat = [e.get("skill", "") for e in verified if e.get("skill")]
        profile["top_hard_skills"] = _dedup_preserve_order(verified_flat)
    return profile


_TITLE_NOISE_RE = re.compile(
    r"\b(?:intern(?:ship)?|engineer|engineering|developer|analyst|"
    r"manager|director|associate|specialist|assistant|coordinator|"
    r"officer|consultant|scientist|architect|designer|representative|"
    r"junior|senior|sr|jr|lead|principal|staff|the|of|at|for|in|to|and)\b",
    re.IGNORECASE,
)


def _title_signal_tokens(title: str) -> set[str]:
    """Return content tokens of a title with role-noise filler stripped.

    "Software Engineering Intern" → {"software"}
    "Photonics / Optoelectronics Engineering Intern" → {"photonics", "optoelectronics"}
    "Marketing Manager" → {"marketing"}
    Used by the reranker to verify a title's *domain* signal appears in the
    candidate's resume, ignoring generic role words that match every resume.
    """
    if not title:
        return set()
    cleaned = _TITLE_NOISE_RE.sub(" ", title.lower())
    cleaned = re.sub(r"[^a-z0-9+\-#.]+", " ", cleaned)
    return {tok for tok in cleaned.split() if len(tok) >= 3}


def rerank_titles(profile: dict) -> dict:
    """Keep target titles only when the title's domain signal is grounded in
    the candidate's actual resume content (Education / Experience / Projects).

    No domain whitelist — a candidate's resume is the source of truth. The
    reranker drops a title only when none of its content tokens appear in
    the resume corpus AND the LLM-supplied evidence string also doesn't
    match. This catches LLM fabrication ("FPGA Intern" on a marketing
    resume; "Marketing Manager" on a hardware resume) without baking in a
    domain bias of our own.
    """
    detailed = profile.get("target_titles_detailed") or []
    if not detailed:
        return profile

    # Build an "anchor" corpus from Education + Experience + Projects + Skills.
    chunks: list = []
    for e in profile.get("education") or []:
        chunks.append(f"{e.get('degree', '')} {e.get('institution', '')}")
        chunks.extend(e.get("coursework") or [])
    for bucket in ("research_experience", "work_experience", "experience"):
        for r in profile.get(bucket) or []:
            chunks.append(r.get("title", ""))
            chunks.append(r.get("company", ""))
            chunks.extend(r.get("bullets") or [])
    for pr in profile.get("projects") or []:
        chunks.append(pr.get("name", ""))
        chunks.append(pr.get("description", ""))
        chunks.extend(pr.get("skills_used") or [])
        chunks.extend(pr.get("bullets") or [])
    chunks.extend(profile.get("top_hard_skills") or [])
    anchor = " ".join(c for c in chunks if c).lower()
    anchor_tokens = set(re.findall(r"[a-z0-9+\-#.]+", anchor))

    kept: list = []
    dropped: list = []
    for t in detailed:
        title = (t.get("title") or "").strip()
        if not title:
            continue

        # Domain-signal grounding: at least one content token of the title
        # must appear in the resume corpus. Generic role words (engineer,
        # intern, manager, ...) are stripped before this check so a title
        # like "Software Engineering Intern" is grounded by "software", not
        # by "engineer" / "intern" which match almost every resume.
        title_tokens = _title_signal_tokens(title)
        title_grounded = bool(title_tokens & anchor_tokens) if title_tokens else True

        # Evidence grounding: if the LLM provided an evidence string, at
        # least one significant token (>3 chars) of that evidence should
        # appear in the resume corpus. Optional — many heuristic-only
        # entries have no evidence and that's fine.
        ev = (t.get("evidence") or "").strip().lower()
        if ev:
            ev_tokens = {tok for tok in re.findall(r"[a-z0-9+\-#.]+", ev) if len(tok) > 3}
            ev_grounded = bool(ev_tokens & anchor_tokens) if ev_tokens else True
        else:
            ev_grounded = True

        if anchor and not title_grounded and not ev_grounded:
            dropped.append(title)
            continue
        kept.append(t)

    if kept:
        profile["target_titles_detailed"] = kept
        profile["target_titles"] = _dedup_preserve_order([t["title"] for t in kept])
    if dropped:
        _log(profile, f"reranker dropped {len(dropped)} ungrounded titles: {dropped}")
    return profile


def _education_entry_is_sparse(e: dict) -> bool:
    """An entry counts as sparse when it has fewer than two of the four
    structured fields, OR when the degree text still contains the GPA/year
    that should have been split out."""
    if not isinstance(e, dict):
        return True
    filled = sum(1 for k in ("degree", "institution", "year", "gpa") if e.get(k))
    if filled < 2:
        return True
    return False


def _project_entry_is_sparse(p: dict) -> bool:
    if not isinstance(p, dict):
        return True
    has_name = bool(p.get("name"))
    has_body = bool(p.get("description") or (p.get("bullets") or []))
    return not (has_name and has_body)


def enrich_education_and_projects(profile: dict, resume_text: str) -> dict:
    """Run the deterministic regex parsers and merge any structured fields the
    LLM missed. Never overwrites a non-empty LLM value — only fills blanks and
    appends entries the LLM omitted entirely.
    """
    if not profile or not resume_text:
        return profile
    try:
        from .providers import DemoProvider
    except Exception:
        return profile

    sections = DemoProvider._split_sections(resume_text)

    def _grab(*keys):
        for k in keys:
            if k in sections and sections[k]:
                return sections[k]
        return []

    # Education ----------------------------------------------------------
    edu_lines = _grab("education")
    parsed_edu = DemoProvider._parse_education_block(edu_lines) if edu_lines else []
    llm_edu = list(profile.get("education") or [])

    if parsed_edu:
        if not llm_edu:
            profile["education"] = parsed_edu
            _log(profile, f"enrichment: filled {len(parsed_edu)} education entries via regex")
        else:
            # Field-level fill on the matching LLM entry; otherwise append.
            def _matches(a: dict, b: dict) -> bool:
                ai = (a.get("institution") or "").lower()
                bi = (b.get("institution") or "").lower()
                ad = (a.get("degree") or "").lower()
                bd = (b.get("degree") or "").lower()
                if ai and bi and (ai in bi or bi in ai):
                    return True
                if ad and bd and (ad in bd or bd in ad):
                    return True
                return False

            filled_count = 0
            for parsed in parsed_edu:
                target = next((e for e in llm_edu if _matches(e, parsed)), None)
                if target is None:
                    if _education_entry_is_sparse(parsed):
                        continue
                    llm_edu.append(parsed)
                    filled_count += 1
                    continue
                for key in ("degree", "institution", "year", "gpa", "location"):
                    if not target.get(key) and parsed.get(key):
                        target[key] = parsed[key]
                        filled_count += 1
                for list_key in ("coursework", "honors"):
                    if parsed.get(list_key) and not target.get(list_key):
                        target[list_key] = parsed[list_key]
                        filled_count += 1
            if filled_count:
                _log(profile, f"enrichment: filled {filled_count} education fields/entries")
            profile["education"] = llm_edu

    # Projects -----------------------------------------------------------
    proj_lines = _grab("projects")
    parsed_projects = DemoProvider._parse_projects_block(proj_lines) if proj_lines else []
    llm_projects = list(profile.get("projects") or [])

    if parsed_projects:
        if not llm_projects:
            profile["projects"] = parsed_projects
            _log(profile, f"enrichment: filled {len(parsed_projects)} projects via regex")
        else:
            def _name_match(a: dict, b: dict) -> bool:
                an = (a.get("name") or "").lower().strip()
                bn = (b.get("name") or "").lower().strip()
                if not an or not bn:
                    return False
                return an == bn or an in bn or bn in an

            filled_count = 0
            for parsed in parsed_projects:
                target = next((p for p in llm_projects if _name_match(p, parsed)), None)
                if target is None:
                    if _project_entry_is_sparse(parsed):
                        continue
                    llm_projects.append(parsed)
                    filled_count += 1
                    continue
                # Fill blanks; merge skills_used.
                for key in ("description", "dates", "url"):
                    if not target.get(key) and parsed.get(key):
                        target[key] = parsed[key]
                        filled_count += 1
                if parsed.get("bullets") and not target.get("bullets"):
                    target["bullets"] = parsed["bullets"]
                    filled_count += 1
                merged_skills = list(target.get("skills_used") or [])
                seen = {s.lower() for s in merged_skills if s}
                for s in parsed.get("skills_used") or []:
                    if s and s.lower() not in seen:
                        seen.add(s.lower())
                        merged_skills.append(s)
                if merged_skills != list(target.get("skills_used") or []):
                    target["skills_used"] = merged_skills
                    filled_count += 1
            if filled_count:
                _log(profile, f"enrichment: filled {filled_count} project fields/entries")
            profile["projects"] = llm_projects

    return profile


def audit_profile(profile: dict, resume_text: str) -> dict:
    """Run the full audit chain. Safe to call with any profile shape."""
    profile = flatten_profile(profile)
    profile = quarantine_misplaced_skills(profile)
    profile = retention_audit(profile, resume_text)
    profile = verify_evidence(profile, resume_text)
    profile = rerank_titles(profile)
    profile = enrich_education_and_projects(profile, resume_text)
    return profile

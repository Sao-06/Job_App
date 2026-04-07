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

# ── Domain whitelist (used by the LLM prompt AND the reranker) ─────────────

DOMAIN_TITLE_FAMILIES = [
    "IC Design / VLSI",
    "Analog / Mixed-Signal Design",
    "Semiconductor Process / Device Engineering",
    "Photonics / Optoelectronics Engineering",
    "Nanofabrication / MEMS",
    "FPGA / Digital Design",
    "RF / Hardware Engineering",
    "Embedded Systems / Firmware",
    "Research Assistant (semiconductor / photonics / device)",
]

# Titles that should NOT be suggested for a hardware/semiconductor/photonics
# candidate unless their primary Education is Computer Science AND there is
# zero lab/fab/device research experience.
FORBIDDEN_GENERIC_TITLES = {
    "software engineer", "software developer",
    "data scientist", "data analyst", "data engineer",
    "web developer", "full stack developer", "frontend developer",
    "backend developer", "ml engineer", "machine learning engineer",
    "devops engineer", "product manager",
}


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
    hard skills and merge them in (case-preserving best effort)."""
    text_l = (resume_text or "").lower()
    extracted_l = {s.lower() for s in profile.get("top_hard_skills", [])}

    missed: list = []
    for token in HARD_SKILL_LEXICON:
        token_s = token.strip()
        if not token_s or len(token_s) < 2:
            continue
        if token_s not in text_l:
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
        # Fallback: if the skill name itself appears in the resume, keep it
        # even if the LLM's evidence quote was imperfect.
        if skill_l and len(skill_l) >= 2 and skill_l in text_l:
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


def rerank_titles(profile: dict) -> dict:
    """Keep only target titles whose evidence line traces back to
    Education or Research Experience; drop forbidden generic titles
    unless the candidate is CS-only with no research."""
    detailed = profile.get("target_titles_detailed") or []
    if not detailed:
        return profile

    # Build an "anchor" corpus from Education + Research/Experience + Projects.
    chunks: list = []
    for e in profile.get("education") or []:
        chunks.append(f"{e.get('degree', '')} {e.get('institution', '')}")
    for r in (profile.get("research_experience") or profile.get("experience") or []):
        chunks.append(r.get("title", ""))
        chunks.append(r.get("company", ""))
        chunks.extend(r.get("bullets") or [])
    for pr in profile.get("projects") or []:
        chunks.append(pr.get("name", ""))
        chunks.append(pr.get("description", ""))
        chunks.extend(pr.get("skills_used") or [])
    anchor = " ".join(c for c in chunks if c).lower()

    # Does the candidate have ANY lab/hardware footprint?
    has_hw_footprint = _is_hard_token(anchor)

    kept: list = []
    dropped: list = []
    for t in detailed:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        title_l = title.lower()

        # Rule 1: forbidden generics get dropped unless the candidate truly
        # has no hardware/lab footprint at all.
        if has_hw_footprint and any(fg in title_l for fg in FORBIDDEN_GENERIC_TITLES):
            dropped.append(title)
            continue

        # Rule 2: require evidence grounding when evidence is provided.
        ev = (t.get("evidence") or "").strip().lower()
        if ev:
            tokens = [tok for tok in ev.split() if len(tok) > 3]
            if tokens and not any(tok in anchor for tok in tokens):
                dropped.append(title)
                continue
        kept.append(t)

    if kept:
        profile["target_titles_detailed"] = kept
        profile["target_titles"] = _dedup_preserve_order([t["title"] for t in kept])
    if dropped:
        _log(profile, f"reranker dropped {len(dropped)} titles: {dropped}")
    return profile


def audit_profile(profile: dict, resume_text: str) -> dict:
    """Run the full audit chain. Safe to call with any profile shape."""
    profile = flatten_profile(profile)
    profile = quarantine_misplaced_skills(profile)
    profile = retention_audit(profile, resume_text)
    profile = verify_evidence(profile, resume_text)
    profile = rerank_titles(profile)
    return profile

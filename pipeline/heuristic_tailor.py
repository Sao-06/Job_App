"""
pipeline/heuristic_tailor.py
────────────────────────────
Deterministic resume tailoring.  No LLM call.

Two responsibilities:

1. ``validate_tailoring(tailored, profile, job)`` — coerces / rejects raw
   provider output.  Low-level local LLMs frequently return JSON that's the
   wrong *shape* (string instead of list, ``null`` role, dict where a string
   was expected).  The empty-check at ``phase4_tailor_resume`` only catches
   "both fields empty"; this validator rejects malformed shapes too so we
   can fall back to the heuristic path before bad data reaches the renderer.

2. ``heuristic_tailor_resume(job, profile, resume_text)`` — produces a
   *complete* tailoring dict (skills_reordered + experience_bullets +
   ats_keywords_missing + section_order) using only string heuristics.
   This is the safety net the user's "more heuristic to prevent glitches"
   ask is asking for: when the LLM mangles the response, the renderer still
   gets a fully-populated tailoring instead of skills-only.

Anti-fabrication invariants (DO NOT VIOLATE):

  • ``skills_reordered`` only ever *reorders* the user's existing skills.
    Missing JD requirements are surfaced in ``ats_keywords_missing`` so the
    UI can render them as "consider adding" — they are never silently
    appended to the user's skill list.

  • ``experience_bullets`` only reorders the *existing* bullets within each
    role; it never invents new bullets, never rewrites them.  The previous
    fallback at ``phase4_tailor_resume`` claimed JD skills as if the user
    had them; this module fixes that.
"""

from __future__ import annotations

import re


# ── Validation ────────────────────────────────────────────────────────────────

def _is_str(x) -> bool:
    return isinstance(x, str) and bool(x.strip())


def _coerce_skill(s) -> str | None:
    """A skill entry can come back as a plain string or a ``{skill: "..."}``
    object from some providers.  Reduce to a clean string or drop it."""
    if isinstance(s, str):
        return s.strip() or None
    if isinstance(s, dict):
        for k in ("skill", "name", "label", "value"):
            v = s.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _coerce_bullet_block(entry) -> dict | None:
    """Validate a single ``{role, bullets[]}`` entry from the provider."""
    if not isinstance(entry, dict):
        return None
    role_raw = entry.get("role") or entry.get("title") or ""
    role = role_raw.strip() if isinstance(role_raw, str) else ""
    bullets_raw = entry.get("bullets") or entry.get("items") or []
    if isinstance(bullets_raw, str):
        # Some local models return a single string with embedded newlines.
        bullets_raw = [b for b in re.split(r"[\n\r]+", bullets_raw) if b.strip()]
    if not isinstance(bullets_raw, list):
        return None
    bullets = [str(b).strip() for b in bullets_raw if isinstance(b, (str, int, float)) and str(b).strip()]
    if not role and not bullets:
        return None
    return {"role": role, "bullets": bullets}


def validate_tailoring(tailored: dict | None) -> dict | None:
    """Return a structurally-clean tailoring dict, or ``None`` if the input
    is too damaged to use.

    The caller treats ``None`` as a signal to switch to the heuristic
    path.  When this returns a dict, every field is in the shape the
    renderer expects:

      skills_reordered      : list[str]
      experience_bullets    : list[{role: str, bullets: list[str]}]
      ats_keywords_missing  : list[str]
      section_order         : list[str]   (or absent)
      cover_letter          : str         (passes through unchanged)
    """
    if not isinstance(tailored, dict):
        return None

    skills_raw = tailored.get("skills_reordered") or []
    if not isinstance(skills_raw, list):
        return None
    skills = [s for s in (_coerce_skill(x) for x in skills_raw) if s]

    bullets_raw = tailored.get("experience_bullets") or []
    if not isinstance(bullets_raw, list):
        return None
    bullets = [b for b in (_coerce_bullet_block(x) for x in bullets_raw) if b]

    missing_raw = tailored.get("ats_keywords_missing") or []
    if not isinstance(missing_raw, list):
        missing_raw = []
    missing = [str(x).strip() for x in missing_raw if _is_str(str(x))]

    section_raw = tailored.get("section_order") or []
    if not isinstance(section_raw, list):
        section_raw = []
    section = [str(x).strip() for x in section_raw if _is_str(str(x))]

    cover = tailored.get("cover_letter")
    if cover is not None and not isinstance(cover, str):
        cover = None

    # Reject if neither skills nor bullets came back — no useful tailoring.
    if not skills and not bullets:
        return None

    out: dict = {
        "skills_reordered": skills,
        "experience_bullets": bullets,
        "ats_keywords_missing": missing,
    }
    if section:
        out["section_order"] = section
    if cover:
        out["cover_letter"] = cover
    return out


# ── Heuristic tailoring ───────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+\-#./]*", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    """Lowercase token set, without stop-word filtering — JD requirements
    are usually short multi-word phrases like "FPGA design" or "ETL
    pipelines", so we want both individual words AND the whole phrase to
    count toward overlap."""
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def _phrase_overlap(needle: str, hay_tokens: set[str]) -> int:
    """How many tokens from *needle* appear in *hay_tokens*."""
    return sum(1 for t in _tokens(needle) if t in hay_tokens)


def _reorder_existing_skills(profile_skills: list[str], jd_requirements: list[str]) -> list[str]:
    """Surface user-skills that match JD requirements first; preserve the
    user's other skills behind them.  Never adds new skills."""
    skills = [s for s in profile_skills if isinstance(s, str) and s.strip()]
    if not skills:
        return []

    jd_tokens = set()
    for r in jd_requirements:
        jd_tokens |= _tokens(r)

    if not jd_tokens:
        return list(skills)

    def _score(skill: str) -> tuple[int, int]:
        # Higher score = closer to top.  Tie-break: original order (so we
        # don't reshuffle equally-irrelevant skills.)
        return (_phrase_overlap(skill, jd_tokens), -skills.index(skill))

    matched   = [s for s in skills if _phrase_overlap(s, jd_tokens) > 0]
    unmatched = [s for s in skills if _phrase_overlap(s, jd_tokens) == 0]
    matched.sort(key=_score, reverse=True)
    return matched + unmatched


def _reorder_bullets_in_role(bullets: list[str], jd_tokens: set[str]) -> list[str]:
    """Reorder bullets within one role: strongest JD-keyword overlap first.
    Stable: bullets with equal overlap keep their original order."""
    if not bullets or not jd_tokens:
        return list(bullets)
    indexed = list(enumerate(bullets))
    indexed.sort(key=lambda iv: (-_phrase_overlap(iv[1], jd_tokens), iv[0]))
    return [b for _, b in indexed]


def _missing_jd_keywords(jd_requirements: list[str], profile_skills: list[str],
                          resume_text: str) -> list[str]:
    """JD requirements that aren't represented in the user's profile or
    resume text — surfaced as 'consider adding' (NOT silently merged into
    skills_reordered)."""
    haystack = " ".join([
        " ".join(profile_skills or []),
        resume_text or "",
    ]).lower()
    out: list[str] = []
    seen: set[str] = set()
    for r in jd_requirements:
        if not isinstance(r, str):
            continue
        token = r.strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        if key not in haystack:
            out.append(token)
    return out


def heuristic_tailor_resume(job: dict, profile: dict, resume_text: str = "") -> dict:
    """Deterministic resume tailoring.  No LLM.

    Always returns a complete, structurally-valid tailoring dict.  Used
    when the configured provider can't produce well-formed output (low-end
    local LLM glitches, demo mode, retries exhausted)."""
    job = job or {}
    profile = profile or {}

    requirements: list[str] = [
        str(r).strip() for r in (job.get("requirements") or []) if str(r).strip()
    ]
    profile_skills: list[str] = [
        str(s).strip()
        for s in (profile.get("top_hard_skills") or [])
        if str(s).strip()
    ]

    skills_reordered = _reorder_existing_skills(profile_skills, requirements)

    jd_tokens: set[str] = set()
    for r in requirements:
        jd_tokens |= _tokens(r)

    experience_bullets: list[dict] = []
    for role in (profile.get("experience") or []):
        if not isinstance(role, dict):
            continue
        bullets = [str(b).strip() for b in (role.get("bullets") or []) if str(b).strip()]
        if not bullets:
            continue
        ordered = _reorder_bullets_in_role(bullets, jd_tokens)
        # Skip if reordering produced no observable change AND there's no
        # JD overlap signal — saves the renderer from rendering identical
        # role blocks for jobs unrelated to the user's history.
        if ordered != bullets or any(_phrase_overlap(b, jd_tokens) for b in ordered):
            experience_bullets.append({
                "role": role.get("title") or role.get("role") or "",
                "bullets": ordered,
            })

    return {
        "skills_reordered":     skills_reordered,
        "experience_bullets":   experience_bullets,
        "ats_keywords_missing": _missing_jd_keywords(requirements, profile_skills, resume_text),
        "section_order":        ["Skills", "Experience", "Projects", "Education"],
    }


# ── Hybrid merge ──────────────────────────────────────────────────────────────

def merge_with_heuristic(llm_tailored: dict | None, heuristic: dict) -> dict:
    """Take whatever the LLM produced (post-validation) and fill any
    missing field from the heuristic.  Used when the LLM gave us *some*
    usable signal (e.g. great skills_reordered, empty experience_bullets):
    we keep the LLM's good work and let the heuristic fill the gaps."""
    out: dict = dict(heuristic)
    if not isinstance(llm_tailored, dict):
        return out

    if llm_tailored.get("skills_reordered"):
        out["skills_reordered"] = llm_tailored["skills_reordered"]
    if llm_tailored.get("experience_bullets"):
        out["experience_bullets"] = llm_tailored["experience_bullets"]
    if llm_tailored.get("ats_keywords_missing"):
        out["ats_keywords_missing"] = llm_tailored["ats_keywords_missing"]
    if llm_tailored.get("section_order"):
        out["section_order"] = llm_tailored["section_order"]
    if llm_tailored.get("cover_letter"):
        out["cover_letter"] = llm_tailored["cover_letter"]
    return out


# ── v2 (TailoredResume schema) ───────────────────────────────────────────────

from .tailored_schema import (  # noqa: E402
    SCHEMA_VERSION, TailoredResume, default_v2, validate_v2,
)


def heuristic_tailor_resume_v2(
    job: dict, profile: dict, resume_text: str = "",
    selected_keywords: list[str] | None = None,
) -> TailoredResume:
    """Deterministic v2 tailoring. No LLM. No fabrication.

    Reorders skills + bullets by JD overlap. Honors `selected_keywords` by
    appending them to the appropriate skill category with diff='added'.
    Surfaces unselected JD keywords in ats_keywords_missing.
    """
    job = job or {}
    profile = profile or {}
    selected_keywords = [k.strip() for k in (selected_keywords or []) if isinstance(k, str) and k.strip()]

    base = default_v2(profile)

    requirements = [str(r).strip() for r in (job.get("requirements") or []) if str(r).strip()]
    profile_skills = [
        s for s in (profile.get("top_hard_skills") or [])
        if isinstance(s, str) and s.strip()
    ]

    # Reorder existing skills
    reordered = _reorder_existing_skills(profile_skills, requirements)
    skill_items: list[dict] = [{"text": s, "diff": "unchanged"} for s in reordered]
    # Append user-selected keywords as added
    existing_lower = {s.lower() for s in reordered}
    for kw in selected_keywords:
        if kw.lower() not in existing_lower:
            skill_items.append({"text": kw, "diff": "added"})
            existing_lower.add(kw.lower())
    base["skills"] = [{"name": "", "items": skill_items}] if skill_items else []

    # Reorder bullets within each role (no fabrication, all diff="unchanged")
    jd_tokens: set[str] = set()
    for r in requirements:
        jd_tokens |= _tokens(r)

    for role in base["experience"]:
        original_texts = [b.get("text", "") for b in role.get("bullets") or []]
        if not original_texts:
            continue
        ordered = _reorder_bullets_in_role(original_texts, jd_tokens)
        role["bullets"] = [{"text": b, "diff": "unchanged"} for b in ordered]

    # ATS gap surfacing — anything from JD that's missing AND not user-selected
    selected_lower = {s.lower() for s in selected_keywords}
    base["ats_keywords_missing"] = [
        kw for kw in _missing_jd_keywords(requirements, profile_skills, resume_text)
        if kw.lower() not in selected_lower
    ]
    base["ats_keywords_added"] = list(selected_keywords)
    return base


def validate_v2_or_none(raw):
    """Thin wrapper exposing the schema validator under the heuristic_tailor
    namespace so callers in phases.py have one import."""
    return validate_v2(raw)


def merge_with_heuristic_v2(
    llm: TailoredResume | None, heuristic: TailoredResume,
) -> TailoredResume:
    """Hybrid merge: take any non-empty LLM field, fall back to heuristic."""
    out: TailoredResume = dict(heuristic)
    if not isinstance(llm, dict):
        return out
    for key in (
        "summary", "skills", "experience", "projects", "education",
        "awards", "certifications", "publications", "activities",
        "leadership", "volunteer", "coursework", "languages",
        "custom_sections", "section_order",
        "ats_keywords_added", "ats_keywords_missing",
    ):
        if llm.get(key):
            out[key] = llm[key]
    for key in ("ats_score_before", "ats_score_after"):
        v = llm.get(key)
        if isinstance(v, int):
            out[key] = v
    # Pass through cover_letter when present
    if llm.get("cover_letter"):
        out["cover_letter"] = llm["cover_letter"]
    return out

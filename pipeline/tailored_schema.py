"""
pipeline/tailored_schema.py
───────────────────────────
TailoredResume v2 schema. Single source of truth consumed by every
renderer (HTML/CSS template lib, in-place LaTeX, in-place DOCX).

Diff markers (per TextNode):
  unchanged | modified | added
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

SCHEMA_VERSION = 2

DiffMarker = Literal["unchanged", "modified", "added"]
_VALID_DIFF: set[str] = {"unchanged", "modified", "added"}


class TextNode(TypedDict, total=False):
    text: str
    original: str
    diff: DiffMarker


class SkillCategory(TypedDict, total=False):
    name: str
    items: list[TextNode]


class Role(TypedDict, total=False):
    title: str
    company: str
    location: str
    dates: str
    bullets: list[TextNode]


class EducationEntry(TypedDict, total=False):
    institution: str
    degree: str
    dates: str
    location: str
    gpa: str
    notes: list[TextNode]


class ProjectEntry(TypedDict, total=False):
    name: str
    description: TextNode
    skills_used: list[TextNode]
    bullets: list[TextNode]
    dates: str
    url: str


class GenericEntry(TypedDict, total=False):
    title: TextNode
    detail: TextNode
    bullets: list[TextNode]


class CustomSection(TypedDict, total=False):
    name: str
    items: list[GenericEntry]


class TailoredResume(TypedDict, total=False):
    schema_version: int
    name: str
    email: str
    phone: str
    linkedin: str
    github: str
    location: str
    website: str
    summary: TextNode
    skills: list[SkillCategory]
    experience: list[Role]
    projects: list[ProjectEntry]
    education: list[EducationEntry]
    awards: list[GenericEntry]
    certifications: list[GenericEntry]
    publications: list[GenericEntry]
    activities: list[GenericEntry]
    leadership: list[GenericEntry]
    volunteer: list[GenericEntry]
    coursework: list[GenericEntry]
    languages: list[GenericEntry]
    custom_sections: list[CustomSection]
    section_order: list[str]
    ats_keywords_added: list[str]
    ats_keywords_missing: list[str]
    ats_score_before: int
    ats_score_after: int


_GENERIC_BUCKETS = (
    "awards", "certifications", "publications", "activities",
    "leadership", "volunteer", "coursework", "languages",
)
_DEFAULT_SECTION_ORDER = ["Skills", "Experience", "Projects", "Education"]


# ── Coercion helpers ─────────────────────────────────────────────────────────

def _coerce_text_node(x: Any) -> TextNode | None:
    """Accept either a plain string or a {text, diff, original} dict."""
    if isinstance(x, str):
        s = x.strip()
        return {"text": s, "diff": "unchanged"} if s else None
    if not isinstance(x, dict):
        return None
    text = x.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    out: TextNode = {"text": text.strip()}
    diff = x.get("diff")
    out["diff"] = diff if diff in _VALID_DIFF else "unchanged"
    if x.get("original") and isinstance(x["original"], str):
        out["original"] = x["original"]
    return out


def _coerce_text_list(items: Any) -> list[TextNode]:
    if not isinstance(items, list):
        return []
    out: list[TextNode] = []
    for it in items:
        node = _coerce_text_node(it)
        if node:
            out.append(node)
    return out


def _coerce_skill_category(c: Any) -> SkillCategory | None:
    if isinstance(c, list):
        items = _coerce_text_list(c)
        return {"name": "", "items": items} if items else None
    if not isinstance(c, dict):
        return None
    items = _coerce_text_list(c.get("items"))
    if not items:
        return None
    return {"name": str(c.get("name") or ""), "items": items}


def _coerce_role(r: Any) -> Role | None:
    if not isinstance(r, dict):
        return None
    out: Role = {
        "title":    str(r.get("title") or "").strip(),
        "company":  str(r.get("company") or "").strip(),
        "dates":    str(r.get("dates") or "").strip(),
        "location": str(r.get("location") or "").strip(),
        "bullets":  _coerce_text_list(r.get("bullets")),
    }
    if not (out["title"] or out["company"] or out["bullets"]):
        return None
    return out


def _coerce_generic_entry(e: Any) -> GenericEntry | None:
    if not isinstance(e, dict):
        return None
    title = _coerce_text_node(e.get("title")) or {"text": "", "diff": "unchanged"}
    detail = _coerce_text_node(e.get("detail")) or {"text": "", "diff": "unchanged"}
    bullets = _coerce_text_list(e.get("bullets"))
    if not (title.get("text") or detail.get("text") or bullets):
        return None
    return {"title": title, "detail": detail, "bullets": bullets}


def _coerce_education(e: Any) -> EducationEntry | None:
    if not isinstance(e, dict):
        return None
    out: EducationEntry = {
        "institution": str(e.get("institution") or "").strip(),
        "degree":      str(e.get("degree") or "").strip(),
        "dates":       str(e.get("dates") or e.get("year") or "").strip(),
        "location":    str(e.get("location") or "").strip(),
        "gpa":         str(e.get("gpa") or "").strip(),
        "notes":       _coerce_text_list(e.get("notes")),
    }
    if not (out["institution"] or out["degree"]):
        return None
    return out


def _coerce_project(p: Any) -> ProjectEntry | None:
    if not isinstance(p, dict):
        return None
    name = str(p.get("name") or "").strip()
    desc = _coerce_text_node(p.get("description"))
    bullets = _coerce_text_list(p.get("bullets"))
    skills = _coerce_text_list(p.get("skills_used"))
    if not (name or desc or bullets):
        return None
    out: ProjectEntry = {"name": name, "skills_used": skills, "bullets": bullets}
    if desc:
        out["description"] = desc
    if p.get("dates"):
        out["dates"] = str(p["dates"])
    if p.get("url"):
        out["url"] = str(p["url"])
    return out


def _coerce_custom_section(c: Any) -> CustomSection | None:
    if not isinstance(c, dict):
        return None
    name = str(c.get("name") or "").strip()
    if not name:
        return None
    items = [g for g in (_coerce_generic_entry(x) for x in (c.get("items") or [])) if g]
    if not items:
        return None
    return {"name": name, "items": items}


# ── Validator ────────────────────────────────────────────────────────────────

def validate_v2(d: Any) -> TailoredResume | None:
    """Strict validator. Returns None when the input is too malformed to render."""
    if not isinstance(d, dict):
        return None
    name_raw = d.get("name")
    name = name_raw.strip() if isinstance(name_raw, str) else ""
    if not name:
        return None

    out: TailoredResume = {"schema_version": SCHEMA_VERSION, "name": name}
    for key in ("email", "phone", "linkedin", "github", "location", "website"):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()

    summary = _coerce_text_node(d.get("summary"))
    if summary and summary.get("text"):
        out["summary"] = summary

    skills_raw = d.get("skills")
    if isinstance(skills_raw, list):
        skills = [c for c in (_coerce_skill_category(s) for s in skills_raw) if c]
    elif isinstance(skills_raw, dict):
        single = _coerce_skill_category(skills_raw)
        skills = [single] if single else []
    else:
        skills = []
    out["skills"] = skills

    out["experience"] = [r for r in (_coerce_role(x) for x in (d.get("experience") or [])) if r]
    out["projects"] = [p for p in (_coerce_project(x) for x in (d.get("projects") or [])) if p]
    out["education"] = [e for e in (_coerce_education(x) for x in (d.get("education") or [])) if e]

    for bucket in _GENERIC_BUCKETS:
        vals = [g for g in (_coerce_generic_entry(x) for x in (d.get(bucket) or [])) if g]
        if vals:
            out[bucket] = vals

    out["custom_sections"] = [
        c for c in (_coerce_custom_section(x) for x in (d.get("custom_sections") or [])) if c
    ]

    section_order = d.get("section_order")
    if isinstance(section_order, list):
        out["section_order"] = [str(s) for s in section_order if str(s).strip()]
    else:
        out["section_order"] = []

    out["ats_keywords_added"] = [
        str(s) for s in (d.get("ats_keywords_added") or []) if str(s).strip()
    ]
    out["ats_keywords_missing"] = [
        str(s) for s in (d.get("ats_keywords_missing") or []) if str(s).strip()
    ]
    try:
        out["ats_score_before"] = int(d.get("ats_score_before") or 0)
        out["ats_score_after"] = int(d.get("ats_score_after") or 0)
    except (TypeError, ValueError):
        out["ats_score_before"] = 0
        out["ats_score_after"] = 0

    if not (out["skills"] or out["experience"] or out["projects"] or out["education"]):
        return None
    return out


# ── Adapters ────────────────────────────────────────────────────────────────

def default_v2(profile: dict | None) -> TailoredResume:
    """Build a v2 skeleton from a Phase-1 profile. Every TextNode is
    diff='unchanged'. Used by the heuristic safety net and as a baseline
    for the LLM."""
    profile = profile or {}
    out: TailoredResume = {
        "schema_version": SCHEMA_VERSION,
        "name": (str(profile.get("name") or "").strip()) or "—",
    }
    for key in ("email", "phone", "linkedin", "github", "location", "website"):
        v = profile.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()

    skills = [
        s for s in (profile.get("top_hard_skills") or [])
        if isinstance(s, str) and s.strip()
    ]
    out["skills"] = (
        [{"name": "", "items": [{"text": s, "diff": "unchanged"} for s in skills]}]
        if skills else []
    )

    out["experience"] = [
        {
            "title":    str(r.get("title") or ""),
            "company":  str(r.get("company") or ""),
            "dates":    str(r.get("dates") or ""),
            "location": str(r.get("location") or ""),
            "bullets": [
                {"text": b, "diff": "unchanged"}
                for b in (r.get("bullets") or [])
                if isinstance(b, str) and b.strip()
            ],
        }
        for r in (profile.get("experience") or [])
        if isinstance(r, dict)
    ]
    out["projects"] = []
    for p in (profile.get("projects") or []):
        if not isinstance(p, dict):
            continue
        desc = p.get("description")
        proj: ProjectEntry = {
            "name": str(p.get("name") or ""),
            "skills_used": [
                {"text": s, "diff": "unchanged"}
                for s in (p.get("skills_used") or [])
                if isinstance(s, str) and s.strip()
            ],
            "bullets": [
                {"text": b, "diff": "unchanged"}
                for b in (p.get("bullets") or [])
                if isinstance(b, str) and b.strip()
            ],
        }
        if isinstance(desc, str) and desc.strip():
            proj["description"] = {"text": desc.strip(), "diff": "unchanged"}
        out["projects"].append(proj)

    out["education"] = [
        {
            "institution": str(e.get("institution") or ""),
            "degree":      str(e.get("degree") or ""),
            "dates":       str(e.get("year") or e.get("dates") or ""),
            "gpa":         str(e.get("gpa") or ""),
            "notes":       [],
        }
        for e in (profile.get("education") or [])
        if isinstance(e, dict)
    ]
    out["custom_sections"] = []
    out["section_order"] = list(_DEFAULT_SECTION_ORDER)
    out["ats_keywords_added"] = []
    out["ats_keywords_missing"] = []
    out["ats_score_before"] = 0
    out["ats_score_after"] = 0
    return out


def legacy_to_v2(legacy: dict | None, profile: dict | None) -> TailoredResume:
    """Adapt the old {skills_reordered, experience_bullets, ats_*} dict to v2."""
    base = default_v2(profile)
    legacy = legacy or {}

    sk = [s for s in (legacy.get("skills_reordered") or []) if isinstance(s, str) and s.strip()]
    if sk:
        base["skills"] = [{
            "name": "",
            "items": [{"text": s, "diff": "unchanged"} for s in sk],
        }]

    bullets_by_role: dict[str, list[str]] = {}
    for entry in (legacy.get("experience_bullets") or []):
        if not isinstance(entry, dict):
            continue
        role = (entry.get("role") or "").strip().lower()
        bs = [b for b in (entry.get("bullets") or []) if isinstance(b, str) and b.strip()]
        if role and bs:
            bullets_by_role[role] = bs

    for role in base["experience"]:
        title_l = (role.get("title") or "").lower()
        for k, v in bullets_by_role.items():
            if k and (k in title_l or title_l in k):
                role["bullets"] = [{"text": b, "diff": "unchanged"} for b in v]
                break

    base["ats_keywords_missing"] = [
        str(s) for s in (legacy.get("ats_keywords_missing") or [])
        if isinstance(s, str) and s.strip()
    ]
    try:
        base["ats_score_before"] = int(legacy.get("ats_score_before") or 0)
        base["ats_score_after"] = int(legacy.get("ats_score_after") or 0)
    except (TypeError, ValueError):
        pass

    so = legacy.get("section_order")
    if isinstance(so, list) and so:
        base["section_order"] = [str(s) for s in so]
    return base

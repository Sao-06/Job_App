# Tailor Resume Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current Phase 4 tailoring with a comprehensive, source-format-aware system that preserves the user's original resume layout and adds a keyword-checkbox review step + green-highlight diff.

**Architecture:** Two layers of change — (1) a new TailoredResume v2 schema covering every section, consumed by all renderers; (2) source-format-aware dispatch — in-place edits for `.tex`/`.docx`, HTML/CSS template-library match for `.pdf`. Frontend gains a two-step UX (analyze → review keywords → generate) plus inline green-highlight preview.

**Tech Stack:** Python 3.11+, FastAPI, python-docx, Jinja2, WeasyPrint (with reportlab fallback), pdfplumber, React 18 (Babel-in-browser, no build step).

**Reference spec:** `docs/superpowers/specs/2026-05-07-tailor-resume-redesign-design.md`

---

## File Map

### To create (new modules)
```
pipeline/tailored_schema.py            # TypedDicts, validator, adapter, default factory
pipeline/latex_tailor.py               # In-place LaTeX rewriter
pipeline/docx_tailor.py                # In-place DOCX rewriter
pipeline/template_match.py             # Score format_profile against template library
pipeline/template_render.py            # Jinja2 → WeasyPrint (or reportlab fallback)
pipeline/templates/_shared/base.css    # CSS variables, reset
pipeline/templates/_shared/diff.css    # Green-highlight rules
pipeline/templates/single_column_classic.html.j2
pipeline/templates/single_column_modern.html.j2
pipeline/templates/two_column_left.html.j2
pipeline/templates/two_column_right.html.j2
pipeline/templates/compact_tech.html.j2
pipeline/templates/academic_multipage.html.j2
tests/unit/pipeline/test_tailored_schema.py
tests/unit/pipeline/test_heuristic_tailor_v2.py
tests/unit/pipeline/test_template_match.py
tests/unit/pipeline/test_template_render.py
tests/unit/pipeline/test_latex_tailor.py
tests/unit/pipeline/test_docx_tailor.py
tests/integration/test_app_tailor_v2.py
tests/fixtures/resumes/jake_classic.tex
tests/fixtures/resumes/modern_sans.docx
tests/fixtures/resumes/academic_publications.txt   # text representation, used to seed
tests/fixtures/resumes/compact_tech.txt
```

### To modify
```
pipeline/providers.py        # Tool schemas + tailor_resume() signature for all 3 providers
pipeline/heuristic_tailor.py # heuristic_tailor_resume_v2 + validate_v2 + merge_v2
pipeline/phases.py           # phase4_tailor_resume accepts selected_keywords
pipeline/resume.py           # _save_tailored_resume dispatches by source_format
pipeline/__init__.py         # re-exports
app.py                       # /api/resume/tailor/analyze; /tailor extended; source_format on upload; _build_tailored_item v2-aware
frontend/app.jsx             # TailorDrawer two-step UX, TailoredResumeCard preview, upload-page hint
frontend/index.html          # CSS rules: mark.diff-add, mark.diff-mod, .tailor-keyword-row
requirements.txt             # weasyprint, jinja2, docx2pdf
```

---

## Task 1: Tailored schema (data model + adapter + validator)

**Files:**
- Create: `pipeline/tailored_schema.py`
- Test: `tests/unit/pipeline/test_tailored_schema.py`

- [ ] **Step 1: Write the failing tests** — `tests/unit/pipeline/test_tailored_schema.py`

```python
import pytest
from pipeline.tailored_schema import (
    TextNode, validate_v2, legacy_to_v2, default_v2,
    SCHEMA_VERSION,
)

pytestmark = pytest.mark.unit


def test_validate_v2_accepts_minimal():
    d = {
        "schema_version": SCHEMA_VERSION,
        "name": "Jane Doe",
        "skills": [{"name": "", "items": [{"text": "Python", "diff": "unchanged"}]}],
        "experience": [],
        "section_order": ["Skills"],
    }
    assert validate_v2(d) is not None


def test_validate_v2_rejects_missing_name():
    d = {"schema_version": SCHEMA_VERSION, "skills": [], "experience": []}
    assert validate_v2(d) is None


def test_validate_v2_rejects_unknown_diff_marker():
    d = {
        "schema_version": SCHEMA_VERSION,
        "name": "X",
        "skills": [{"name": "", "items": [{"text": "Foo", "diff": "garbage"}]}],
        "experience": [],
    }
    assert validate_v2(d) is None


def test_validate_v2_coerces_missing_diff_to_unchanged():
    d = {
        "schema_version": SCHEMA_VERSION,
        "name": "X",
        "skills": [{"name": "", "items": [{"text": "Foo"}]}],
        "experience": [],
    }
    out = validate_v2(d)
    assert out is not None
    assert out["skills"][0]["items"][0]["diff"] == "unchanged"


def test_legacy_to_v2_carries_skills_and_bullets():
    legacy = {
        "skills_reordered": ["Python", "FPGA"],
        "experience_bullets": [
            {"role": "Intern", "bullets": ["Built a thing", "Tested another"]},
        ],
        "ats_keywords_missing": ["AXI4"],
        "ats_score_before": 60,
        "ats_score_after": 78,
    }
    profile = {
        "name": "Jane",
        "experience": [{"title": "Intern", "company": "Acme", "dates": "2024", "bullets": []}],
        "education": [{"degree": "BS EE", "institution": "Cal", "year": "2025"}],
    }
    v2 = legacy_to_v2(legacy, profile)
    assert v2["schema_version"] == SCHEMA_VERSION
    assert v2["name"] == "Jane"
    assert len(v2["skills"]) >= 1
    assert v2["skills"][0]["items"][0]["text"] == "Python"
    assert v2["experience"][0]["bullets"][0]["text"] == "Built a thing"
    assert v2["ats_keywords_missing"] == ["AXI4"]
    assert v2["ats_score_before"] == 60
    assert v2["ats_score_after"] == 78


def test_default_v2_from_profile_has_every_section():
    profile = {
        "name": "Jane",
        "email": "j@example.com",
        "top_hard_skills": ["Python", "Verilog"],
        "experience": [{"title": "Intern", "company": "Acme", "dates": "2024",
                         "bullets": ["Did stuff"]}],
        "education": [{"degree": "BS", "institution": "Cal", "year": "2025"}],
        "projects": [{"name": "Foo", "description": "Did Foo"}],
    }
    v2 = default_v2(profile)
    assert v2["name"] == "Jane"
    assert v2["email"] == "j@example.com"
    assert v2["skills"][0]["items"][0]["text"] == "Python"
    assert v2["experience"][0]["bullets"][0]["diff"] == "unchanged"
    assert v2["section_order"] == ["Skills", "Experience", "Projects", "Education"]
```

- [ ] **Step 2: Run tests to verify they fail**

`pytest tests/unit/pipeline/test_tailored_schema.py -v` → expect ImportError on `pipeline.tailored_schema`.

- [ ] **Step 3: Create `pipeline/tailored_schema.py`**

```python
"""
pipeline/tailored_schema.py
───────────────────────────
TailoredResume v2 schema. Single source of truth consumed by every
renderer (HTML/CSS template lib, in-place LaTeX, in-place DOCX).

Diff markers (per TextNode):
  unchanged | modified | added
"""

from __future__ import annotations

from typing import Literal, TypedDict, Any

SCHEMA_VERSION = 2

DiffMarker = Literal["unchanged", "modified", "added"]
_VALID_DIFF = {"unchanged", "modified", "added"}


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


# ── Coercion helpers ─────────────────────────────────────────────────────────

def _coerce_text_node(x: Any) -> TextNode | None:
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
    if x.get("original"):
        out["original"] = str(x["original"])
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
        "title": str(r.get("title") or "").strip(),
        "company": str(r.get("company") or "").strip(),
        "dates": str(r.get("dates") or "").strip(),
        "location": str(r.get("location") or "").strip(),
        "bullets": _coerce_text_list(r.get("bullets")),
    }
    if not (out["title"] or out["company"] or out["bullets"]):
        return None
    return out


def _coerce_generic_entry(e: Any) -> GenericEntry | None:
    if not isinstance(e, dict):
        return None
    title = _coerce_text_node(e.get("title")) or {"text": ""}
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
        "degree": str(e.get("degree") or "").strip(),
        "dates": str(e.get("dates") or e.get("year") or "").strip(),
        "location": str(e.get("location") or "").strip(),
        "gpa": str(e.get("gpa") or "").strip(),
        "notes": _coerce_text_list(e.get("notes")),
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
    name = (d.get("name") or "").strip() if isinstance(d.get("name"), str) else ""
    if not name:
        return None

    out: TailoredResume = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
    }
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
        skills = [c for c in (_coerce_skill_category(skills_raw),) if c]
    else:
        skills = []
    out["skills"] = skills

    out["experience"] = [r for r in (_coerce_role(x) for x in (d.get("experience") or [])) if r]
    out["projects"] = [p for p in (_coerce_project(x) for x in (d.get("projects") or [])) if p]
    out["education"] = [e for e in (_coerce_education(x) for x in (d.get("education") or [])) if e]

    for bucket in ("awards", "certifications", "publications", "activities",
                   "leadership", "volunteer", "coursework", "languages"):
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

    out["ats_keywords_added"] = [str(s) for s in (d.get("ats_keywords_added") or []) if str(s).strip()]
    out["ats_keywords_missing"] = [str(s) for s in (d.get("ats_keywords_missing") or []) if str(s).strip()]
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

_DEFAULT_SECTION_ORDER = ["Skills", "Experience", "Projects", "Education"]


def default_v2(profile: dict | None) -> TailoredResume:
    """Build a v2 skeleton from a Phase-1 profile. Every TextNode is
    diff='unchanged'. Used by the heuristic safety net and as a baseline
    for the LLM."""
    profile = profile or {}
    out: TailoredResume = {
        "schema_version": SCHEMA_VERSION,
        "name": str(profile.get("name") or "").strip() or "—",
    }
    for key in ("email", "phone", "linkedin", "github", "location", "website"):
        v = profile.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()

    skills = [s for s in (profile.get("top_hard_skills") or []) if isinstance(s, str) and s.strip()]
    if skills:
        out["skills"] = [{
            "name": "",
            "items": [{"text": s, "diff": "unchanged"} for s in skills],
        }]
    else:
        out["skills"] = []

    out["experience"] = [
        {
            "title": str(r.get("title") or ""),
            "company": str(r.get("company") or ""),
            "dates": str(r.get("dates") or ""),
            "location": str(r.get("location") or ""),
            "bullets": [{"text": b, "diff": "unchanged"} for b in (r.get("bullets") or []) if isinstance(b, str) and b.strip()],
        }
        for r in (profile.get("experience") or [])
        if isinstance(r, dict)
    ]
    out["projects"] = [
        {
            "name": str(p.get("name") or ""),
            "description": ({"text": str(p["description"]), "diff": "unchanged"}
                             if isinstance(p.get("description"), str) and p["description"].strip()
                             else None) or {"text": "", "diff": "unchanged"},
            "skills_used": [{"text": s, "diff": "unchanged"} for s in (p.get("skills_used") or []) if isinstance(s, str)],
            "bullets": [{"text": b, "diff": "unchanged"} for b in (p.get("bullets") or []) if isinstance(b, str)],
        }
        for p in (profile.get("projects") or [])
        if isinstance(p, dict)
    ]
    out["education"] = [
        {
            "institution": str(e.get("institution") or ""),
            "degree": str(e.get("degree") or ""),
            "dates": str(e.get("year") or e.get("dates") or ""),
            "gpa": str(e.get("gpa") or ""),
            "notes": [],
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

    base["ats_keywords_missing"] = list(legacy.get("ats_keywords_missing") or [])
    try:
        base["ats_score_before"] = int(legacy.get("ats_score_before") or 0)
        base["ats_score_after"] = int(legacy.get("ats_score_after") or 0)
    except (TypeError, ValueError):
        pass

    so = legacy.get("section_order")
    if isinstance(so, list) and so:
        base["section_order"] = [str(s) for s in so]
    return base
```

- [ ] **Step 4: Run tests to verify they pass**

`pytest tests/unit/pipeline/test_tailored_schema.py -v` → all green.

- [ ] **Step 5: Commit**

```bash
git add pipeline/tailored_schema.py tests/unit/pipeline/test_tailored_schema.py
git commit -m "feat(pipeline): add TailoredResume v2 schema + legacy adapter"
```

---

## Task 2: Heuristic v2 path

**Files:**
- Modify: `pipeline/heuristic_tailor.py`
- Test: `tests/unit/pipeline/test_heuristic_tailor_v2.py`

- [ ] **Step 1: Write the failing tests** — `tests/unit/pipeline/test_heuristic_tailor_v2.py`

```python
import pytest
from pipeline.heuristic_tailor import (
    heuristic_tailor_resume_v2, validate_v2_or_none, merge_with_heuristic_v2,
)
from pipeline.tailored_schema import SCHEMA_VERSION

pytestmark = pytest.mark.unit


def _profile():
    return {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "top_hard_skills": ["Python", "Verilog", "FPGA"],
        "experience": [{"title": "Intern", "company": "Acme", "dates": "2024",
                        "bullets": ["Built a thing", "Tested another"]}],
        "education": [{"degree": "BS EE", "institution": "Cal", "year": "2025"}],
    }


def _job():
    return {
        "title": "Hardware Engineer",
        "company": "FooCo",
        "requirements": ["Verilog", "AXI4", "FPGA verification"],
    }


def test_heuristic_v2_returns_complete_schema():
    out = heuristic_tailor_resume_v2(_job(), _profile(), "Jane Doe resume...")
    assert out["schema_version"] == SCHEMA_VERSION
    assert out["name"] == "Jane Doe"
    assert out["skills"][0]["items"][0]["text"] in ("Verilog", "FPGA")
    assert out["experience"][0]["bullets"]
    assert "AXI4" in out["ats_keywords_missing"]


def test_heuristic_v2_marks_added_when_keyword_selected():
    out = heuristic_tailor_resume_v2(
        _job(), _profile(), "resume text",
        selected_keywords=["AXI4"],
    )
    flat = [it["text"] for cat in out["skills"] for it in cat["items"]]
    assert "AXI4" in flat
    added = [it for cat in out["skills"] for it in cat["items"] if it["diff"] == "added"]
    assert any(it["text"] == "AXI4" for it in added)


def test_heuristic_v2_does_not_fabricate_bullets():
    out = heuristic_tailor_resume_v2(_job(), _profile(), "resume text")
    for role in out["experience"]:
        for b in role["bullets"]:
            assert b["diff"] == "unchanged"


def test_validate_v2_or_none_rejects_garbage():
    assert validate_v2_or_none(None) is None
    assert validate_v2_or_none({}) is None
    assert validate_v2_or_none({"name": "X"}) is None  # no body sections


def test_merge_with_heuristic_v2_keeps_llm_bullets():
    heuristic = heuristic_tailor_resume_v2(_job(), _profile(), "")
    llm = dict(heuristic)
    llm["experience"] = [{
        "title": "Intern", "company": "Acme", "dates": "2024", "location": "",
        "bullets": [{"text": "Modified bullet", "diff": "modified", "original": "Built a thing"}],
    }]
    merged = merge_with_heuristic_v2(llm, heuristic)
    assert merged["experience"][0]["bullets"][0]["text"] == "Modified bullet"
    assert merged["experience"][0]["bullets"][0]["diff"] == "modified"
```

- [ ] **Step 2: Run tests to verify they fail**

`pytest tests/unit/pipeline/test_heuristic_tailor_v2.py -v` → ImportError.

- [ ] **Step 3: Append the new functions to `pipeline/heuristic_tailor.py`**

Append at end of file (keep all existing v1 code untouched):

```python
# ── v2 (TailoredResume schema) ───────────────────────────────────────────────

from .tailored_schema import (  # noqa: E402
    TailoredResume, SCHEMA_VERSION, default_v2, validate_v2,
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
    selected_keywords = [k.strip() for k in (selected_keywords or []) if k.strip()]

    base = default_v2(profile)

    requirements = [str(r).strip() for r in (job.get("requirements") or []) if str(r).strip()]
    profile_skills = [s for s in (profile.get("top_hard_skills") or []) if isinstance(s, str) and s.strip()]

    # Reorder existing skills
    reordered = _reorder_existing_skills(profile_skills, requirements)
    skill_items = [{"text": s, "diff": "unchanged"} for s in reordered]
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
        original_texts = [b["text"] for b in role["bullets"]]
        if not original_texts:
            continue
        ordered = _reorder_bullets_in_role(original_texts, jd_tokens)
        role["bullets"] = [{"text": b, "diff": "unchanged"} for b in ordered]

    # ATS gap surfacing — anything from JD that's missing AND not user-selected
    base["ats_keywords_missing"] = [
        kw for kw in _missing_jd_keywords(requirements, profile_skills, resume_text)
        if kw.lower() not in {s.lower() for s in selected_keywords}
    ]
    base["ats_keywords_added"] = list(selected_keywords)
    return base


def validate_v2_or_none(raw):
    """Thin wrapper exposing the schema validator under the heuristic_tailor
    namespace so callers in phases.py have one import."""
    return validate_v2(raw)


def merge_with_heuristic_v2(llm: TailoredResume | None, heuristic: TailoredResume) -> TailoredResume:
    """Hybrid merge: take any non-empty LLM field, fall back to heuristic."""
    out: TailoredResume = dict(heuristic)
    if not isinstance(llm, dict):
        return out
    for key in ("summary", "skills", "experience", "projects", "education",
                "awards", "certifications", "publications", "activities",
                "leadership", "volunteer", "coursework", "languages",
                "custom_sections", "section_order",
                "ats_keywords_added", "ats_keywords_missing"):
        if llm.get(key):
            out[key] = llm[key]
    for key in ("ats_score_before", "ats_score_after"):
        v = llm.get(key)
        if isinstance(v, int):
            out[key] = v
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

`pytest tests/unit/pipeline/test_heuristic_tailor_v2.py -v`

- [ ] **Step 5: Commit**

```bash
git add pipeline/heuristic_tailor.py tests/unit/pipeline/test_heuristic_tailor_v2.py
git commit -m "feat(pipeline): heuristic_tailor v2 path producing TailoredResume"
```

---

## Task 3: Provider tool schemas (v2)

**Files:**
- Modify: `pipeline/providers.py` (3 providers: Anthropic, Ollama, Demo)
- Test: extend `tests/unit/pipeline/test_providers_anthropic.py`, `test_providers_ollama.py`, `test_providers_demo.py`

Each provider's `tailor_resume(job, profile, resume_text, *, selected_keywords=None, source_format=None)` must return a v2 dict (passes `validate_v2_or_none`).

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/pipeline/test_providers_demo.py`

```python
def test_demo_tailor_resume_returns_v2():
    from pipeline.providers import DemoProvider
    from pipeline.heuristic_tailor import validate_v2_or_none

    prov = DemoProvider()
    profile = {
        "name": "Jane",
        "top_hard_skills": ["Python", "Verilog"],
        "experience": [{"title": "Intern", "company": "Acme", "dates": "2024",
                        "bullets": ["Built a thing"]}],
        "education": [{"degree": "BS", "institution": "Cal", "year": "2025"}],
    }
    job = {"title": "HW Eng", "company": "X",
           "requirements": ["Verilog", "FPGA verification"]}
    out = prov.tailor_resume(job, profile, "Jane resume",
                             selected_keywords=["FPGA verification"])
    assert validate_v2_or_none(out) is not None
    assert out["schema_version"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

`pytest tests/unit/pipeline/test_providers_demo.py -v -k test_demo_tailor_resume_returns_v2`

- [ ] **Step 3: Update `DemoProvider.tailor_resume` in `pipeline/providers.py`**

Replace the existing DemoProvider tailor_resume (around line 1761) with:

```python
def tailor_resume(self, job: dict, profile: dict, resume_text: str,
                  *, selected_keywords: list[str] | None = None,
                  source_format: str | None = None) -> dict:
    from .heuristic_tailor import heuristic_tailor_resume_v2
    return heuristic_tailor_resume_v2(job, profile, resume_text,
                                       selected_keywords=selected_keywords)
```

- [ ] **Step 4: Update `AnthropicProvider.tailor_resume`** — replace tool schema in `pipeline/providers.py` around line 592 with the comprehensive v2 schema.

Replace the existing method body (line 592-624) with:

```python
def tailor_resume(self, job: dict, profile: dict, resume_text: str,
                  *, selected_keywords: list[str] | None = None,
                  source_format: str | None = None) -> dict:
    from .tailored_schema import default_v2, SCHEMA_VERSION

    text_node = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "diff": {"type": "string", "enum": ["unchanged", "modified", "added"]},
            "original": {"type": "string"},
        },
        "required": ["text"],
    }
    role = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "company": {"type": "string"},
            "dates": {"type": "string"},
            "location": {"type": "string"},
            "bullets": {"type": "array", "items": text_node},
        },
    }
    generic = {
        "type": "object",
        "properties": {
            "title": text_node,
            "detail": text_node,
            "bullets": {"type": "array", "items": text_node},
        },
    }
    project = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": text_node,
            "skills_used": {"type": "array", "items": text_node},
            "bullets": {"type": "array", "items": text_node},
            "dates": {"type": "string"},
            "url": {"type": "string"},
        },
    }
    education = {
        "type": "object",
        "properties": {
            "institution": {"type": "string"},
            "degree": {"type": "string"},
            "dates": {"type": "string"},
            "gpa": {"type": "string"},
            "notes": {"type": "array", "items": text_node},
        },
    }
    skill_cat = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "items": {"type": "array", "items": text_node},
        },
    }
    custom = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "items": {"type": "array", "items": generic},
        },
    }

    tool = {
        "name": "tailored_resume_v2",
        "description": "Return a complete TailoredResume v2 covering every section in the source resume.",
        "input_schema": {
            "type": "object",
            "properties": {
                "schema_version": {"type": "integer"},
                "name": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "linkedin": {"type": "string"},
                "github": {"type": "string"},
                "location": {"type": "string"},
                "website": {"type": "string"},
                "summary": text_node,
                "skills": {"type": "array", "items": skill_cat},
                "experience": {"type": "array", "items": role},
                "projects": {"type": "array", "items": project},
                "education": {"type": "array", "items": education},
                "awards": {"type": "array", "items": generic},
                "certifications": {"type": "array", "items": generic},
                "publications": {"type": "array", "items": generic},
                "activities": {"type": "array", "items": generic},
                "leadership": {"type": "array", "items": generic},
                "volunteer": {"type": "array", "items": generic},
                "coursework": {"type": "array", "items": generic},
                "languages": {"type": "array", "items": generic},
                "custom_sections": {"type": "array", "items": custom},
                "section_order": {"type": "array", "items": {"type": "string"}},
                "ats_keywords_added": {"type": "array", "items": {"type": "string"}},
                "ats_keywords_missing": {"type": "array", "items": {"type": "string"}},
                "ats_score_before": {"type": "integer"},
                "ats_score_after": {"type": "integer"},
            },
            "required": ["name", "skills", "experience", "education", "section_order"],
        },
    }

    skeleton = default_v2(profile)
    selected_keywords = list(selected_keywords or [])
    declined = [k for k in (job.get("requirements") or [])
                if isinstance(k, str) and k not in selected_keywords]

    prompt = (
        f"Tailor this resume for: {job.get('title','')} at {job.get('company','')}.\n\n"
        "INPUT — full structured profile (you must preserve every section):\n"
        f"{json.dumps(skeleton, ensure_ascii=False)[:8000]}\n\n"
        f"JD requirements: {', '.join(job.get('requirements', []) or [])}\n"
        f"JD description: {job.get('description', '')[:2000]}\n\n"
        f"USER-SELECTED keywords to weave in: {', '.join(selected_keywords) or '(none — default to all must-have JD keywords missing from the resume)'}\n"
        f"USER-DECLINED keywords (do NOT include): {', '.join(declined[:20])}\n\n"
        "RULES:\n"
        "1. NEVER fabricate. NEVER change titles/companies/dates/institutions/degrees/GPA.\n"
        "2. For each user-selected keyword, REPHRASE an existing bullet (diff=modified) when it fits, else ADD a new bullet (diff=added) under the most relevant role.\n"
        "3. Reorder bullets within each role by JD relevance (no diff change for reorder alone).\n"
        "4. Keep every section from the source — even Awards / Publications / Coursework / Activities / Leadership / Volunteer / Languages / custom sections.\n"
        "5. Set diff=unchanged on every TextNode you didn't modify.\n"
        "6. Return the FULL TailoredResume — every section, every bullet, every role.\n"
        f"Source format hint: {source_format or 'pdf'}\n"
    )
    return self._tool_call(tool, prompt, max_tokens=8192, thinking=True)
```

- [ ] **Step 5: Update `OllamaProvider.tailor_resume`** — replace the existing method around line 2145.

Replace with:

```python
def tailor_resume(self, job: dict, profile: dict, resume_text: str,
                  *, selected_keywords: list[str] | None = None,
                  source_format: str | None = None) -> dict:
    """Ollama: ask for v2 JSON via response_format."""
    from .tailored_schema import default_v2

    skeleton = default_v2(profile)
    selected_keywords = list(selected_keywords or [])
    requirements = list(job.get("requirements") or [])
    declined = [k for k in requirements if isinstance(k, str) and k not in selected_keywords]

    system = (
        "You are a resume tailoring assistant. Output ONLY valid JSON matching "
        "the TailoredResume v2 schema. Preserve every section from the input "
        "skeleton — do not drop any. Diff markers: 'unchanged' (default), "
        "'modified' (you rewrote it), 'added' (new content). Never fabricate "
        "titles, companies, dates, institutions, degrees, or GPAs."
    )
    user = (
        f"Tailor for: {job.get('title','')} at {job.get('company','')}\n\n"
        f"INPUT skeleton:\n{json.dumps(skeleton, ensure_ascii=False)[:6000]}\n\n"
        f"JD requirements: {', '.join(requirements)}\n"
        f"JD description: {(job.get('description') or '')[:1500]}\n\n"
        f"USER-SELECTED keywords: {', '.join(selected_keywords) or '(default to all missing must-haves)'}\n"
        f"USER-DECLINED keywords: {', '.join(declined[:15])}\n\n"
        "Return ONLY the full TailoredResume v2 JSON, no markdown, no commentary."
    )
    try:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        return json.loads(m.group()) if m else {}
    except Exception as e:
        console.print(f"  [yellow]Ollama tailor error: {e}[/yellow]")
        return {}
```

- [ ] **Step 6: Run all provider tests**

```bash
pytest tests/unit/pipeline/test_providers_demo.py tests/unit/pipeline/test_providers_anthropic.py tests/unit/pipeline/test_providers_ollama.py -v
```

Existing tests that hit the old schema may need updating — fix them inline (they should still pass with v2 because validate_v2 accepts a superset).

- [ ] **Step 7: Commit**

```bash
git add pipeline/providers.py tests/unit/pipeline/test_providers_*.py
git commit -m "feat(pipeline): provider tool schemas emit TailoredResume v2"
```

---

## Task 4: Template library — base CSS + 6 HTML templates

**Files:**
- Create:
  - `pipeline/templates/_shared/base.css`
  - `pipeline/templates/_shared/diff.css`
  - `pipeline/templates/_macros.html.j2`
  - `pipeline/templates/single_column_classic.html.j2`
  - `pipeline/templates/single_column_modern.html.j2`
  - `pipeline/templates/two_column_left.html.j2`
  - `pipeline/templates/two_column_right.html.j2`
  - `pipeline/templates/compact_tech.html.j2`
  - `pipeline/templates/academic_multipage.html.j2`

- [ ] **Step 1: Create `pipeline/templates/_shared/base.css`**

```css
/* base.css — shared resets + variables consumed by every template */
:root {
  --accent: #1F4E79;
  --body-size: 10pt;
  --header-size: 12pt;
  --name-size: 22pt;
  --text-color: #111;
  --muted-color: #555;
  --rule-color: #d0d0d0;
  --page-margin: 0.55in;
  --font-body: "Times New Roman", Georgia, serif;
  --font-heading: "Times New Roman", Georgia, serif;
}
* { box-sizing: border-box; }
@page { size: Letter; margin: var(--page-margin); }
html, body {
  margin: 0; padding: 0; color: var(--text-color);
  font-family: var(--font-body); font-size: var(--body-size); line-height: 1.32;
}
h1.name { font-family: var(--font-heading); font-size: var(--name-size); margin: 0 0 4pt; text-align: center; letter-spacing: 0.02em; }
.contact { text-align: center; color: var(--muted-color); font-size: calc(var(--body-size) - 0.5pt); margin-bottom: 8pt; }
.contact span + span::before { content: "  ·  "; color: var(--rule-color); }
section.r { margin-top: 9pt; }
section.r > h2 { font-family: var(--font-heading); font-size: var(--header-size); color: var(--accent); margin: 0 0 2pt; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 0.6pt solid var(--accent); padding-bottom: 1pt; }
.entry { margin-top: 4pt; page-break-inside: avoid; }
.entry-h { display: flex; justify-content: space-between; gap: 8pt; }
.entry-h .l { font-weight: 700; }
.entry-h .r { color: var(--muted-color); font-style: italic; }
.entry-sub { display: flex; justify-content: space-between; gap: 8pt; color: var(--muted-color); font-style: italic; }
ul.bullets { margin: 2pt 0 0; padding-left: 16pt; }
ul.bullets li { margin-bottom: 1.5pt; }
.skills-line { line-height: 1.4; }
.skills-cat { margin-bottom: 2pt; }
.skills-cat-name { font-weight: 700; margin-right: 4pt; }
```

- [ ] **Step 2: Create `pipeline/templates/_shared/diff.css`**

```css
/* diff.css — green-highlight rules for modified/added text */
mark.diff-add { background: rgba(74, 222, 128, 0.20); color: #0a662c; border-bottom: 1.2pt solid #0a662c; padding: 0 1pt; text-decoration: none; }
mark.diff-mod { background: rgba(74, 222, 128, 0.10); color: #0a662c; border-bottom: 1.0pt dotted #0a662c; padding: 0 1pt; text-decoration: none; }
@media print { mark.diff-add, mark.diff-mod { -webkit-print-color-adjust: exact; print-color-adjust: exact; } }
```

- [ ] **Step 3: Create `pipeline/templates/_macros.html.j2`** — shared macros every template uses.

```jinja2
{# Render a TextNode wrapped in <mark> only when modified/added #}
{%- macro tn(node) -%}
{%- if not node -%}{%- elif node.diff == 'added' -%}<mark class="diff-add">{{ node.text }}</mark>{%- elif node.diff == 'modified' -%}<mark class="diff-mod">{{ node.text }}</mark>{%- else -%}{{ node.text }}{%- endif -%}
{%- endmacro -%}

{%- macro contact_line(t) -%}
<div class="contact">
{%- if t.email %}<span>{{ t.email }}</span>{% endif -%}
{%- if t.phone %}<span>{{ t.phone }}</span>{% endif -%}
{%- if t.linkedin %}<span>{{ t.linkedin }}</span>{% endif -%}
{%- if t.github %}<span>{{ t.github }}</span>{% endif -%}
{%- if t.website %}<span>{{ t.website }}</span>{% endif -%}
{%- if t.location %}<span>{{ t.location }}</span>{% endif -%}
</div>
{%- endmacro -%}

{%- macro skills_block(t) -%}
{% if t.skills %}<section class="r"><h2>Skills</h2>
{% for cat in t.skills %}
  <div class="skills-cat skills-line">
    {% if cat.name %}<span class="skills-cat-name">{{ cat.name }}:</span>{% endif %}
    {% for it in cat.get('items', []) %}{{ tn(it) }}{% if not loop.last %}, {% endif %}{% endfor %}
  </div>
{% endfor %}
</section>{% endif %}
{%- endmacro -%}

{%- macro experience_block(t) -%}
{% if t.experience %}<section class="r"><h2>Experience</h2>
{% for r in t.experience %}<div class="entry">
  <div class="entry-h"><span class="l">{{ r.title }}{% if r.company %} — {{ r.company }}{% endif %}</span><span class="r">{{ r.dates }}</span></div>
  {% if r.location %}<div class="entry-sub"><span>{{ r.location }}</span></div>{% endif %}
  {% if r.bullets %}<ul class="bullets">{% for b in r.bullets %}<li>{{ tn(b) }}</li>{% endfor %}</ul>{% endif %}
</div>{% endfor %}
</section>{% endif %}
{%- endmacro -%}

{%- macro projects_block(t) -%}
{% if t.projects %}<section class="r"><h2>Projects</h2>
{% for p in t.projects %}<div class="entry">
  <div class="entry-h"><span class="l">{{ p.name }}</span>{% if p.dates %}<span class="r">{{ p.dates }}</span>{% endif %}</div>
  {% if p.description and p.description.text %}<div>{{ tn(p.description) }}</div>{% endif %}
  {% if p.bullets %}<ul class="bullets">{% for b in p.bullets %}<li>{{ tn(b) }}</li>{% endfor %}</ul>{% endif %}
  {% if p.skills_used %}<div class="entry-sub"><span><em>Skills:</em>
    {% for s in p.skills_used %}{{ tn(s) }}{% if not loop.last %}, {% endif %}{% endfor %}</span></div>{% endif %}
</div>{% endfor %}
</section>{% endif %}
{%- endmacro -%}

{%- macro education_block(t) -%}
{% if t.education %}<section class="r"><h2>Education</h2>
{% for e in t.education %}<div class="entry">
  <div class="entry-h"><span class="l">{{ e.institution }}</span><span class="r">{{ e.dates }}</span></div>
  <div class="entry-sub"><span>{{ e.degree }}{% if e.gpa %} — GPA: {{ e.gpa }}{% endif %}</span>{% if e.location %}<span>{{ e.location }}</span>{% endif %}</div>
  {% if e.notes %}<ul class="bullets">{% for n in e.notes %}<li>{{ tn(n) }}</li>{% endfor %}</ul>{% endif %}
</div>{% endfor %}
</section>{% endif %}
{%- endmacro -%}

{%- macro generic_block(title, items) -%}
{% if items %}<section class="r"><h2>{{ title }}</h2>
{% for g in items %}<div class="entry">
  <div class="entry-h"><span class="l">{{ tn(g.title) }}</span>{% if g.detail and g.detail.text %}<span class="r">{{ tn(g.detail) }}</span>{% endif %}</div>
  {% if g.bullets %}<ul class="bullets">{% for b in g.bullets %}<li>{{ tn(b) }}</li>{% endfor %}</ul>{% endif %}
</div>{% endfor %}
</section>{% endif %}
{%- endmacro -%}

{%- macro custom_blocks(t) -%}
{% for cs in t.get('custom_sections', []) %}{{ generic_block(cs.name, cs['items']) }}{% endfor %}
{%- endmacro -%}

{%- macro summary_block(t) -%}
{% if t.summary and t.summary.text %}<section class="r"><h2>Summary</h2><p>{{ tn(t.summary) }}</p></section>{% endif %}
{%- endmacro -%}

{# Section dispatch: pick block by section_order #}
{%- macro section(t, name) -%}
{%- set k = name.lower() -%}
{%- if k == 'summary' -%}{{ summary_block(t) }}
{%- elif k == 'skills' -%}{{ skills_block(t) }}
{%- elif k == 'experience' -%}{{ experience_block(t) }}
{%- elif k == 'projects' -%}{{ projects_block(t) }}
{%- elif k == 'education' -%}{{ education_block(t) }}
{%- elif k == 'awards' -%}{{ generic_block('Awards', t.get('awards', [])) }}
{%- elif k == 'certifications' -%}{{ generic_block('Certifications', t.get('certifications', [])) }}
{%- elif k == 'publications' -%}{{ generic_block('Publications', t.get('publications', [])) }}
{%- elif k == 'activities' -%}{{ generic_block('Activities', t.get('activities', [])) }}
{%- elif k == 'leadership' -%}{{ generic_block('Leadership', t.get('leadership', [])) }}
{%- elif k == 'volunteer' -%}{{ generic_block('Volunteer', t.get('volunteer', [])) }}
{%- elif k == 'coursework' -%}{{ generic_block('Coursework', t.get('coursework', [])) }}
{%- elif k == 'languages' -%}{{ generic_block('Languages', t.get('languages', [])) }}
{%- endif -%}
{%- endmacro -%}
```

- [ ] **Step 4: Create `pipeline/templates/single_column_classic.html.j2`**

```jinja2
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{{ t.name }} — Resume</title>
<style>{{ base_css }}{{ diff_css }}
:root { --accent: {{ accent or '#1F4E79' }}; --body-size: {{ body_size or 10 }}pt; --header-size: {{ header_size or 12 }}pt; --name-size: {{ name_size or 22 }}pt; --font-body: "Times New Roman", Georgia, serif; --font-heading: "Times New Roman", Georgia, serif; }
</style></head><body>
{% from "_macros.html.j2" import tn, contact_line, section %}
<h1 class="name">{{ t.name }}</h1>
{{ contact_line(t) }}
{% for s in t.section_order %}{{ section(t, s) }}{% endfor %}
{% from "_macros.html.j2" import custom_blocks %}{{ custom_blocks(t) }}
</body></html>
```

- [ ] **Step 5: Create `pipeline/templates/single_column_modern.html.j2`**

```jinja2
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{{ t.name }} — Resume</title>
<style>{{ base_css }}{{ diff_css }}
:root { --accent: {{ accent or '#5e6ad2' }}; --body-size: {{ body_size or 10 }}pt; --header-size: {{ header_size or 11.5 }}pt; --name-size: {{ name_size or 20 }}pt; --font-body: "Inter", "Helvetica Neue", Arial, sans-serif; --font-heading: "Inter", "Helvetica Neue", Arial, sans-serif; }
section.r > h2 { border-bottom: 1.2pt solid var(--accent); padding-bottom: 2pt; }
h1.name { letter-spacing: 0.04em; text-align: left; font-weight: 700; }
.contact { text-align: left; }
</style></head><body>
{% from "_macros.html.j2" import tn, contact_line, section, custom_blocks %}
<h1 class="name">{{ t.name }}</h1>
{{ contact_line(t) }}
{% for s in t.section_order %}{{ section(t, s) }}{% endfor %}
{{ custom_blocks(t) }}
</body></html>
```

- [ ] **Step 6: Create `pipeline/templates/two_column_left.html.j2`**

```jinja2
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{{ t.name }} — Resume</title>
<style>{{ base_css }}{{ diff_css }}
:root { --accent: {{ accent or '#1F4E79' }}; --body-size: {{ body_size or 9.5 }}pt; --header-size: {{ header_size or 11 }}pt; --name-size: {{ name_size or 19 }}pt; --font-body: "Helvetica", Arial, sans-serif; --font-heading: "Helvetica", Arial, sans-serif; }
.layout { display: grid; grid-template-columns: 33% 1fr; gap: 14pt; margin-top: 8pt; }
.sidebar section.r > h2 { font-size: calc(var(--header-size) - 0.5pt); }
.sidebar { background: rgba(0,0,0,0.025); padding: 8pt; }
h1.name { text-align: left; }
.contact { text-align: left; }
</style></head><body>
{% from "_macros.html.j2" import tn, contact_line, skills_block, experience_block, projects_block, education_block, generic_block, custom_blocks, section %}
<h1 class="name">{{ t.name }}</h1>
{{ contact_line(t) }}
<div class="layout">
  <aside class="sidebar">
    {{ skills_block(t) }}
    {{ generic_block('Languages', t.get('languages', [])) }}
    {{ generic_block('Certifications', t.get('certifications', [])) }}
    {{ generic_block('Awards', t.get('awards', [])) }}
    {{ education_block(t) }}
  </aside>
  <main class="body">
    {{ experience_block(t) }}
    {{ projects_block(t) }}
    {{ generic_block('Publications', t.get('publications', [])) }}
    {{ generic_block('Leadership', t.get('leadership', [])) }}
    {{ generic_block('Volunteer', t.get('volunteer', [])) }}
    {{ generic_block('Activities', t.get('activities', [])) }}
    {{ custom_blocks(t) }}
  </main>
</div>
</body></html>
```

- [ ] **Step 7: Create `pipeline/templates/two_column_right.html.j2`** — same as left but with sidebar on right.

```jinja2
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{{ t.name }} — Resume</title>
<style>{{ base_css }}{{ diff_css }}
:root { --accent: {{ accent or '#1F4E79' }}; --body-size: {{ body_size or 9.5 }}pt; --header-size: {{ header_size or 11 }}pt; --name-size: {{ name_size or 19 }}pt; --font-body: "Helvetica", Arial, sans-serif; --font-heading: "Helvetica", Arial, sans-serif; }
.layout { display: grid; grid-template-columns: 1fr 33%; gap: 14pt; margin-top: 8pt; }
.sidebar section.r > h2 { font-size: calc(var(--header-size) - 0.5pt); }
.sidebar { background: rgba(0,0,0,0.025); padding: 8pt; }
h1.name { text-align: left; }
.contact { text-align: left; }
</style></head><body>
{% from "_macros.html.j2" import tn, contact_line, skills_block, experience_block, projects_block, education_block, generic_block, custom_blocks %}
<h1 class="name">{{ t.name }}</h1>
{{ contact_line(t) }}
<div class="layout">
  <main class="body">
    {{ experience_block(t) }}
    {{ projects_block(t) }}
    {{ generic_block('Publications', t.get('publications', [])) }}
    {{ generic_block('Leadership', t.get('leadership', [])) }}
    {{ custom_blocks(t) }}
  </main>
  <aside class="sidebar">
    {{ skills_block(t) }}
    {{ generic_block('Languages', t.get('languages', [])) }}
    {{ generic_block('Certifications', t.get('certifications', [])) }}
    {{ generic_block('Awards', t.get('awards', [])) }}
    {{ education_block(t) }}
  </aside>
</div>
</body></html>
```

- [ ] **Step 8: Create `pipeline/templates/compact_tech.html.j2`**

```jinja2
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{{ t.name }} — Resume</title>
<style>{{ base_css }}{{ diff_css }}
:root { --accent: {{ accent or '#0a662c' }}; --body-size: {{ body_size or 9 }}pt; --header-size: {{ header_size or 10 }}pt; --name-size: {{ name_size or 17 }}pt; --font-body: "Helvetica Neue", Arial, sans-serif; --font-heading: "JetBrains Mono", "Menlo", "Monaco", monospace; --page-margin: 0.4in; }
section.r > h2 { font-family: var(--font-heading); border-bottom: 0.5pt solid #888; color: #222; }
h1.name { text-align: left; font-family: var(--font-heading); font-weight: 700; letter-spacing: 0.06em; }
.contact { text-align: left; }
.entry { margin-top: 3pt; }
ul.bullets li { margin-bottom: 0.8pt; }
</style></head><body>
{% from "_macros.html.j2" import tn, contact_line, section, custom_blocks %}
<h1 class="name">{{ t.name }}</h1>
{{ contact_line(t) }}
{% for s in t.section_order %}{{ section(t, s) }}{% endfor %}
{{ custom_blocks(t) }}
</body></html>
```

- [ ] **Step 9: Create `pipeline/templates/academic_multipage.html.j2`**

```jinja2
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{{ t.name }} — Curriculum Vitae</title>
<style>{{ base_css }}{{ diff_css }}
:root { --accent: {{ accent or '#1F4E79' }}; --body-size: {{ body_size or 10.5 }}pt; --header-size: {{ header_size or 12 }}pt; --name-size: {{ name_size or 22 }}pt; --font-body: "Times New Roman", Georgia, serif; --font-heading: "Times New Roman", Georgia, serif; --page-margin: 0.7in; }
@page { @bottom-center { content: counter(page) " / " counter(pages); font-size: 9pt; color: #666; } }
section.r > h2 { font-variant: small-caps; letter-spacing: 0.06em; }
.entry { margin-top: 5pt; page-break-inside: avoid; }
</style></head><body>
{% from "_macros.html.j2" import tn, contact_line, section, custom_blocks, generic_block %}
<h1 class="name">{{ t.name }}</h1>
{{ contact_line(t) }}
{% set ordered = t.section_order or ['Education','Publications','Experience','Awards','Skills'] %}
{% for s in ordered %}{{ section(t, s) }}{% endfor %}
{{ custom_blocks(t) }}
</body></html>
```

- [ ] **Step 10: Commit**

```bash
git add pipeline/templates/
git commit -m "feat(pipeline): add 6-template HTML/CSS resume library"
```

---

## Task 5: Template renderer (Jinja2 + WeasyPrint + reportlab fallback)

**Files:**
- Create: `pipeline/template_render.py`, `tests/unit/pipeline/test_template_render.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Add deps to `requirements.txt`**

Append after the existing block (before the optional ollama line):

```
jinja2>=3.1.0
weasyprint>=62.0
docx2pdf>=0.1.8 ; sys_platform == "win32" or sys_platform == "darwin"
```

- [ ] **Step 2: Write the failing tests** — `tests/unit/pipeline/test_template_render.py`

```python
import pytest
from pathlib import Path
from pipeline.template_render import render_html, render_pdf, list_templates
from pipeline.tailored_schema import default_v2

pytestmark = pytest.mark.unit


def _profile():
    return {
        "name": "Jane Doe",
        "email": "j@example.com",
        "top_hard_skills": ["Python", "Verilog"],
        "experience": [{"title": "Intern", "company": "Acme", "dates": "2024",
                        "bullets": ["Built it", "Tested it"]}],
        "education": [{"degree": "BS EE", "institution": "Cal", "year": "2025"}],
    }


def test_list_templates_returns_six():
    ids = list_templates()
    assert len(ids) == 6
    assert "single_column_classic" in ids


def test_render_html_contains_name_and_section():
    t = default_v2(_profile())
    html = render_html(t, "single_column_classic", format_profile={})
    assert "Jane Doe" in html
    assert "Skills" in html
    assert "Experience" in html


def test_render_html_emits_diff_marks_when_present():
    t = default_v2(_profile())
    t["skills"] = [{"name": "", "items": [
        {"text": "Python", "diff": "unchanged"},
        {"text": "Rust", "diff": "added"},
    ]}]
    html = render_html(t, "single_column_classic", format_profile={})
    assert 'class="diff-add"' in html


def test_render_pdf_writes_a_file(tmp_path):
    t = default_v2(_profile())
    html = render_html(t, "single_column_classic", format_profile={})
    out = tmp_path / "resume.pdf"
    ok = render_pdf(html, out)
    # If neither WeasyPrint nor reportlab is available the test environment is broken — skip.
    if not ok:
        pytest.skip("No PDF backend available")
    assert out.exists()
    assert out.stat().st_size > 1000
```

- [ ] **Step 3: Run tests to verify they fail**

`pytest tests/unit/pipeline/test_template_render.py -v` → ImportError.

- [ ] **Step 4: Create `pipeline/template_render.py`**

```python
"""
pipeline/template_render.py
───────────────────────────
Renders a TailoredResume v2 → HTML (via Jinja2) → PDF (WeasyPrint, with
reportlab fallback). The HTML produced is also served back to the SPA
preview so the in-page green highlights match the downloaded PDF byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import console

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_SHARED = _TEMPLATES_DIR / "_shared"

_TEMPLATE_IDS = [
    "single_column_classic",
    "single_column_modern",
    "two_column_left",
    "two_column_right",
    "compact_tech",
    "academic_multipage",
]


def list_templates() -> list[str]:
    return list(_TEMPLATE_IDS)


def _load_jinja_env():
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError as e:
        raise RuntimeError(
            "jinja2 not installed. pip install jinja2."
        ) from e
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "htm", "xml"]),
        trim_blocks=False, lstrip_blocks=False,
    )
    return env


def _read_shared_css() -> tuple[str, str]:
    base = (_SHARED / "base.css").read_text(encoding="utf-8")
    diff = (_SHARED / "diff.css").read_text(encoding="utf-8")
    return base, diff


def render_html(
    tailored: dict,
    template_id: str,
    format_profile: dict | None = None,
) -> str:
    """Render the TailoredResume v2 into a self-contained HTML string."""
    if template_id not in _TEMPLATE_IDS:
        template_id = "single_column_classic"
    env = _load_jinja_env()
    tmpl = env.get_template(f"{template_id}.html.j2")
    base_css, diff_css = _read_shared_css()
    fp = format_profile or {}
    body_size = float(fp.get("body_font_size") or 10)
    body_size = max(8.5, min(12.5, body_size))
    header_size = float(fp.get("header_font_size") or (body_size + 1.5))
    header_size = max(body_size + 0.5, min(14.0, header_size))
    name_size = round(min(24, header_size + 7.5))
    accent = fp.get("accent_color")
    if not (isinstance(accent, str) and accent.startswith("#") and len(accent) == 7):
        accent = None
    return tmpl.render(
        t=tailored,
        base_css=base_css,
        diff_css=diff_css,
        body_size=body_size,
        header_size=header_size,
        name_size=name_size,
        accent=accent,
    )


def render_pdf(html: str, output_path: Path) -> bool:
    """Render an HTML string to a PDF file. Tries WeasyPrint first, then
    falls back to a reportlab-based plaintext render. Returns True on success."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if _render_pdf_weasyprint(html, output_path):
        return True
    console.print("  [yellow]WeasyPrint unavailable — falling back to reportlab text render.[/yellow]")
    return _render_pdf_reportlab_fallback(html, output_path)


def _render_pdf_weasyprint(html: str, output_path: Path) -> bool:
    try:
        from weasyprint import HTML  # type: ignore
    except Exception as e:  # ImportError, OSError (Windows GTK)
        console.print(f"  [yellow]WeasyPrint not available: {type(e).__name__}: {e}[/yellow]")
        return False
    try:
        HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf(target=str(output_path))
        return output_path.exists()
    except Exception as e:
        console.print(f"  [yellow]WeasyPrint render error: {e}[/yellow]")
        return False


def _render_pdf_reportlab_fallback(html: str, output_path: Path) -> bool:
    """Last-ditch: strip HTML and render the plaintext via reportlab. The
    user gets a usable PDF even if WeasyPrint can't install. Loses layout
    polish — but never green highlights — because <mark> wraps survive as
    explicit markers."""
    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    except ImportError:
        return False
    import re
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<head[^>]*>.*?</head>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<mark class="diff-add">(.*?)</mark>',
                  r'<font color="#0a662c"><b>\1</b></font>', text, flags=re.DOTALL)
    text = re.sub(r'<mark class="diff-mod">(.*?)</mark>',
                  r'<font color="#0a662c">\1</font>', text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "<br/>", text)
    text = re.sub(r"</?(html|body|div|main|aside|section|header|footer)[^>]*>",
                  "", text, flags=re.IGNORECASE)
    text = re.sub(r"<h1[^>]*>(.*?)</h1>", r"<<<H1>>>\1<<</H1>>>", text, flags=re.DOTALL)
    text = re.sub(r"<h2[^>]*>(.*?)</h2>", r"<<<H2>>>\1<<</H2>>>", text, flags=re.DOTALL)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"• \1<br/>", text, flags=re.DOTALL)
    text = re.sub(r"</?(ul|ol)[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("<<<H1>>>", "").replace("<<</H1>>>", "")
    text = text.replace("<<<H2>>>", "\n").replace("<<</H2>>>", "\n")

    styles = getSampleStyleSheet()
    body_style = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=12)
    story = []
    for para in text.split("\n"):
        para = para.strip()
        if para:
            story.append(Paragraph(para, body_style))
            story.append(Spacer(1, 2))
    try:
        SimpleDocTemplate(
            str(output_path), pagesize=LETTER,
            leftMargin=0.7 * inch, rightMargin=0.7 * inch,
            topMargin=0.55 * inch, bottomMargin=0.55 * inch,
        ).build(story)
        return output_path.exists()
    except Exception as e:
        console.print(f"  [yellow]reportlab fallback error: {e}[/yellow]")
        return False
```

- [ ] **Step 5: Install deps and run tests**

```bash
pip install jinja2 weasyprint
pytest tests/unit/pipeline/test_template_render.py -v
```

If WeasyPrint fails to install on Windows due to GTK, the reportlab fallback still produces a PDF and the test passes via the skip path; document this in `AGENTS.md` if not already.

- [ ] **Step 6: Commit**

```bash
git add pipeline/template_render.py tests/unit/pipeline/test_template_render.py requirements.txt
git commit -m "feat(pipeline): template renderer (WeasyPrint + reportlab fallback)"
```

---

## Task 6: Template matcher

**Files:**
- Create: `pipeline/template_match.py`, `tests/unit/pipeline/test_template_match.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/pipeline/test_template_match.py
import pytest
from pipeline.template_match import pick_template

pytestmark = pytest.mark.unit


def test_pick_template_two_column_when_columns_2():
    tid, conf = pick_template({"columns": 2, "body_font_size": 10}, "")
    assert tid in ("two_column_left", "two_column_right")
    assert conf > 0.4


def test_pick_template_compact_when_small_font():
    tid, conf = pick_template({"columns": 1, "body_font_size": 8.5}, "")
    assert tid == "compact_tech"


def test_pick_template_academic_when_publications_dense():
    text = ("DOI: 10.1234/abc " * 10) + "et al. " * 8
    tid, _conf = pick_template({"columns": 1, "body_font_size": 10}, text)
    assert tid == "academic_multipage"


def test_pick_template_classic_default():
    tid, _ = pick_template({}, "")
    assert tid == "single_column_classic"


def test_pick_template_modern_when_accent_chromatic():
    tid, _ = pick_template({"columns": 1, "accent_color": "#5e6ad2", "body_font_size": 10.5}, "")
    assert tid == "single_column_modern"
```

- [ ] **Step 2: Run tests to verify they fail**

`pytest tests/unit/pipeline/test_template_match.py -v` → ImportError.

- [ ] **Step 3: Create `pipeline/template_match.py`**

```python
"""
pipeline/template_match.py
──────────────────────────
Score a `format_profile` (and resume_text) against the 6 HTML/CSS
templates. Returns the best (template_id, confidence) pair.
"""
from __future__ import annotations

import re

_TEMPLATE_IDS = [
    "single_column_classic",
    "single_column_modern",
    "two_column_left",
    "two_column_right",
    "compact_tech",
    "academic_multipage",
]

_DOI_RE = re.compile(r"\bdoi[:\s]\s*10\.\d{4,9}/", re.I)
_ETAL_RE = re.compile(r"\bet\s+al\.\b", re.I)


def _is_chromatic(hex_color: str | None) -> bool:
    if not isinstance(hex_color, str) or not hex_color.startswith("#") or len(hex_color) != 7:
        return False
    try:
        r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    except ValueError:
        return False
    return (max(r, g, b) - min(r, g, b)) > 30 and max(r, g, b) > 60


def pick_template(format_profile: dict | None, resume_text: str = "") -> tuple[str, float]:
    fp = format_profile or {}
    text = resume_text or ""

    columns = int(fp.get("columns") or 1)
    body_size = float(fp.get("body_font_size") or 10)
    accent = fp.get("accent_color")
    chromatic = _is_chromatic(accent if isinstance(accent, str) else None)

    pubs = len(_DOI_RE.findall(text)) + len(_ETAL_RE.findall(text))
    is_academic = pubs >= 6 and columns == 1

    scores: dict[str, float] = {tid: 0.0 for tid in _TEMPLATE_IDS}

    # Column dimension dominates
    if columns == 2:
        scores["two_column_left"] += 0.50
        scores["two_column_right"] += 0.45
    else:
        scores["single_column_classic"] += 0.30
        scores["single_column_modern"] += 0.25
        scores["academic_multipage"] += 0.20
        scores["compact_tech"] += 0.20

    # Compact when body font is small
    if body_size < 9.5:
        scores["compact_tech"] += 0.45

    # Modern when there's a chromatic accent
    if chromatic:
        scores["single_column_modern"] += 0.30
        scores["compact_tech"] += 0.05

    # Classic when no accent and serif-y body size
    if not chromatic and 9.5 <= body_size <= 11.5 and columns == 1:
        scores["single_column_classic"] += 0.20

    # Academic when publications + DOIs are dense
    if is_academic:
        scores["academic_multipage"] += 0.55

    best_id = max(scores, key=scores.get)
    return best_id, min(1.0, scores[best_id])
```

- [ ] **Step 4: Run tests**

`pytest tests/unit/pipeline/test_template_match.py -v` → green.

- [ ] **Step 5: Commit**

```bash
git add pipeline/template_match.py tests/unit/pipeline/test_template_match.py
git commit -m "feat(pipeline): template matcher scores format_profile against 6 layouts"
```

---

## Task 7: `_save_tailored_resume` dispatch + default-template path

**Files:**
- Modify: `pipeline/resume.py` (around line 715-773)

- [ ] **Step 1: Add the dispatch logic to `_save_tailored_resume`**

Replace the existing function body with:

```python
def _save_tailored_resume(job: dict, tailored: dict, profile: dict = None,
                          latex_source: str = None,
                          resume_text: str = "",
                          output_dir: Path = None,
                          owner_name: str = None,
                          format_profile: dict | None = None,
                          source_format: str | None = None,
                          source_bytes_path: Path | None = None) -> dict:
    """Write the tailored resume to OUTPUT_DIR.

    Returns ``{"tex": str|None, "pdf": str|None, "docx": str|None,
              "html_preview": str|None, "base": str, "template_id": str|None,
              "template_confidence": float|None}``.

    Dispatch by source_format:
      tex   → in-place LaTeX rewrite via pipeline.latex_tailor
      docx  → in-place python-docx rewrite via pipeline.docx_tailor
      pdf   → template-library match via pipeline.template_render
      else  → default template (single_column_classic)

    Backwards-compatible: when source_format is None or the legacy v1 dict
    is passed, falls through to the template-library path with a v2 adapter.
    """
    from .tailored_schema import legacy_to_v2, validate_v2, default_v2, SCHEMA_VERSION
    from .template_match import pick_template
    from .template_render import render_html, render_pdf

    safe = lambda s: re.sub(r"[^a-zA-Z0-9_\-]", "_", s)
    name = owner_name or OWNER_NAME
    base = (
        f"{safe(name)}_Resume_{safe(job.get('company', ''))}"
        f"_{safe(job.get('title', ''))}"
    )
    out_dir = output_dir or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Normalize tailored to v2 ────────────────────────────────────────────
    if not isinstance(tailored, dict):
        tailored = {}
    if tailored.get("schema_version") == SCHEMA_VERSION:
        v2 = validate_v2(tailored) or default_v2(profile)
    else:
        v2 = legacy_to_v2(tailored, profile)
    if owner_name:
        v2["name"] = owner_name

    # ── Source-format dispatch ──────────────────────────────────────────────
    src = (source_format or "").lower()
    if src == "tex" and latex_source:
        from .latex_tailor import tailor_latex_in_place
        return tailor_latex_in_place(
            v2, latex_source=latex_source, base=base, out_dir=out_dir,
            format_profile=format_profile,
        )
    if src == "docx" and source_bytes_path and Path(source_bytes_path).exists():
        from .docx_tailor import tailor_docx_in_place
        return tailor_docx_in_place(
            v2, source_path=Path(source_bytes_path), base=base, out_dir=out_dir,
            format_profile=format_profile,
        )

    # PDF / unknown / default → template library
    template_id, confidence = pick_template(format_profile, resume_text)
    html = render_html(v2, template_id, format_profile=format_profile)
    html_path = out_dir / (base + "_preview.html")
    html_path.write_text(html, encoding="utf-8")
    pdf_path = out_dir / (base + ".pdf")
    pdf_ok = render_pdf(html, pdf_path)
    return {
        "tex": None,
        "pdf": pdf_path.name if pdf_ok else None,
        "docx": None,
        "html_preview": html_path.name,
        "base": base,
        "template_id": template_id,
        "template_confidence": round(float(confidence), 2),
    }
```

- [ ] **Step 2: Update import block at top of `pipeline/resume.py`**

Verify the `Path` import already exists; no other changes needed (the deferred imports inside the function avoid circular imports during module load).

- [ ] **Step 3: Smoke-test the default path**

```bash
python -c "
from pathlib import Path
import tempfile
from pipeline.resume import _save_tailored_resume
from pipeline.tailored_schema import default_v2

profile = {'name': 'Jane Doe', 'email': 'j@example.com',
           'top_hard_skills': ['Python', 'Verilog'],
           'experience': [{'title': 'Intern', 'company': 'Acme', 'dates': '2024',
                            'bullets': ['Built it', 'Tested it']}],
           'education': [{'degree': 'BS EE', 'institution': 'Cal', 'year': '2025'}]}
v2 = default_v2(profile)
with tempfile.TemporaryDirectory() as d:
    out = _save_tailored_resume({'company': 'Foo', 'title': 'HW Eng'}, v2, profile,
                                output_dir=Path(d))
    print(out)
    print('OK' if out.get('html_preview') else 'FAIL')
"
```

Expected: prints a dict with `html_preview`, `base`, `template_id`. PDF may be None on Windows without WeasyPrint.

- [ ] **Step 4: Commit**

```bash
git add pipeline/resume.py
git commit -m "feat(pipeline): _save_tailored_resume dispatches by source_format"
```

---

## Task 8: LaTeX in-place rewriter

**Files:**
- Create: `pipeline/latex_tailor.py`, `tests/unit/pipeline/test_latex_tailor.py`, `tests/fixtures/resumes/jake_classic.tex`

- [ ] **Step 1: Create the fixture** — `tests/fixtures/resumes/jake_classic.tex`

```latex
\documentclass[11pt,letterpaper]{article}
\usepackage[margin=0.75in]{geometry}
\usepackage{enumitem}
\setlist[itemize]{leftmargin=*,nosep}
\pagenumbering{gobble}
\begin{document}
\begin{center}
{\LARGE \textbf{Jane Doe}}\\
\small jane@example.com $\bullet$ Berkeley, CA
\end{center}

\section*{Skills}
Python, C++, Verilog, MATLAB

\section*{Experience}
\noindent\textbf{Intern $\vert$ Acme Corp $\vert$ 2024}
\begin{itemize}
\item Built a thing for the team
\item Tested another thing
\end{itemize}

\noindent\textbf{Research Assistant $\vert$ Cal Photonics Lab $\vert$ 2023}
\begin{itemize}
\item Aligned an interferometer
\end{itemize}

\section*{Education}
University of California, Berkeley -- B.S. Electrical Engineering -- 2025

\end{document}
```

- [ ] **Step 2: Write the failing tests** — `tests/unit/pipeline/test_latex_tailor.py`

```python
import pytest
from pathlib import Path
from pipeline.latex_tailor import tailor_latex_in_place
from pipeline.tailored_schema import default_v2

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "resumes" / "jake_classic.tex"


def _v2():
    profile = {
        "name": "Jane Doe", "email": "jane@example.com",
        "top_hard_skills": ["Python", "Verilog", "C++", "MATLAB"],
        "experience": [
            {"title": "Intern", "company": "Acme Corp", "dates": "2024",
             "bullets": ["Built a thing for the team", "Tested another thing"]},
            {"title": "Research Assistant", "company": "Cal Photonics Lab", "dates": "2023",
             "bullets": ["Aligned an interferometer"]},
        ],
        "education": [{"degree": "B.S. Electrical Engineering",
                        "institution": "University of California, Berkeley", "year": "2025"}],
    }
    v2 = default_v2(profile)
    # Modify a bullet
    v2["experience"][0]["bullets"][0] = {
        "text": "Built a Verilog testbench for the team",
        "original": "Built a thing for the team",
        "diff": "modified",
    }
    # Add a new skill (FPGA verification)
    v2["skills"][0]["items"].append({"text": "FPGA verification", "diff": "added"})
    return v2


def test_in_place_writes_tex_with_modified_bullet(tmp_path):
    src = FIXTURE.read_text(encoding="utf-8")
    out = tailor_latex_in_place(_v2(), latex_source=src, base="resume", out_dir=tmp_path)
    assert out["tex"] == "resume.tex"
    text = (tmp_path / "resume.tex").read_text(encoding="utf-8")
    assert "Verilog testbench" in text
    # diff="modified" wraps in green color
    assert "textcolor" in text
    assert r"\usepackage{xcolor}" in text


def test_in_place_preserves_unchanged_bullet(tmp_path):
    src = FIXTURE.read_text(encoding="utf-8")
    out = tailor_latex_in_place(_v2(), latex_source=src, base="resume", out_dir=tmp_path)
    text = (tmp_path / "resume.tex").read_text(encoding="utf-8")
    assert "Aligned an interferometer" in text


def test_in_place_appends_added_skill(tmp_path):
    src = FIXTURE.read_text(encoding="utf-8")
    tailor_latex_in_place(_v2(), latex_source=src, base="resume", out_dir=tmp_path)
    text = (tmp_path / "resume.tex").read_text(encoding="utf-8")
    assert "FPGA verification" in text
```

- [ ] **Step 3: Create `pipeline/latex_tailor.py`**

```python
"""
pipeline/latex_tailor.py
────────────────────────
In-place LaTeX rewriter. Preserves the user's original .tex template
verbatim; only swaps text inside Skills, bullet items, and adds new bullets
for diff='added' nodes.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import console
from .latex import compile_latex_to_pdf, _sanitize_latex_source
from .template_render import render_html


def _latex_escape(s: str) -> str:
    if not s:
        return ""
    return (
        str(s)
        .replace("\\", r"\textbackslash{}")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("$", r"\$")
        .replace("#", r"\#")
        .replace("_", r"\_")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("~", r"\textasciitilde{}")
        .replace("^", r"\textasciicircum{}")
    )


def _wrap_diff(text: str, diff: str) -> str:
    esc = _latex_escape(text)
    if diff == "added" or diff == "modified":
        return r"\textcolor{green!50!black}{" + esc + r"}"
    return esc


def _ensure_xcolor(latex: str) -> str:
    if r"\usepackage{xcolor}" in latex or r"\usepackage[" in latex and "xcolor" in latex:
        return latex
    # Insert before \begin{document}
    if r"\begin{document}" in latex:
        return latex.replace(r"\begin{document}",
                             "\\usepackage{xcolor}\n\\begin{document}")
    return "\\usepackage{xcolor}\n" + latex


_SECTION_RE = re.compile(
    r"(\\section\*?\{(?P<head>[^}]+)\})(?P<body>.*?)(?=\\section\*?\{|\\end\{document\})",
    re.DOTALL,
)


def _replace_skills_section(latex: str, tailored: dict) -> str:
    cats = tailored.get("skills") or []
    if not cats:
        return latex
    flat: list[str] = []
    diffs: list[str] = []
    for cat in cats:
        for it in cat.get("items", []):
            flat.append(it.get("text") or "")
            diffs.append(it.get("diff") or "unchanged")
    if not flat:
        return latex
    rendered = ", ".join(_wrap_diff(t, d) for t, d in zip(flat, diffs))

    def _sub(m):
        head_text = (m.group("head") or "").lower()
        if any(k in head_text for k in ("skill", "competenc")):
            return m.group(1) + "\n" + rendered + "\n\n"
        return m.group(0)

    return _SECTION_RE.sub(_sub, latex)


_ITEMIZE_RE = re.compile(
    r"(\\begin\{itemize\})(?P<body>.*?)(\\end\{itemize\})",
    re.DOTALL,
)
_ITEM_RE = re.compile(r"\\item\s+(.+?)(?=\\item|\Z)", re.DOTALL)


def _replace_experience_bullets(latex: str, tailored: dict) -> str:
    """Match itemize blocks to roles by the bold header that precedes them
    (e.g., \\noindent\\textbf{Intern | Acme | 2024}). Replace the bullet
    contents in order; append diff='added' bullets at the end."""
    roles = tailored.get("experience") or []
    if not roles:
        return latex

    def _matches_role(header_text: str, role: dict) -> bool:
        ht = header_text.lower()
        title = (role.get("title") or "").lower()
        company = (role.get("company") or "").lower()
        return (title and title in ht) or (company and company in ht)

    # Walk itemize blocks; for each, find the preceding non-blank line as header.
    out_chunks: list[str] = []
    cursor = 0
    for m in _ITEMIZE_RE.finditer(latex):
        out_chunks.append(latex[cursor:m.start()])
        # Look back up to ~400 chars for a textbf header
        prefix = latex[max(0, m.start() - 400):m.start()]
        header_match = re.search(r"\\textbf\{([^}]+)\}", prefix[::-1])
        header_text = ""
        if header_match:
            # We searched reversed; reverse the match back
            header_text = header_match.group(1)[::-1]
        # Pick the role this block belongs to
        role = next((r for r in roles if _matches_role(header_text, r)), None)
        body = m.group("body")
        if role:
            new_items: list[str] = []
            for b in role.get("bullets") or []:
                wrapped = _wrap_diff(b.get("text") or "", b.get("diff") or "unchanged")
                new_items.append(f"\\item {wrapped}")
            new_body = "\n" + "\n".join(new_items) + "\n"
            out_chunks.append("\\begin{itemize}" + new_body + "\\end{itemize}")
        else:
            out_chunks.append(m.group(0))
        cursor = m.end()
    out_chunks.append(latex[cursor:])
    return "".join(out_chunks)


def _strip_summary(latex: str) -> str:
    pattern = (
        r"\\section\*?\{(?:Summary|Objective|Professional Summary|Career Objective)\}"
        r".*?(?=\\section|\\end\{document\})"
    )
    return re.sub(pattern, "", latex, flags=re.IGNORECASE | re.DOTALL)


def tailor_latex_in_place(
    tailored: dict, latex_source: str, base: str, out_dir: Path,
    format_profile: dict | None = None,
) -> dict:
    """Apply tailoring to a LaTeX source in place. Output: .tex, .pdf (when
    pdflatex is available), .html (preview)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    src = _ensure_xcolor(latex_source)
    src = _strip_summary(src)
    src = _replace_skills_section(src, tailored)
    src = _replace_experience_bullets(src, tailored)
    src = _sanitize_latex_source(src)

    tex_path = out_dir / (base + ".tex")
    tex_path.write_text(src, encoding="utf-8")

    pdf_path = out_dir / (base + ".pdf")
    pdf_ok = compile_latex_to_pdf(src, pdf_path)

    # HTML preview (renders the v2 dict via single_column_classic — used by
    # the SPA preview pane; the .tex/.pdf is the primary download)
    html = render_html(tailored, "single_column_classic", format_profile=format_profile)
    html_path = out_dir / (base + "_preview.html")
    html_path.write_text(html, encoding="utf-8")

    return {
        "tex": tex_path.name,
        "pdf": pdf_path.name if pdf_ok else None,
        "docx": None,
        "html_preview": html_path.name,
        "base": base,
        "template_id": "in_place_latex",
        "template_confidence": 1.0,
    }
```

- [ ] **Step 4: Run tests**

`pytest tests/unit/pipeline/test_latex_tailor.py -v` — all green.

- [ ] **Step 5: Commit**

```bash
git add pipeline/latex_tailor.py tests/unit/pipeline/test_latex_tailor.py tests/fixtures/resumes/jake_classic.tex
git commit -m "feat(pipeline): in-place LaTeX rewriter preserves source template"
```

---

## Task 9: DOCX in-place rewriter

**Files:**
- Create: `pipeline/docx_tailor.py`, `tests/unit/pipeline/test_docx_tailor.py`, `tests/fixtures/resumes/modern_sans.docx` (built by a script in this task)

- [ ] **Step 1: Build the DOCX fixture** — write a one-off script `tests/fixtures/build_resumes.py`

```python
"""Build deterministic .docx fixture for tests. Run once: python tests/fixtures/build_resumes.py"""
from pathlib import Path
from docx import Document
from docx.shared import Pt, RGBColor

HERE = Path(__file__).parent / "resumes"
HERE.mkdir(exist_ok=True)


def build_modern_sans():
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Helvetica"
    style.font.size = Pt(10)

    head = doc.add_paragraph()
    run = head.add_run("Jane Doe")
    run.bold = True; run.font.size = Pt(20)
    doc.add_paragraph("jane@example.com  ·  Berkeley, CA")

    h_skills = doc.add_paragraph()
    h_skills.add_run("SKILLS").bold = True
    doc.add_paragraph("Python, C++, Verilog, MATLAB")

    h_exp = doc.add_paragraph()
    h_exp.add_run("EXPERIENCE").bold = True
    p1 = doc.add_paragraph()
    p1.add_run("Intern · Acme Corp · 2024").bold = True
    doc.add_paragraph("Built a thing for the team", style="List Bullet")
    doc.add_paragraph("Tested another thing", style="List Bullet")

    p2 = doc.add_paragraph()
    p2.add_run("Research Assistant · Cal Photonics Lab · 2023").bold = True
    doc.add_paragraph("Aligned an interferometer", style="List Bullet")

    h_edu = doc.add_paragraph()
    h_edu.add_run("EDUCATION").bold = True
    doc.add_paragraph("University of California, Berkeley — B.S. Electrical Engineering — 2025")

    doc.save(str(HERE / "modern_sans.docx"))


if __name__ == "__main__":
    build_modern_sans()
    print("Built fixtures in", HERE)
```

Run it once:

```bash
python tests/fixtures/build_resumes.py
git add tests/fixtures/resumes/modern_sans.docx tests/fixtures/build_resumes.py
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/pipeline/test_docx_tailor.py
import pytest
from pathlib import Path
from pipeline.docx_tailor import tailor_docx_in_place
from pipeline.tailored_schema import default_v2

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "resumes" / "modern_sans.docx"


def _v2():
    profile = {
        "name": "Jane Doe",
        "top_hard_skills": ["Python", "Verilog", "C++", "MATLAB"],
        "experience": [
            {"title": "Intern", "company": "Acme Corp", "dates": "2024",
             "bullets": ["Built a thing for the team", "Tested another thing"]},
            {"title": "Research Assistant", "company": "Cal Photonics Lab", "dates": "2023",
             "bullets": ["Aligned an interferometer"]},
        ],
        "education": [{"degree": "B.S. Electrical Engineering",
                        "institution": "University of California, Berkeley", "year": "2025"}],
    }
    v2 = default_v2(profile)
    v2["experience"][0]["bullets"][0] = {
        "text": "Built a Verilog testbench for the team",
        "original": "Built a thing for the team",
        "diff": "modified",
    }
    v2["skills"][0]["items"].append({"text": "FPGA verification", "diff": "added"})
    v2["experience"][0]["bullets"].append({
        "text": "Wrote AXI4 transaction generators",
        "original": "",
        "diff": "added",
    })
    return v2


def test_docx_in_place_replaces_modified_bullet(tmp_path):
    out = tailor_docx_in_place(_v2(), source_path=FIXTURE, base="resume", out_dir=tmp_path)
    assert out["docx"] == "resume.docx"
    docx_path = tmp_path / "resume.docx"
    assert docx_path.exists()

    from docx import Document
    doc = Document(str(docx_path))
    texts = [p.text for p in doc.paragraphs]
    assert any("Verilog testbench" in t for t in texts)
    assert any("Aligned an interferometer" in t for t in texts)


def test_docx_in_place_appends_added_bullet(tmp_path):
    tailor_docx_in_place(_v2(), source_path=FIXTURE, base="resume", out_dir=tmp_path)
    from docx import Document
    doc = Document(str(tmp_path / "resume.docx"))
    texts = [p.text for p in doc.paragraphs]
    assert any("AXI4 transaction generators" in t for t in texts)
```

- [ ] **Step 3: Create `pipeline/docx_tailor.py`**

```python
"""
pipeline/docx_tailor.py
───────────────────────
In-place python-docx rewriter. Preserves runs (font, size, color, bold)
on unchanged paragraphs; replaces text and applies green color to runs
of modified/added bullets.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import console
from .template_render import render_html

GREEN = (0x0a, 0x66, 0x2c)


def _is_heading(para) -> str | None:
    """Return the heading word (lowercased) if the paragraph looks like a
    section heading. Heuristic: bold, ALL-CAPS, ≤4 words, no punctuation,
    or a paragraph whose Style starts with 'Heading'."""
    style_name = (para.style.name or "").lower() if para.style else ""
    if style_name.startswith("heading"):
        return para.text.strip().lower() or None
    text = para.text.strip()
    if not text or len(text.split()) > 4 or any(ch in text for ch in ":,;"):
        return None
    if text == text.upper() and any(c.isalpha() for c in text):
        return text.lower()
    if all(r.bold for r in para.runs if r.text.strip()):
        return text.lower()
    return None


_HEADING_BUCKETS = {
    "skills": "skills", "technical skills": "skills", "core competencies": "skills",
    "experience": "experience", "work experience": "experience",
    "professional experience": "experience", "research experience": "experience",
    "education": "education", "projects": "projects", "awards": "awards",
    "certifications": "certifications", "publications": "publications",
    "activities": "activities", "leadership": "leadership", "volunteer": "volunteer",
    "coursework": "coursework", "languages": "languages",
}


def _bucket_for(heading: str) -> str | None:
    return _HEADING_BUCKETS.get(heading.lower())


def _replace_paragraph_text(para, new_text: str, color_green: bool):
    """Replace text content while preserving the leading run's style.
    Adds `color_green` markup to the new run."""
    if not para.runs:
        run = para.add_run(new_text)
    else:
        first = para.runs[0]
        # Wipe other runs
        for r in para.runs[1:]:
            r.text = ""
        first.text = new_text
        run = first
    if color_green:
        from docx.shared import RGBColor
        run.font.color.rgb = RGBColor(*GREEN)


def _clone_bullet_paragraph(template_para, new_text: str, color_green: bool):
    """Clone a List Bullet paragraph by inserting one with the same numbering style."""
    from copy import deepcopy
    new_p = deepcopy(template_para)
    template_para.addnext(new_p)
    # Wipe all text in cloned paragraph
    from docx.oxml.ns import qn
    for r in new_p.findall(qn("w:r")):
        new_p.remove(r)
    # Create wrapping Paragraph and add a new run
    from docx.text.paragraph import Paragraph
    p_obj = Paragraph(new_p, template_para._parent)
    run = p_obj.add_run(new_text)
    if color_green:
        from docx.shared import RGBColor
        run.font.color.rgb = RGBColor(*GREEN)
    return p_obj


def _convert_to_pdf(docx_path: Path, pdf_path: Path) -> bool:
    """Try docx2pdf (Win/macOS) → libreoffice (Linux/Pi) → fail."""
    try:
        from docx2pdf import convert
        convert(str(docx_path), str(pdf_path))
        if pdf_path.exists():
            return True
    except Exception as e:
        console.print(f"  [yellow]docx2pdf unavailable: {type(e).__name__}: {e}[/yellow]")
    libre = shutil.which("libreoffice") or shutil.which("soffice")
    if libre:
        try:
            subprocess.run(
                [libre, "--headless", "--convert-to", "pdf", "--outdir",
                 str(pdf_path.parent), str(docx_path)],
                check=True, timeout=90, capture_output=True,
            )
            produced = pdf_path.parent / (docx_path.stem + ".pdf")
            if produced.exists() and produced != pdf_path:
                shutil.move(str(produced), str(pdf_path))
            return pdf_path.exists()
        except Exception as e:
            console.print(f"  [yellow]LibreOffice convert failed: {e}[/yellow]")
    return False


def tailor_docx_in_place(
    tailored: dict, source_path: Path, base: str, out_dir: Path,
    format_profile: dict | None = None,
) -> dict:
    """Open the source .docx, edit text in-place, save under {base}.docx.

    Always writes:
      {base}.docx           — primary editable artifact
      {base}_preview.html   — HTML preview (template-lib render)
      {base}.pdf            — when docx2pdf or LibreOffice is available
    """
    from docx import Document

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    docx_path = out_dir / (base + ".docx")

    doc = Document(str(source_path))

    # Build a section map: list[(bucket, paragraph_index)]
    section_map: list[tuple[str | None, int]] = []
    current = None
    for i, p in enumerate(doc.paragraphs):
        h = _is_heading(p)
        if h:
            current = _bucket_for(h)
            section_map.append((current, i))
        else:
            section_map.append((current, i))

    # ── Skills replacement ──────────────────────────────────────────────────
    cats = tailored.get("skills") or []
    skill_text_parts: list[tuple[str, bool]] = []
    for cat in cats:
        for it in cat.get("items") or []:
            skill_text_parts.append((it.get("text") or "", (it.get("diff") in ("added", "modified"))))
    skills_replaced = False
    if skill_text_parts:
        for (bucket, idx) in section_map:
            if bucket == "skills":
                p = doc.paragraphs[idx]
                if _is_heading(p):
                    continue
                # First non-heading paragraph in skills bucket
                if not skills_replaced:
                    flat = ", ".join(t for t, _ in skill_text_parts)
                    _replace_paragraph_text(p, flat, color_green=False)
                    skills_replaced = True
                    # Greens added/modified parts: simplest path is to color
                    # the entire first run when ANY skill is added/modified.
                    if any(g for _, g in skill_text_parts):
                        from docx.shared import RGBColor
                        p.runs[0].font.color.rgb = RGBColor(*GREEN)

    # ── Experience bullets ──────────────────────────────────────────────────
    # Walk each role in `tailored.experience`, find the matching role-header
    # paragraph (bold text containing the title or company), then sequentially
    # replace bullet paragraphs that follow until the next non-bullet.
    roles = tailored.get("experience") or []
    role_idx = 0
    p_iter = list(enumerate(doc.paragraphs))
    i = 0
    while i < len(p_iter) and role_idx < len(roles):
        idx, p = p_iter[i]
        text = p.text.strip()
        role = roles[role_idx]
        title = (role.get("title") or "").lower()
        company = (role.get("company") or "").lower()
        is_header = (title and title in text.lower()) or (company and company in text.lower())
        if is_header:
            # Find subsequent List-Bullet paragraphs
            bullets_to_apply = list(role.get("bullets") or [])
            j = i + 1
            template_p = None
            while j < len(p_iter):
                _, pj = p_iter[j]
                if pj.style and "Bullet" in (pj.style.name or ""):
                    template_p = pj
                    if not bullets_to_apply:
                        # Original had more bullets than tailored — clear the leftover
                        _replace_paragraph_text(pj, "", color_green=False)
                    else:
                        b = bullets_to_apply.pop(0)
                        _replace_paragraph_text(pj, b.get("text") or "",
                                                color_green=(b.get("diff") in ("added", "modified")))
                    j += 1
                else:
                    break
            # Append remaining new bullets by cloning the last bullet style
            if bullets_to_apply and template_p is not None:
                anchor = template_p
                for b in bullets_to_apply:
                    anchor = _clone_bullet_paragraph(
                        anchor,
                        b.get("text") or "",
                        color_green=True,
                    )
            i = j
            role_idx += 1
            continue
        i += 1

    # ── Save .docx ──────────────────────────────────────────────────────────
    doc.save(str(docx_path))

    # ── HTML preview ────────────────────────────────────────────────────────
    html = render_html(tailored, "single_column_modern", format_profile=format_profile)
    html_path = out_dir / (base + "_preview.html")
    html_path.write_text(html, encoding="utf-8")

    # ── PDF (best-effort) ───────────────────────────────────────────────────
    pdf_path = out_dir / (base + ".pdf")
    pdf_ok = _convert_to_pdf(docx_path, pdf_path)

    return {
        "tex": None,
        "pdf": pdf_path.name if pdf_ok else None,
        "docx": docx_path.name,
        "html_preview": html_path.name,
        "base": base,
        "template_id": "in_place_docx",
        "template_confidence": 1.0,
    }
```

- [ ] **Step 4: Run tests**

`pytest tests/unit/pipeline/test_docx_tailor.py -v`

- [ ] **Step 5: Commit**

```bash
git add pipeline/docx_tailor.py tests/unit/pipeline/test_docx_tailor.py tests/fixtures/build_resumes.py tests/fixtures/resumes/modern_sans.docx
git commit -m "feat(pipeline): in-place DOCX rewriter via run-level edits"
```

---

## Task 10: phase4_tailor_resume v2 + selected_keywords

**Files:**
- Modify: `pipeline/phases.py` (around line 647-712)

- [ ] **Step 1: Update `phase4_tailor_resume`**

Replace the existing function (lines 647-712) with:

```python
def phase4_tailor_resume(job: dict, profile: dict, resume_text: str,
                          provider: BaseProvider, include_cover_letter: bool = False,
                          section_order: list = None, *,
                          selected_keywords: list[str] | None = None,
                          source_format: str | None = None) -> dict:
    """Produce a TailoredResume v2 dict for *job* against *profile*.

    Pipeline:
      1. Compute heuristic v2 baseline.
      2. Ask provider for v2 (passing user-selected keywords + source_format).
      3. Validate v2 shape.
      4. Retry once on validation failure.
      5. If still bad, fall back to heuristic.
      6. Hybrid merge: keep LLM's good fields, heuristic backfills.
      7. Compute ATS scores before/after, attach.
    """
    from .heuristic_tailor import (
        heuristic_tailor_resume_v2, validate_v2_or_none, merge_with_heuristic_v2,
    )
    heuristic = heuristic_tailor_resume_v2(
        job, profile, resume_text, selected_keywords=selected_keywords,
    )

    raw = None
    try:
        raw = provider.tailor_resume(
            job, profile, resume_text,
            selected_keywords=selected_keywords,
            source_format=source_format,
        ) or {}
    except TypeError:
        # Backward-compat: legacy provider didn't accept kwargs
        try:
            raw = provider.tailor_resume(job, profile, resume_text) or {}
        except Exception as e:
            console.print(f"  [yellow]Tailoring provider error: {e}[/yellow]")
    except Exception as e:
        console.print(f"  [yellow]Tailoring provider error: {e}[/yellow]")

    validated = validate_v2_or_none(raw)
    if validated is None:
        console.print("  [yellow][!] Tailoring response unusable — retrying once.[/yellow]")
        try:
            retry = provider.tailor_resume(
                job, profile, resume_text,
                selected_keywords=selected_keywords, source_format=source_format,
            ) or {}
        except Exception as e:
            console.print(f"  [yellow]Retry failed: {e}[/yellow]")
            retry = {}
        validated = validate_v2_or_none(retry)

    if validated is None:
        console.print("  [yellow][!] Falling back to heuristic v2 tailoring.[/yellow]")
        tailored = heuristic
    else:
        tailored = merge_with_heuristic_v2(validated, heuristic)

    if section_order:
        tailored["section_order"] = section_order
    if include_cover_letter:
        tailored["cover_letter"] = provider.generate_cover_letter(job, profile)

    # ATS scoring (before / after) — text harvested from all bullets + skills
    requirements = job.get("requirements") or []
    before_text = (resume_text or "") + " " + _profile_to_text(profile)
    after_extras: list[str] = []
    for cat in tailored.get("skills") or []:
        for it in cat.get("items") or []:
            after_extras.append(it.get("text") or "")
    for r in tailored.get("experience") or []:
        for b in r.get("bullets") or []:
            after_extras.append(b.get("text") or "")
    after_text = before_text + " " + " ".join(after_extras)
    tailored["ats_score_before"] = _ats_score(before_text, requirements)
    tailored["ats_score_after"] = _ats_score(after_text, requirements)
    return tailored
```

- [ ] **Step 2: Update `tests/unit/pipeline/test_phases_unit.py`** if it asserts on the v1 schema. Search and replace any `skills_reordered` references with the v2 equivalents.

```bash
grep -n "skills_reordered\|experience_bullets" tests/unit/pipeline/test_phases_unit.py
```

For each match, update the assertion (typically: replace `out["skills_reordered"]` → `[it["text"] for cat in out["skills"] for it in cat["items"]]`).

- [ ] **Step 3: Run unit tests**

`pytest tests/unit/pipeline/test_phases_unit.py -v` → green.

- [ ] **Step 4: Commit**

```bash
git add pipeline/phases.py tests/unit/pipeline/test_phases_unit.py
git commit -m "feat(pipeline): phase4 emits TailoredResume v2 + accepts selected_keywords"
```

---

## Task 11: Backend endpoints — `/tailor/analyze`, extended `/tailor`, source_format upload

**Files:**
- Modify: `app.py`
- Test: `tests/integration/test_app_tailor_v2.py`

- [ ] **Step 1: Write the failing tests** — `tests/integration/test_app_tailor_v2.py`

```python
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_analyze_returns_classified_keywords(authed_client_with_profile, fake_job):
    resp = authed_client_with_profile.post(
        "/api/resume/tailor/analyze",
        json={"job_id": fake_job["id"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "must_have" in data and "nice_to_have" in data
    assert isinstance(data["must_have"], list)
    assert "ats_score_current" in data
    assert "estimated_after" in data


def test_tailor_accepts_selected_keywords(authed_client_with_profile, fake_job):
    resp = authed_client_with_profile.post(
        "/api/resume/tailor",
        json={"job_id": fake_job["id"], "selected_keywords": ["FPGA verification"]},
    )
    assert resp.status_code == 200
    item = resp.json()["item"]
    assert item["co"] == fake_job["company"]
    assert "ats_keywords_added" in item or "skills" in item


def test_resume_upload_persists_source_format(authed_client):
    # Upload a .tex source — assert source_format stored on resume record
    files = {"resume": ("resume.tex", b"\\documentclass{article}\n\\begin{document}Test\\end{document}", "application/x-tex")}
    resp = authed_client.post("/api/resume/upload", files=files)
    assert resp.status_code == 200
    state = authed_client.get("/api/state").json()
    rec = next((r for r in state.get("resumes", []) if r.get("primary")), None)
    assert rec is not None
    assert rec["source_format"] == "tex"
```

(Add `authed_client_with_profile` and `fake_job` fixtures to `tests/conftest.py` if not present — they should mirror existing patterns.)

- [ ] **Step 2: Run tests to verify they fail**

`pytest tests/integration/test_app_tailor_v2.py -v` → 404 / KeyError.

- [ ] **Step 3: Add `/api/resume/tailor/analyze` endpoint to `app.py`**

Insert before the existing `@app.post("/api/resume/tailor")` block (around line 3365):

```python
@app.post("/api/resume/tailor/analyze")
async def resume_tailor_analyze(request: Request):
    """Step 1 of the two-step tailoring flow: classify JD keywords as
    must-have / nice-to-have and report which are already present.
    Heuristic-only — no LLM call. Used by TailorDrawer's review step."""
    auth_user = _require_auth_user(request)
    profile = _S.get("profile") or {}
    if not profile:
        raise HTTPException(400, "Upload a resume first.")
    body = await request.json()
    job_id = (body.get("job_id") or "").strip()
    if not job_id:
        raise HTTPException(400, "job_id is required")
    job = _find_job_by_id(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id!r}")

    from pipeline.heuristic_tailor import _missing_jd_keywords, _tokens
    requirements = [str(r).strip() for r in (job.get("requirements") or []) if str(r).strip()]
    skills = [str(s).strip() for s in (profile.get("top_hard_skills") or []) if str(s).strip()]
    resume_text = _S.get("resume_text") or ""

    # Split must-have (top half) vs nice-to-have (bottom half)
    half = max(1, len(requirements) // 2)
    must = requirements[:half]
    nice = requirements[half:]

    haystack = " ".join(skills + [resume_text]).lower()

    def _classify(kws: list[str]) -> list[dict]:
        out: list[dict] = []
        for kw in kws:
            present = kw.lower() in haystack
            out.append({
                "keyword": kw,
                "present": present,
                "suggested_section": "skills" if not present else "experience",
            })
        return out

    missing_count = sum(1 for c in _classify(must) + _classify(nice) if not c["present"])
    estimated = min(100, _ats_score(resume_text + " " + " ".join(skills), requirements) + missing_count * 4)
    current = _ats_score(resume_text + " " + " ".join(skills), requirements)
    return {
        "must_have": _classify(must),
        "nice_to_have": _classify(nice),
        "ats_score_current": current,
        "estimated_after": estimated,
    }
```

You'll also need to add `_ats_score` import; it lives in `pipeline.phases`. If not already imported in `app.py`:

```python
from pipeline.phases import _ats_score
```

- [ ] **Step 4: Extend `/api/resume/tailor` with `selected_keywords` + `source_format` plumbing**

Replace the body of `resume_tailor` (the existing endpoint around line 3365 onward) — find the call to `phase4_tailor_resume` and `_save_tailored_resume` and add:

```python
selected_keywords = list(body.get("selected_keywords") or [])

# Resolve source_format from primary resume record
primary = _get_primary_resume() or {}
source_format = primary.get("source_format")
source_bytes_path = (
    OUTPUT_DIR / "sessions" / _S["session_id"] / primary.get("source_bytes_path", "")
    if primary.get("source_bytes_path") else None
)
if source_format is None and primary.get("original_path"):
    suffix = Path(primary["original_path"]).suffix.lower().lstrip(".")
    source_format = suffix if suffix in ("tex", "docx", "pdf", "txt", "md") else None

# ... existing _make_provider() ...

tailored = phase4_tailor_resume(
    job, profile, _S.get("resume_text", ""), prov,
    include_cover_letter=include_cover,
    selected_keywords=selected_keywords,
    source_format=source_format,
)
resume_files = _save_tailored_resume(
    job, tailored, profile,
    _S.get("latex_source"),
    resume_text=_S.get("resume_text", ""),
    output_dir=_session_output_dir(),
    format_profile=_primary_format_profile(),
    source_format=source_format,
    source_bytes_path=source_bytes_path,
)
```

- [ ] **Step 5: Update resume upload to persist source_format**

In the resume upload endpoint(s) (`POST /api/resume/upload` and `POST /api/resume/text`), after creating the resume record:

```python
suffix = Path(filename).suffix.lower().lstrip(".") if filename else ""
record["source_format"] = suffix if suffix in ("tex", "docx", "pdf", "txt", "md") else "txt"
```

For existing records that lack `source_format`, add a backfill in `_get_primary_resume()`:

```python
def _get_primary_resume() -> dict | None:
    resumes = _S.get("resumes") or []
    if not resumes:
        return None
    primary = next((r for r in resumes if r.get("primary")), resumes[0])
    if "source_format" not in primary and primary.get("original_path"):
        suffix = Path(primary["original_path"]).suffix.lower().lstrip(".")
        primary["source_format"] = suffix if suffix in ("tex", "docx", "pdf", "txt", "md") else "txt"
    return primary
```

- [ ] **Step 6: Update `_build_tailored_item` to expose v2 fields to the SPA**

Locate `_build_tailored_item` (around line 1869). Add v2 fields to the returned dict:

```python
# After the existing dict construction, before return:
if tailored.get("schema_version") == 2:
    item["schema_version"] = 2
    item["v2"] = {
        "skills": tailored.get("skills") or [],
        "experience": tailored.get("experience") or [],
        "projects": tailored.get("projects") or [],
        "education": tailored.get("education") or [],
        "section_order": tailored.get("section_order") or [],
        # generic buckets propagate as-is
        **{k: tailored[k] for k in (
            "summary", "awards", "certifications", "publications",
            "activities", "leadership", "volunteer", "coursework",
            "languages", "custom_sections",
        ) if k in tailored},
        "ats_keywords_added": tailored.get("ats_keywords_added") or [],
    }
    if "html_preview" in (tailored.get("_files") or {}):
        item["html_preview_url"] = "/output/" + tailored["_files"]["html_preview"]
```

You'll also need to thread the file dict (`{tex,pdf,docx,html_preview}`) into `tailored` before serialization so `_build_tailored_item` can find it. The simplest path: in `resume_tailor`, after `_save_tailored_resume`:

```python
tailored["_files"] = resume_files  # carried into _build_tailored_item
```

- [ ] **Step 7: Run tests + smoke**

```bash
pytest tests/integration/test_app_tailor_v2.py -v
python -c "import app"  # imports clean
```

- [ ] **Step 8: Commit**

```bash
git add app.py tests/integration/test_app_tailor_v2.py
git commit -m "feat(api): /tailor/analyze + selected_keywords + source_format plumbing"
```

---

## Task 12: Frontend — TailorDrawer two-step UX + diff CSS + upload-page hint

**Files:**
- Modify: `frontend/app.jsx`, `frontend/index.html`

- [ ] **Step 1: Add CSS rules to `frontend/index.html`**

In the `<style>` block at the bottom of the existing rules (search for the existing `.tr-card` class for context), append:

```css
mark.diff-add { background: rgba(74,222,128,.20); color: var(--good); border-bottom: 1.5px solid var(--good); padding: 0 2px; text-decoration: none; }
mark.diff-mod { background: rgba(74,222,128,.10); color: var(--good); border-bottom: 1.5px dotted var(--good); padding: 0 2px; text-decoration: none; }

.tailor-review { padding: 16px 18px; }
.tailor-review h4 { margin: 0 0 8px; font-size: 13px; color: var(--t2); text-transform: uppercase; letter-spacing: .04em; }
.tailor-review h4.must { color: var(--accent); }
.tailor-review h4.nice { color: var(--t3); }
.tailor-keyword-row { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 8px; cursor: pointer; user-select: none; }
.tailor-keyword-row:hover { background: var(--sur2); }
.tailor-keyword-row input[type="checkbox"] { accent-color: var(--accent); cursor: pointer; }
.tailor-keyword-row .kw-name { flex: 1; font-size: 13px; }
.tailor-keyword-row .kw-meta { font-size: 11px; color: var(--t4); }
.tailor-keyword-row .kw-pill { font-size: 10px; padding: 2px 7px; border-radius: 999px; background: var(--good-d); color: var(--good); border: 1px solid var(--good-b); }
.tailor-review-actions { display: flex; gap: 8px; margin-top: 12px; }
.tailor-review-actions button { flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid var(--bdr); background: var(--surface); color: var(--t1); cursor: pointer; font-weight: 600; }
.tailor-review-actions button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.tailor-review-actions button:hover { background: var(--sur2); }
.tailor-review-actions button.primary:hover { background: var(--accent-h); }

.tailor-preview-iframe { width: 100%; height: 65vh; border: 1px solid var(--bdr); border-radius: 8px; background: #fff; }
.tailor-template-pick { font-size: 11.5px; color: var(--t3); display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.tailor-template-pick select { background: var(--surface); border: 1px solid var(--bdr); color: var(--t1); padding: 3px 6px; border-radius: 6px; font-size: 11.5px; }
```

- [ ] **Step 2: Replace the `TailorDrawer` body (frontend/app.jsx, line 3915-4052)**

Replace the entire `function TailorDrawer(...)` block with the two-step version:

```jsx
function TailorDrawer({ job, mode, isPro, hasResume, onClose }) {
  const jobId = job.id || `${job.co || job.company || ''}|${job.role || job.title || ''}`;
  const [stage, setStage] = useState('analyzing'); // 'analyzing' | 'review' | 'generating' | 'result' | 'error'
  const [analysis, setAnalysis] = useState(null);
  const [selectedKws, setSelectedKws] = useState({}); // map kw → bool
  const [item, setItem] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose?.(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Stage 0: analyze on mount
  useEffect(() => {
    let cancelled = false;
    if (!hasResume) {
      setStage('error');
      setError('Upload a resume first — Atlas needs your profile to tailor against this posting.');
      return () => { cancelled = true; };
    }
    setStage('analyzing');
    setError(null);
    api.post('/api/resume/tailor/analyze', { job_id: jobId }, { timeoutMs: 30000 })
      .then(res => {
        if (cancelled) return;
        setAnalysis(res);
        const initial = {};
        (res.must_have || []).forEach(c => { if (!c.present) initial[c.keyword] = true; });
        (res.nice_to_have || []).forEach(c => { initial[c.keyword] = false; });
        setSelectedKws(initial);
        setStage('review');
      })
      .catch(e => { if (cancelled) return; setError(e?.message || 'Analyze failed.'); setStage('error'); });
    return () => { cancelled = true; };
  }, [jobId, hasResume]);

  const generate = (skipReview = false) => {
    setStage('generating');
    setError(null);
    const selected = skipReview
      ? (analysis?.must_have || []).filter(c => !c.present).map(c => c.keyword)
      : Object.entries(selectedKws).filter(([_, v]) => v).map(([k]) => k);
    api.post('/api/resume/tailor', { job_id: jobId, selected_keywords: selected }, { timeoutMs: 120000 })
      .then(res => { setItem(res?.item || null); setStage('result'); })
      .catch(e => { setError(e?.message || 'Tailoring failed.'); setStage('error'); });
  };

  const co = job.co || job.company || '—';
  const role = job.role || job.title || 'Untitled role';
  const score = Math.round(job.score || 0);
  const provLbl = mode === 'demo' ? 'Demo' : (mode === 'anthropic' ? 'Claude' : 'Ollama');

  return (
    <div className="ask-overlay" onClick={onClose}>
      <aside className="ask-drawer tailor-drawer" onClick={e => e.stopPropagation()}>
        <header className="ask-head">
          <div className="ask-head-l">
            <CompanyLogo company={co} fallbackVariant="v2" size={36}/>
            <div className="ask-head-meta">
              <div className="ask-head-eyebrow">
                <Icon name="wand-2" size={10}/> Tailor · {provLbl}
                {!isPro && mode === 'anthropic' && <span className="ask-pro-pill">Pro</span>}
              </div>
              <div className="ask-head-role">{role}</div>
              <div className="ask-head-co"><span>{co}</span>{score > 0 && <span className="ask-head-score">{score}<i>/100</i></span>}</div>
            </div>
          </div>
          <button className="ask-close" onClick={onClose} title="Close (Esc)">
            <Icon name="x" size={16}/>
          </button>
        </header>

        <div className="tailor-body">
          {(stage === 'analyzing' || stage === 'generating') && (
            <div className="tailor-loading">
              <div className="tailor-loading-eyebrow">
                <span className="spin"/>
                {stage === 'analyzing' ? ' Reading the job description…' : ' Generating tailored resume…'}
              </div>
              <div className="tailor-loading-hint">
                {stage === 'analyzing'
                  ? 'Comparing JD requirements against your resume — finding the keywords you might want to weave in.'
                  : (mode === 'anthropic'
                      ? 'Claude is rewriting bullets and skills with your selected keywords.'
                      : (mode === 'ollama'
                          ? 'Your local model is running.'
                          : 'Demo: deterministic reorder + keyword merge.'))}
              </div>
            </div>
          )}

          {stage === 'error' && (
            <div className="tailor-error">
              <Icon name="alert-triangle" size={14}/>
              <div>
                <div className="tailor-error-h">Couldn't tailor this job</div>
                <div className="tailor-error-msg">{error}</div>
              </div>
            </div>
          )}

          {stage === 'review' && analysis && (
            <div className="tailor-review">
              <h4 className="must">Must-have keywords ({(analysis.must_have || []).length})</h4>
              {(analysis.must_have || []).map((c, i) => (
                <label key={'m' + i} className="tailor-keyword-row">
                  <input type="checkbox" checked={!!selectedKws[c.keyword]}
                         disabled={c.present}
                         onChange={e => setSelectedKws(s => ({ ...s, [c.keyword]: e.target.checked }))}/>
                  <span className="kw-name">{c.keyword}</span>
                  {c.present
                    ? <span className="kw-pill">already on resume</span>
                    : <span className="kw-meta">→ {c.suggested_section}</span>}
                </label>
              ))}
              <h4 className="nice" style={{ marginTop: 14 }}>Nice-to-have ({(analysis.nice_to_have || []).length})</h4>
              {(analysis.nice_to_have || []).map((c, i) => (
                <label key={'n' + i} className="tailor-keyword-row">
                  <input type="checkbox" checked={!!selectedKws[c.keyword]}
                         disabled={c.present}
                         onChange={e => setSelectedKws(s => ({ ...s, [c.keyword]: e.target.checked }))}/>
                  <span className="kw-name">{c.keyword}</span>
                  {c.present
                    ? <span className="kw-pill">already on resume</span>
                    : <span className="kw-meta">→ {c.suggested_section}</span>}
                </label>
              ))}
              <div style={{ marginTop: 14, fontSize: 12, color: 'var(--t3)' }}>
                ATS score: <b style={{ color: 'var(--t1)' }}>{analysis.ats_score_current}</b>
                {' → '} estimated after: <b style={{ color: 'var(--good)' }}>{analysis.estimated_after}</b>
              </div>
              <div className="tailor-review-actions">
                <button onClick={() => generate(true)}>Skip review — generate now</button>
                <button className="primary" onClick={() => generate(false)}>
                  Generate with selected ({Object.values(selectedKws).filter(Boolean).length})
                </button>
              </div>
            </div>
          )}

          {stage === 'result' && item && (
            <div className="tailor-result">
              <TailoredResumePreview item={item} co={co} role={role} job={job} score={score}/>
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

function TailoredResumePreview({ item, co, role, job, score }) {
  // Map the v2 item.html_preview_url directly into an iframe; otherwise
  // fall back to the existing <TailoredResumeCard/> view.
  const previewUrl = item?.html_preview_url || null;
  const [tplOverride, setTplOverride] = useState('');
  // Allow user to switch templates inline (re-issues /tailor with template hint —
  // future work; for v1 we just show the current pick).
  return (
    <>
      {previewUrl && (
        <div style={{ marginBottom: 12 }}>
          <iframe className="tailor-preview-iframe" src={previewUrl} title="Tailored resume preview"/>
          {item.template_id && (
            <div className="tailor-template-pick">
              <span>Template:</span><b style={{ color: 'var(--t1)' }}>{item.template_id.replaceAll('_', ' ')}</b>
              {item.template_confidence != null && (
                <span>· confidence {Math.round(item.template_confidence * 100)}%</span>
              )}
            </div>
          )}
        </div>
      )}
      <TailoredResumeCard item={{
        ...item,
        co: item.co || co, role: item.role || role,
        loc: item.loc || job.loc || job.location || '', score: item.score || score || 0,
      }}/>
    </>
  );
}
```

- [ ] **Step 3: Add the upload-page hint**

Search for the resume upload UI in `frontend/app.jsx` (likely in `Onboarding` or `ResumePage`). Add directly under the file input:

```jsx
<small style={{ display: 'block', marginTop: 6, color: 'var(--t4)', fontSize: 11.5 }}>
  For best format match, upload <b>.tex</b> or <b>.docx</b> if you have them — Atlas
  preserves the original layout exactly. PDF works too: it's matched to the closest
  template in our library.
</small>
```

- [ ] **Step 4: Manually exercise**

```bash
python app.py  # serve at :8000
```

Open `http://localhost:8000/app`, upload a `.tex` or `.docx` resume, click "Tailor for this job" on a discovered listing, walk the two-step flow. Verify:
- Step 1 shows must-have / nice-to-have checkboxes
- Step 2 shows iframe preview with green-highlighted text
- Download link works

- [ ] **Step 5: Commit**

```bash
git add frontend/app.jsx frontend/index.html
git commit -m "feat(ui): TailorDrawer two-step UX with green-highlight preview + upload hint"
```

---

## Task 13: Smoke tests + golden fixtures + final QA

**Files:**
- Create: `tests/unit/pipeline/test_tailor_smoke.py`, golden snapshots under `tests/fixtures/golden/`

- [ ] **Step 1: Write end-to-end smoke for each source format** — `tests/unit/pipeline/test_tailor_smoke.py`

```python
import pytest
from pathlib import Path
from pipeline.tailored_schema import default_v2
from pipeline.heuristic_tailor import heuristic_tailor_resume_v2
from pipeline.resume import _save_tailored_resume

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "resumes"


def _profile_jane():
    return {
        "name": "Jane Doe", "email": "jane@example.com",
        "top_hard_skills": ["Python", "Verilog", "C++", "MATLAB"],
        "experience": [{"title": "Intern", "company": "Acme Corp", "dates": "2024",
                         "bullets": ["Built a thing for the team", "Tested another thing"]}],
        "education": [{"degree": "B.S. EE", "institution": "Cal", "year": "2025"}],
    }


def _job_hw():
    return {"company": "FooCo", "title": "HW Eng",
             "requirements": ["Verilog", "FPGA verification", "AXI4"]}


def test_smoke_tex_in_place(tmp_path):
    src = (FIXTURES / "jake_classic.tex").read_text(encoding="utf-8")
    profile = _profile_jane()
    tailored = heuristic_tailor_resume_v2(_job_hw(), profile, "",
                                            selected_keywords=["FPGA verification"])
    out = _save_tailored_resume(
        _job_hw(), tailored, profile,
        latex_source=src, output_dir=tmp_path,
        source_format="tex",
    )
    assert out["tex"] is not None
    text = (tmp_path / out["tex"]).read_text(encoding="utf-8")
    assert "FPGA verification" in text


def test_smoke_docx_in_place(tmp_path):
    profile = _profile_jane()
    tailored = heuristic_tailor_resume_v2(_job_hw(), profile, "",
                                            selected_keywords=["AXI4"])
    out = _save_tailored_resume(
        _job_hw(), tailored, profile,
        output_dir=tmp_path, source_format="docx",
        source_bytes_path=FIXTURES / "modern_sans.docx",
    )
    assert out["docx"] is not None
    docx_path = tmp_path / out["docx"]
    assert docx_path.exists()


def test_smoke_default_template(tmp_path):
    profile = _profile_jane()
    tailored = heuristic_tailor_resume_v2(_job_hw(), profile, "")
    out = _save_tailored_resume(
        _job_hw(), tailored, profile, output_dir=tmp_path,
        source_format="pdf", format_profile={"columns": 1, "body_font_size": 10},
    )
    assert out["html_preview"] is not None
    assert out["template_id"] in (
        "single_column_classic", "single_column_modern",
        "two_column_left", "two_column_right",
        "compact_tech", "academic_multipage",
    )


def test_smoke_every_section_renders(tmp_path):
    """Cover sections beyond the original four — confirm none are dropped."""
    profile = _profile_jane()
    tailored = heuristic_tailor_resume_v2(_job_hw(), profile, "")
    tailored["awards"] = [{"title": {"text": "Dean's List", "diff": "unchanged"},
                             "detail": {"text": "2024", "diff": "unchanged"}, "bullets": []}]
    tailored["publications"] = [{"title": {"text": "FPGA Verification at Scale", "diff": "unchanged"},
                                   "detail": {"text": "IEEE ICCAD 2024", "diff": "unchanged"},
                                   "bullets": []}]
    tailored["section_order"] = ["Skills", "Experience", "Awards", "Publications", "Education"]
    out = _save_tailored_resume(
        _job_hw(), tailored, profile, output_dir=tmp_path, source_format="pdf",
    )
    html = (tmp_path / out["html_preview"]).read_text(encoding="utf-8")
    assert "Dean's List" in html
    assert "FPGA Verification at Scale" in html
    assert "Awards" in html
    assert "Publications" in html
```

- [ ] **Step 2: Run all unit tests**

```bash
pytest tests/unit/pipeline -v -m unit
```

Expected: every test passes.

- [ ] **Step 3: Run integration tests**

```bash
pytest tests/integration -v -m integration
```

- [ ] **Step 4: Manual end-to-end QA** with the dev server

```bash
python app.py
```

Manual checklist (open `http://localhost:8000/app`):
- [ ] Upload a `.tex` resume → tailor a discovered job → confirm tailored .tex preserves the source's preamble verbatim, only Skills + Experience bullets change, modified bullets show `\textcolor{green!50!black}{...}`.
- [ ] Upload a `.docx` resume → tailor → confirm tailored .docx opens cleanly in Word, modified bullets are green, fonts/styles preserved on unchanged paragraphs.
- [ ] Upload a `.pdf` resume → confirm template matcher picks a sensible template; HTML preview renders green-highlighted text; PDF download works.
- [ ] Step 1 (analyze) shows must-have / nice-to-have keywords with checkboxes.
- [ ] Unchecking all keywords + clicking "Generate" still produces a valid resume (heuristic-only path).
- [ ] Upload page shows the .tex/.docx hint.

- [ ] **Step 5: Commit smoke tests**

```bash
git add tests/unit/pipeline/test_tailor_smoke.py
git commit -m "test(pipeline): smoke tests for tailoring across all source formats"
```

- [ ] **Step 6: Final commit + push**

```bash
git status   # confirm clean
git log --oneline -20   # confirm commit history is sensible
```

---

## Self-Review Checklist (run after the plan is written)

**Spec coverage:**
- §5 Data model — Task 1 ✓
- §6.1 schema module — Task 1 ✓
- §6.2 providers v2 — Task 3 ✓
- §6.2 heuristic v2 — Task 2 ✓
- §6.3 phase4 v2 — Task 10 ✓
- §6.4 _save_tailored_resume dispatch — Task 7 ✓
- §6.5 LaTeX in-place — Task 8 ✓
- §6.6 DOCX in-place — Task 9 ✓
- §6.7 Resume upload metadata — Task 11 ✓
- §6.8 Template library — Task 4 ✓
- §6.9 Template matcher — Task 6 ✓
- §6.10 Renderer — Task 5 ✓
- §6.11 /tailor/analyze — Task 11 ✓
- §6.12 /tailor extended — Task 11 ✓
- §7.1 TailorDrawer states — Task 12 ✓
- §7.2 TailoredResumeCard preview — Task 12 ✓
- §7.3 CSS rules — Task 12 ✓
- §7.4 Upload-page hint — Task 12 ✓
- §9 Tests — embedded in Tasks 1-13 ✓

**Type consistency:**
- `validate_v2` (Task 1) consumed by `validate_v2_or_none` (Task 2) ✓
- `default_v2` (Task 1) used in Tasks 2, 3, 5, 7 ✓
- `legacy_to_v2` (Task 1) used in Task 7 ✓
- `tailor_latex_in_place` (Task 8) called from Task 7 ✓
- `tailor_docx_in_place` (Task 9) called from Task 7 ✓
- `pick_template` + `render_html` + `render_pdf` (Tasks 5, 6) called from Task 7 ✓

**Placeholder scan:** No "TBD"/"TODO" in steps. Each step shows complete code.

---

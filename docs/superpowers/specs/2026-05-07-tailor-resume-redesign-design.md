# Tailor Resume — End-to-End Redesign

**Status:** approved (pending spec sign-off)
**Owner:** main
**Date:** 2026-05-07

---

## 1. Problem

The current Phase 4 / `POST /api/resume/tailor` flow produces tailored PDFs that are nearly empty: a generic four-section template, missing every section beyond Skills/Experience/Projects/Education, with no real format match to the user's source resume. There's also no keyword-review step — the user has no agency over which JD keywords get woven in.

Two independent problems are conflated in the user-visible failure:

1. **Schema collapse.** `pipeline.providers.*.tailor_resume` returns only `skills_reordered` + `experience_bullets`. The renderer (`pipeline.resume._render_resume_latex` / `_render_resume_pdf_reportlab`) only iterates Skills, Experience, Projects, Education. Awards, Publications, Certifications, Coursework, Activities, Leadership, Volunteer, Languages, Summary — all silently dropped. *This alone makes tailored resumes look "blank."*
2. **Format drop.** Even if the schema were complete, the rebuilt PDF goes through a single hard-coded reportlab template with mild visual hints from `pdf_format.detect_format_profile`. There's no path that preserves the user's actual layout, even when the source is editable.

There is also no keyword-checkbox step and no green-highlight diff anywhere.

## 2. Goals

1. The tailored output contains every section the source had — including Awards, Publications, Certifications, Coursework, Activities, Leadership, Volunteer, Languages, custom user-defined sections.
2. Visual layout matches the user's source:
   - Source `.tex` → in-place LaTeX rewrite (true preserve)
   - Source `.docx` → in-place python-docx rewrite (true preserve)
   - Source `.pdf` → closest match from a 6-template HTML/CSS library, parameterized by the existing `format_profile` (columns, fonts, accent)
   - Source `.txt` / `.md` → default template
3. Two-step UX:
   - **Step 1**: user reviews missing JD keywords with checkboxes; chooses which to include
   - **Step 2**: tailored resume preview with **green highlights** on every changed/added text run; downloadable as PDF (and `.tex` / `.docx` when applicable)
4. Anti-fabrication invariants are preserved (no inventing dates / titles / companies / degrees).

## 3. Non-goals

- Pixel-perfect reconstruction of arbitrary PDFs. Without source font files this isn't reliably possible. We aim for visually-equivalent — same columns, fonts within practical limits, accent color, heading style, section ordering.
- Real-time editing in the preview ("Edit With AI" button on Jobright). Out of scope; user re-runs tailoring with different keyword choices to iterate.
- Replacing Phase 4's batch flow. Per-job `POST /api/resume/tailor` is the primary path; batch reuses the same renderer.
- Backwards-incompatible changes to the legacy schema. Old `tailored_map` entries continue rendering via an adapter.

## 4. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│  TailorDrawer (frontend, app.jsx)                              │
│  ┌──────────────┐    ┌────────────────┐    ┌─────────────────┐ │
│  │ Step 0: open │ →  │ Step 1: review │ →  │ Step 2: preview │ │
│  │  + analyze   │    │  keywords      │    │  + download     │ │
│  └──────────────┘    └────────────────┘    └─────────────────┘ │
│         ↓                    ↓                      ↓          │
└─────────│────────────────────│──────────────────────│──────────┘
          │                    │                      │
          ▼                    ▼                      ▼
   POST /api/resume/    POST /api/resume/      GET /output/{file}
   tailor/analyze       tailor                  (PDF / .tex / .docx)
          │                    │
          ▼                    ▼
   ┌──────────────┐     ┌──────────────────────┐
   │ JD keyword   │     │ phase4_tailor_resume │
   │ extraction + │     │ (extended)           │
   │ classify     │     └──────────────────────┘
   │ present/miss │              │
   └──────────────┘              ▼
                          ┌─────────────────────────────┐
                          │ Provider.tailor_resume      │
                          │ (new comprehensive schema)  │
                          └─────────────────────────────┘
                                       │
                                       ▼
                  ┌────────────────────────────────────────────┐
                  │ _save_tailored_resume (extended)           │
                  │   ├─ src=tex  → tailor_latex_in_place      │
                  │   ├─ src=docx → tailor_docx_in_place       │
                  │   ├─ src=pdf  → render_via_template_lib    │
                  │   └─ default  → render_via_template_lib    │
                  │                  (default template)        │
                  └────────────────────────────────────────────┘
```

## 5. Data model — `TailoredResume`

The provider returns a single `TailoredResume` dict. Each text node carries diff metadata so every renderer can paint green highlights uniformly.

```python
# pipeline/tailored_schema.py  (new module)

DiffMarker = Literal["unchanged", "modified", "added"]

class TextNode(TypedDict, total=False):
    text: str               # final text after tailoring
    original: str           # source text (or "" if added)
    diff: DiffMarker        # "unchanged" | "modified" | "added"

class SkillCategory(TypedDict, total=False):
    name: str               # e.g. "Programming", "Lab Skills" — "" for flat list
    items: list[TextNode]   # each skill is a TextNode

class Bullet(TypedDict):
    node: TextNode

class Role(TypedDict, total=False):
    title: str              # never modified by LLM
    company: str            # never modified by LLM
    location: str
    dates: str              # never modified by LLM
    bullets: list[TextNode]

class EducationEntry(TypedDict, total=False):
    institution: str
    degree: str
    dates: str
    location: str
    gpa: str
    notes: list[TextNode]   # honors, coursework lines, etc.

class ProjectEntry(TypedDict, total=False):
    name: str
    description: TextNode
    skills_used: list[TextNode]
    bullets: list[TextNode]
    dates: str
    url: str

class GenericEntry(TypedDict, total=False):
    """Used for awards, certifications, publications, activities, etc."""
    title: TextNode
    detail: TextNode        # date / venue / issuer / description
    bullets: list[TextNode]

class CustomSection(TypedDict):
    """Anything else the user's resume had that doesn't fit the named buckets."""
    name: str               # heading verbatim from source
    items: list[GenericEntry]

class TailoredResume(TypedDict, total=False):
    # Header (never modified)
    name: str
    email: str
    phone: str
    linkedin: str
    github: str
    location: str
    website: str

    # Optional summary (modifiable; LLM may add a 1-2 sentence summary)
    summary: TextNode

    # Skills section (categories optional — empty name = flat list)
    skills: list[SkillCategory]

    # Core sections — each entry is structurally pinned, only TextNodes change
    experience: list[Role]
    projects: list[ProjectEntry]
    education: list[EducationEntry]

    # Named extra sections — emitted only when source had them
    awards: list[GenericEntry]
    certifications: list[GenericEntry]
    publications: list[GenericEntry]
    activities: list[GenericEntry]
    leadership: list[GenericEntry]
    volunteer: list[GenericEntry]
    coursework: list[GenericEntry]
    languages: list[GenericEntry]

    # Catch-all for unrecognized headings — preserves user-defined sections
    custom_sections: list[CustomSection]

    # Section render order — derived from source resume
    section_order: list[str]

    # ATS metadata
    ats_keywords_added: list[str]    # keywords woven into bullets/skills
    ats_keywords_missing: list[str]  # keywords NOT woven in (user unchecked)
    ats_score_before: int
    ats_score_after: int

    # Schema version — bump when shape changes
    schema_version: int              # 2 (legacy was implicit v1)
```

### Diff semantics
- A `TextNode` with `diff="unchanged"` renders as plain text.
- `diff="modified"` renders with green underline + `original` available on hover (HTML) or as a comment in the .tex/.docx source.
- `diff="added"` renders with green background fill (stronger emphasis than modified).
- Top-level `name`, `dates`, `company`, `institution`, `degree`, `gpa` etc. NEVER carry diff markers — they are always identical to source.

## 6. Backend changes

### 6.1 New module: `pipeline/tailored_schema.py`
- TypedDicts above.
- `legacy_to_v2(old_dict, profile) → TailoredResume`: adapter so existing `tailored_map` entries keep rendering.
- `validate_v2(d) → TailoredResume | None`: structural validator (mirrors `heuristic_tailor.validate_tailoring` for the new shape).
- `default_v2(profile) → TailoredResume`: empty-but-valid skeleton built from a profile (used by the heuristic safety net).

### 6.2 Extended providers (`pipeline/providers.py`)
All three providers (`AnthropicProvider`, `OllamaProvider`, `DemoProvider`) get:

```python
def tailor_resume(self, job, profile, resume_text, *,
                  selected_keywords: list[str] | None = None,
                  source_format: str | None = None) -> TailoredResume: ...
```

#### Tool schema (Anthropic)
The Anthropic tool schema becomes the v2 shape directly. Critical: Claude's tool-use API supports nested objects, so we declare the whole thing as one tool input. Token budget: ~6 KB output for a typical resume; bump `max_tokens` to 8192.

#### Prompt redesign
```
You are tailoring a resume for {job.title} at {job.company}.

INPUT — full structured profile (preserve every field; only modify TextNodes):
{json.dumps(profile_as_v2_skeleton)}

JD requirements: {job.requirements}
JD description: {job.description}

USER-SELECTED KEYWORDS to weave in (REQUIRED): {selected_keywords}
USER-DECLINED keywords (do NOT include): {declined_keywords}

If selected_keywords is empty, default to "all must-have JD keywords that aren't already
on the resume" — same behavior as today's silent tailoring.

RULES:
1. NEVER fabricate. NEVER change titles/companies/dates/institutions/degrees/GPA.
2. For each of the user-selected keywords:
   - If it fits an existing bullet, REPHRASE that bullet (set diff="modified").
   - If no bullet fits, ADD a new bullet to the most relevant role (set diff="added").
   - Skills section: add to the most relevant category (set diff="added").
3. Reorder bullets within each role by JD relevance (no diff change for reorder alone).
4. Keep every section present in the source.
5. Set diff="unchanged" on all unmodified TextNodes.

Return the full TailoredResume v2 — every section, every bullet.
```

#### Heuristic fallback
`pipeline/heuristic_tailor.py` gains:
- `heuristic_tailor_resume_v2(job, profile, resume_text, selected_keywords)` — produces a v2 TailoredResume with no fabrication. Modifies bullets only by reordering (so `diff="unchanged"` everywhere); adds requested keywords to Skills as `diff="added"` only when the user explicitly selected them.
- `validate_v2_or_none(raw)` — structural check.
- `merge_with_heuristic_v2(llm, heuristic)` — same hybrid behavior as today.

### 6.3 Extended `phase4_tailor_resume`
```python
def phase4_tailor_resume(job, profile, resume_text, provider, *,
                         include_cover_letter=False, section_order=None,
                         selected_keywords=None) -> TailoredResume:
```
Behavior unchanged structurally; passes `selected_keywords` to the provider; produces v2 dicts.

### 6.4 Extended `_save_tailored_resume`
Dispatches by source format:

```python
def _save_tailored_resume(job, tailored, profile=None, *,
                          source_format: str = None,    # "tex" | "docx" | "pdf" | "txt"
                          source_bytes: bytes = None,    # original file when in-place
                          latex_source: str = None,
                          format_profile: dict = None,
                          ...) -> dict:
    if source_format == "tex" and latex_source:
        return _tailor_latex_in_place(job, tailored, latex_source, ...)
    if source_format == "docx" and source_bytes:
        return _tailor_docx_in_place(job, tailored, source_bytes, ...)
    return _render_via_template_lib(job, tailored, profile, format_profile, ...)
```

`source_format` and `source_bytes` come from the resume record (added at upload time — see §6.7). Existing call sites that don't pass them keep working (fall through to template lib).

### 6.5 New module: `pipeline/latex_tailor.py`
`_tailor_latex_in_place(latex_source, tailored)` extends today's `apply_tailoring_to_latex`:

- Replace the Skills section content (already done).
- Walk `\section{...}` blocks; for each Experience/Projects entry, locate `\item{...}` / `\resumeItem{...}` / `\cventry` blocks and replace bullet text using a deterministic anchor (the original bullet's first 8 words). When `diff != "unchanged"`, wrap the new text in `\textcolor{green!50!black}{...}` (auto-add `\usepackage{xcolor}` if missing).
- Append entirely new bullets at the end of their role's `itemize`/`resumeItemList` block.
- For sections present in `tailored` but absent in source: append a new `\section{...}` at the appropriate point per `section_order`.
- Sanitize: re-run `_sanitize_latex_source` before compile.
- Output paths produced: `{base}.tex` (always), `{base}.pdf` (when pdflatex is on PATH), `{base}_preview.html` (template-lib rendering for the in-page preview). The .tex is the primary editable artifact.

### 6.6 New module: `pipeline/docx_tailor.py`
`_tailor_docx_in_place(source_bytes, tailored)` opens via `docx.Document(BytesIO(source_bytes))`:

- Build a section map: walk paragraphs, detect headings (Heading 1/2 styles, or all-caps short lines), bucket subsequent paragraphs under each heading.
- For each Experience/Projects/Education entry the LLM modified, locate the corresponding paragraphs by date+title prefix matching, then replace text at the **run** level (`paragraph.runs[i].text = ...`) to preserve fonts/colors/sizes.
- For added bullets: clone the last bullet paragraph (preserves numPr/style), set its text, color the new run's font green (`RGBColor(0x0a, 0x66, 0x2c)`).
- For modified bullets: replace text in-run, color modified runs green.
- For Skills: locate the Skills paragraph, replace its text content while preserving the run's font/style. Added skills go in green.
- Output paths produced (when conversion succeeds): `{base}.docx` (always), `{base}.pdf` (when convertible), `{base}_preview.html` (rendered via the same template lib as a viewable preview — the in-place .docx is the *primary* artifact, the HTML is only for the in-page green-highlight preview).
- DOCX → PDF conversion order: `docx2pdf` (Windows / macOS, in-process) → LibreOffice headless (`libreoffice --headless --convert-to pdf`, Linux/Pi) → fail. On failure the user gets the `.docx` plus the HTML preview; UI shows a "Install LibreOffice for PDF download" hint.

### 6.7 Resume upload metadata (`app.py`)
At upload time, persist the source format and original bytes path on the resume record:

```python
record["source_format"] = "tex" | "docx" | "pdf" | "txt" | "md"
record["source_bytes_path"] = "uploads/{id}{suffix}"   # already stored
```

Both are read by `_save_tailored_resume` via `_S["resumes"][primary]`. No DB migration — these live in `session_state`'s JSON blob.

**Backfill for existing resume records:** when `source_format` is missing, infer from `original_path` suffix (`.tex`, `.docx`, `.pdf`, `.txt`, `.md`). The original bytes are already on disk under `uploads/{id}{suffix}` — no re-upload needed.

### 6.8 Template library — `pipeline/templates/`
6 Jinja2 + CSS template pairs:

```
pipeline/templates/
├── single_column_classic.html.j2   # Jake's Resume / academic CV (serif, rules under headings)
├── single_column_modern.html.j2    # sans-serif, accent-colored headings
├── two_column_left.html.j2         # left sidebar (skills/contact), right body
├── two_column_right.html.j2        # right sidebar
├── compact_tech.html.j2            # dense, monospace section headers
├── academic_multipage.html.j2      # publications-heavy, long-form
└── _shared/
    ├── base.css                    # variables, reset, diff-highlight rules
    └── diff.css                    # green underline / fill styles
```

Each template:
- Iterates `tailored.section_order` to render sections in source order.
- Renders every section type (skills, experience, projects, education, awards, certifications, publications, activities, leadership, volunteer, coursework, languages, custom_sections).
- Reads `--accent`, `--body-size`, `--header-size` from `format_profile`.
- Wraps every TextNode: `{% if node.diff == "modified" %}<mark class="diff-mod">{{ node.text }}</mark>{% elif node.diff == "added" %}<mark class="diff-add">{{ node.text }}</mark>{% else %}{{ node.text }}{% endif %}`.

### 6.9 Template matcher — `pipeline/template_match.py`
```python
def pick_template(format_profile: dict, resume_text: str) -> tuple[str, float]:
    """Returns (template_id, confidence in [0,1])."""
```
Scoring features:
- `columns` exact match: ±0.4
- `body_font_size` band (≤10 = "compact", 10-12 = "standard", >12 = "loose"): ±0.2
- `accent_color` presence: hints modern vs classic: ±0.15
- Section heading style detected from `resume_text` regex (rules-under-heading, ALL-CAPS, accent-colored): ±0.2
- Publications / academic markers (count of "doi:", "et al.", years): ±0.15

Confidence below 0.5 → frontend shows "best-effort match" badge with a "Try a different template" dropdown.

### 6.10 Renderer — `pipeline/template_render.py`
```python
def render_html(tailored: TailoredResume, template_id: str, format_profile: dict) -> str: ...
def render_pdf(html: str, output_path: Path, *, prefer_engine: str = "weasyprint") -> bool: ...
```
PDF engines tried in order: WeasyPrint → reportlab-from-html (RML) → fail.

WeasyPrint is added to `requirements.txt`. Install docs added to AGENTS.md noting Windows GTK requirements. If WeasyPrint import fails at runtime, we log a clear message and fall back to a reportlab-based renderer that approximates each template (lower fidelity but always works).

### 6.11 New endpoint: `POST /api/resume/tailor/analyze`
Body:
```json
{ "job_id": "..." }
```
Returns:
```json
{
  "must_have": [
    {"keyword": "FPGA design", "present": false, "suggested_section": "experience"},
    {"keyword": "Verilog", "present": true}
  ],
  "nice_to_have": [
    {"keyword": "AXI4", "present": false, "suggested_section": "skills"}
  ],
  "ats_score_current": 67,
  "estimated_after": 84
}
```
Implementation: reuse `pipeline.providers.compute_skill_coverage` + `pipeline.heuristic_tailor._missing_jd_keywords`; classify must-have vs nice-to-have by `job.requirements` order (top half = must-have, bottom half = nice-to-have).

### 6.12 Extended endpoint: `POST /api/resume/tailor`
Body adds an optional field:
```json
{ "job_id": "...", "selected_keywords": ["FPGA design", "AXI4"] }
```
When omitted, the server defaults to "all must-haves selected, nice-to-haves not selected" — preserves single-step UX for callers (CLI, batch phase 4) that don't run analyze first.

## 7. Frontend changes (`frontend/app.jsx`)

### 7.1 `TailorDrawer` — three states
```
state ∈ { "analyzing" | "review" | "generating" | "result" | "error" }
```
- **analyzing** (initial): hits `/tailor/analyze`. Shows the existing checklist animation.
- **review**: shows missing keywords as checkboxes grouped by `must_have` / `nice_to_have`, with a "Generate" button. Must-have boxes default checked; nice-to-have unchecked. There is also a "Skip review — generate now" button that uses defaults.
- **generating**: hits `/tailor` with selected keywords. Same checklist animation.
- **result**: shows preview + downloads (existing `TailoredResumeCard`, extended).

CLI / batch phase 4 callers don't go through TailorDrawer; they call `/tailor` (or `phase4_tailor_resume` directly) without `selected_keywords`, which uses the same default-must-haves behavior.

State persists in component state only — no Redux/server persistence beyond what's already in `tailored_map`.

### 7.2 `TailoredResumeCard` — full preview
Today the card shows skills chips, role bullets, and ATS score. Extended:
- New "Preview" panel rendering the tailored HTML inline (sandboxed iframe pointing at `/output/sessions/{sid}/{base}_preview.html`). The preview HTML IS the WeasyPrint input — what you see is what downloads.
- Green highlights are visible in the preview (CSS classes already in template).
- Existing skill-comparison + bullet sections remain as supporting context below the preview.
- New "Choose template" dropdown when source is PDF (shows the matcher's confidence; lets user pick a different template).

### 7.3 New CSS rules in `frontend/index.html`
```css
mark.diff-add { background: rgba(74,222,128,.22); color: var(--good); border-bottom: 1.5px solid var(--good); padding: 0 2px; }
mark.diff-mod { background: rgba(74,222,128,.10); color: var(--good); border-bottom: 1.5px dotted var(--good); padding: 0 2px; }
.tailor-keyword-row { display: flex; align-items: center; gap: 10px; padding: 8px; border-radius: 8px; }
.tailor-keyword-row:hover { background: var(--sur2); }
.tailor-keyword-row input[type="checkbox"] { accent-color: var(--accent); }
.tailor-must-have { color: var(--accent); }
.tailor-nice-have { color: var(--t3); }
.tailor-template-pick { font-size: 11.5px; color: var(--t3); }
```

### 7.4 Resume upload page hint
In the file-picker block of `Onboarding` / `ResumePage`:
```
For best format match, upload .tex or .docx if you have them.
PDF works too — Atlas will pick the closest matching template.
```
One-line `<small>` styled as `var(--t4)`.

## 8. Backwards compatibility

- `tailored_map` entries with the legacy schema render via `legacy_to_v2()` adapter on the fly.
- Anything that calls `_save_tailored_resume` without `source_format` falls through to the template lib using `default_v2` → unchanged behavior at the worst case.
- `_build_tailored_item` (in `app.py`) already accepts loose dicts; it gets a v2-aware branch.
- Existing `apply_tailoring_to_latex` is *not* removed — `_tailor_latex_in_place` calls it for the Skills replacement and adds the bullet logic. Old callers stay valid.

## 9. Testing

### 9.1 Fixtures (`tests/fixtures/resumes/`)
- `jake_classic.tex` — single-column LaTeX
- `modern_sans.docx` — sans-serif DOCX with two columns
- `academic.pdf` — multi-page academic CV with publications
- `compact_tech.pdf` — dense tech resume with sidebar
- `plain.txt` — plain text fallback

### 9.2 Unit tests (`tests/test_tailor_schema.py`, `tests/test_template_match.py`, `tests/test_docx_tailor.py`, `tests/test_latex_tailor.py`)
- Schema: legacy_to_v2 round-trip preserves data; validate_v2 rejects malformed inputs.
- Template matcher: each fixture maps to its expected template_id.
- DOCX in-place: a 5-bullet experience role can have 1 modified, 1 added, 3 unchanged → output preserves all original styling on unchanged runs, greens the modified/added runs, and the produced document opens cleanly in Word.
- LaTeX in-place: skill swap + bullet swap roundtrip compiles via pdflatex (when available; skip otherwise).

### 9.3 Smoke (`tests/test_tailor_smoke.py`)
End-to-end with DemoProvider for each fixture format → assert tailored output contains every section name from the source.

### 9.4 Visual snapshot
After implementation, render each template with a synthetic profile + commit the resulting PDFs to `tests/fixtures/golden/` so future regressions are caught by `pdftotext` diff.

## 10. Implementation sequence

In this order; each item is a self-contained PR-sized chunk:

1. **Schema + adapter** — `pipeline/tailored_schema.py`, legacy adapter, validator, default factory. Unit tests.
2. **Heuristic v2** — `heuristic_tailor.py` adds v2 path + merge. Unit tests.
3. **Provider tool schemas** — Anthropic / Ollama / Demo updated to v2. Smoke each provider with a fixture.
4. **Template library v0** — `pipeline/templates/`, `template_match.py`, `template_render.py`. WeasyPrint + reportlab fallback. Visual snapshot of all 6 templates with synthetic profile.
5. **`_save_tailored_resume` dispatch** — wire source_format → renderer. Default falls through to template lib.
6. **LaTeX in-place rewriter** — `pipeline/latex_tailor.py`. Unit tests with a fixture.
7. **DOCX in-place rewriter** — `pipeline/docx_tailor.py`. Unit tests with a fixture.
8. **`/tailor/analyze` endpoint** — backend. Unit tests.
9. **Frontend two-step UX** — `TailorDrawer` rewrite, `TailoredResumeCard` preview panel, upload-page hint. Manual exercise with the dev server.
10. **End-to-end smoke + golden fixtures** — every source format through every provider.

## 11. Risks & mitigations

- **WeasyPrint Windows install.** Mitigation: reportlab fallback per template; clear error in UI when neither works.
- **DOCX→PDF conversion path.** `docx2pdf` requires Word/LibreOffice. Mitigation: when no converter is present, ship the .docx alone and tell the user; the in-page HTML preview still works.
- **LLM latency** with v2 schema (richer output). Mitigation: bump `max_tokens` to 8192 for Anthropic; on Ollama, prefer larger models in the dev config; the existing 90-s timeout in TailorDrawer covers it.
- **Template matcher confidence** — wrong template feels worse than current generic. Mitigation: confidence < 0.5 surfaces a banner + dropdown to switch templates.
- **Schema bloat with `custom_sections`.** Mitigation: cap to 3 custom sections; warn in the UI if more were detected.

## 12. Open decisions

| # | Decision | Default |
|---|----------|---------|
| 1 | WeasyPrint as a hard dependency? | YES — install with `pip install weasyprint`. Falls back to reportlab on import failure. |
| 2 | Number of templates in v0 library? | 6 (listed in §6.8). |
| 3 | Where does keyword review live? | Inline expansion within `TailorDrawer` (no separate modal). |
| 4 | docx-to-pdf path? | `docx2pdf` if available, else LibreOffice headless, else .docx-only. |

Defaults proceed unless flagged before implementation.

---

**Approvals**
- Design: ✅ approved 2026-05-07
- Spec: ⏳ awaiting user review

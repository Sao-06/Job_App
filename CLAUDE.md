# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

# Project Context

This is an AI agent workspace for resume/CV generation, AI-initiated job applications, and tracking application results. The core deliverable is `agent.py` — a 7-phase autonomous job application pipeline.

---

# Running the Agent

**Setup** (one-time):
```bash
pip install -r requirements.txt
```

**Run modes:**
```bash
python agent.py                              # Anthropic Claude (requires ANTHROPIC_API_KEY env var)
python agent.py --demo                       # No API key — uses regex/template logic + hardcoded demo jobs
python agent.py --ollama                     # Local Ollama LLM (requires Ollama running at localhost:11434)
python agent.py --ollama --model mistral     # Choose specific Ollama model
```

**Before running** (Anthropic mode):
```bash
export ANTHROPIC_API_KEY=sk-...   # bash
# or
set ANTHROPIC_API_KEY=sk-...      # Windows CMD
```

---

# Architecture

`agent.py` is a single-file pipeline with three pluggable LLM backends and seven sequential phase functions.

**Provider abstraction** (`BaseProvider` → three concrete implementations):
- `AnthropicProvider` — calls `claude-opus-4-6` via the Anthropic SDK; uses structured tool_use calls for JSON output; enables `thinking` mode for resume parsing and tailoring
- `DemoProvider` — pure Python regex/keyword matching; zero cost, fully offline; uses `DEMO_JOBS` hardcoded list (8 semiconductor/EE internship postings)
- `OllamaProvider` — calls any local Ollama model via the OpenAI-compatible `/v1` endpoint; uses `openai` package (optional dependency)

**Phase functions** (called sequentially in `main()`):
| Function | Phase | What it does |
|---|---|---|
| `phase1_ingest_resume()` | 1 | Parses resume text → structured profile dict |
| `phase2_discover_jobs()` | 2 | Loads `resources/sample_jobs.json` or generates via provider |
| `phase3_score_jobs()` | 3 | Scores each job 0–100 using weighted model; filters below `min_score` |
| `phase4_tailor_resume()` | 4 | Returns tailored resume sections + optional cover letter per job |
| `phase5_simulate_submission()` | 5 | Demo-mode only; simulates application submission |
| `phase6_update_tracker()` | 6 | Writes `output/Job_Applications_Tracker_YYYY-MM.xlsx` via openpyxl |
| `phase7` (inline) | 7 | Generates and prints end-of-run report |

**Scoring weights** (Phase 3):
- Skills match: 30% | Title alignment: 25% | Experience: 15% | Education: 10% | Industry: 10% | Location: 10%

**Key config constants** at top of `agent.py` (update before running):
- `OWNER_NAME` — used in cover letters and resume filenames
- `OUTPUT_DIR` — defaults to `output/`
- `RESOURCES_DIR` — defaults to `resources/`

**Job data flow**: Phase 2 first checks `resources/sample_jobs.json`; if absent, asks the provider to generate jobs and saves them there for reuse.

---

# Agent Behavior Rules

- Clarify: Always ask clarifying questions before starting a complex task.
- Plan-first: Show a short plan and steps before executing any multi-step task.
- Concise outputs: Prefer bullet-point summaries over long paragraphs.
- Save outputs: Save all final deliverables to the `output/` folder.
- Citations: Cite sources when doing research (include links and brief notes).
- Permissions: Ask before accessing or modifying private accounts or external services.
- Include Next Steps suggestion whenever beneficial.

### Job Application Agent Configuration
- Auto-apply threshold: match score ≥ 75 (brief pause for user approval before submitting)
- Manual review: score 60–74
- Skip: score < 60
- Default job search date range: last 14 days
- Max automated applications per run: 20
- Tracker filename: `Job_Applications_Tracker_YYYY-MM.xlsx` (saved to `output/`)

---

# Output Conventions

- Folder: `output/`
- Filename pattern: `YYYYMMDD_task_short-description.ext` (e.g., `20260328_resume_CompanyX.pdf`)
- Tailored resumes: `[UserName]_Resume_[CompanyName]_[JobTitle].pdf` (append `_v2` etc. for duplicates)
- Cover letters: `[UserName]_CoverLetter_[CompanyName].pdf`
- Tracker: `Job_Applications_Tracker_YYYY-MM.xlsx`
- Drafts: Markdown; final resumes: PDF; data artifacts in named subfolders.

---

# Workflow Reference

- `Workflow/job-application-agent.md` — canonical master prompt spec (7 phases, scoring rubric, tracker schema, startup checklist). Read this before modifying phase logic in `agent.py`.

---

# About Me

<!-- TODO: Replace this section with a brief description of yourself -->
I am a [undergraduate/graduate] student at [University Name]. As of [Semester Year] I am a [year] studying [Major]. My interests: [interest 1], [interest 2], [interest 3]. I am building [field] projects to improve my internship/job candidacy.

# Goals

<!-- TODO: Update with your own goals -->
- Short-term: [e.g., build 2–3 small projects; prepare targeted resumes and applications]
- Medium-term: [e.g., secure internship offers in X or Y field]
- Long-term: [e.g., graduate with a portfolio of projects and research experience]

# Fillable Template ← fill in your details here

- Full name: [YOUR FULL NAME]
- LinkedIn URL: [https://www.linkedin.com/in/your-profile]
- Current university and major: [University Name — Major]
- Current year: [freshman / sophomore / junior / senior / graduate], [Semester Year]
- Top 3 technical interests: [Interest 1], [Interest 2], [Interest 3]
- Key skills & tools:
    - [Skill 1], [Skill 2], [Skill 3]
    - [Tool 1], [Tool 2]
- Primary internship/job targets:
    - Primary: [Role type — Company 1, Company 2, Company 3]
    - Secondary: [Role type — Company 4, Company 5]
- Job Boards: LinkedIn, Indeed, Glassdoor, ZipRecruiter, Wellfound, Dice, Handshake (add/remove as needed)
- Preferred resume format: [1 page / 2 page]
- Any sensitive data or privacy constraints: [e.g., do not mention X unless required]
- Permissions: Agent may create/commit files: [yes / no]

# Environment & Preferences

- OS: [Windows / macOS / Linux]
- Timezone: [Your timezone]
- Tone: professional, concise, and helpful.
- Languages & tools: [list your languages and tools, e.g., Python, MATLAB, C++]

# Contact & Ownership
- Owner: [YOUR FULL NAME]
- Preferred contact: [your.email@example.com]

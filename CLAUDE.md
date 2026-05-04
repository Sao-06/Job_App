# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

# Project Overview

**Job_App** is an AI-powered job application automation platform designed to become a full-featured web application comparable to jobright.ai. The system streamlines the entire job search workflow through a sophisticated 7-phase pipeline that handles resume parsing, job discovery, intelligent scoring, resume tailoring, application submission, tracking, and reporting.

**Long-term Vision:** Build a production-ready SaaS platform for automated job applications with:

- Multi-user support with authentication
- Resume versioning and management
- Job opportunity curation and personalization
- Real-time application tracking dashboard
- Advanced filtering and preferences
- Integration with major job boards (LinkedIn, Indeed, Glassdoor, etc.)

---

# Project Context

This is an AI agent workspace for autonomous job application automation. The core deliverable is `agent.py` — a sophisticated 7-phase pipeline that:

1. **Ingests resumes** (Phase 1) → Extracts profile using LLM with MD5-based caching
2. **Discovers jobs** (Phase 2) → Scrapes from multiple sources, respects customizable limits
3. **Scores jobs** (Phase 3) → Two-tier scoring: fast heuristic pre-filtering + selective LLM evaluation
4. **Tailors resumes** (Phase 4) → Generates customized resume/cover letter per job
5. **Submits applications** (Phase 5) → Automated or manual application submission
6. **Tracks progress** (Phase 6) → Maintains Excel tracker of all applications
7. **Generates reports** (Phase 7) → Summary statistics and next steps

---

# Running the Agent

**Setup** (one-time):

```bash
pip install -r requirements.txt
```

**Launch the FastAPI web UI:**

```bash
python app.py
# Navigate to http://localhost:8000
```

**Run the backend directly (CLI):**

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

## Tech Stack

- **Backend:** FastAPI (Python) with SSE streaming for real-time updates
- **Frontend:** React with Tailwind CSS (served from `frontend/index.html`)
- **LLM Integration:**
  - Anthropic Claude (claude-opus-4-6) — primary production model
  - Local Ollama — for offline/cost-free development
  - Demo mode — pure regex/keyword matching for testing
- **Job Scraping:** `jobspy` library (supports LinkedIn, Indeed, Glassdoor, ZipRecruiter, etc.)
- **Data Storage:**
  - Excel (openpyxl) for application tracking
  - JSON for job caching (auto-cleared each Phase 2 run)
  - MD5-based profile caching for instant resume re-extraction
- **Real-time UI:** Server-Sent Events (SSE) for streaming phase updates and logs

## Project Structure

```
Job_App/
├── app.py                      # FastAPI backend (main entry point)
├── agent.py                    # CLI phase pipeline
├── frontend/
│   ├── index.html             # React single-page app (React CDN)
│   └── ...                    # Static assets
├── pipeline/
│   ├── phases.py              # 7-phase execution logic
│   ├── config.py              # Global config, demo jobs, CLI spinner
│   └── providers.py           # LLM provider abstraction
├── resources/                 # Input data (sample resumes, job lists)
├── output/                    # Generated artifacts (tailored resumes, tracker, reports)
├── requirements.txt           # Python dependencies
└── CLAUDE.md                  # This file
```

## Backend Architecture (`app.py`)

**FastAPI endpoints:**

- `GET /api/state` — Retrieve current pipeline state
- `POST /api/config` — Update pipeline configuration
- `GET /api/phase/{n}/run` — SSE stream for running phase n
- `GET /api/phase/{n}/rerun` — SSE stream for re-running phase n (clears downstream phases)
- `POST /api/reset` — Reset entire pipeline state
- `GET /api/health` — Health check

**In-memory state dictionary (`_S`):**

```python
_S = {
    'done': set(),                 # Completed phases
    'error': {},                   # Error messages by phase
    'elapsed': {},                 # Execution times by phase
    'resume': None,                # Loaded resume text
    'resume_filename': '',         # Filename of loaded resume
    'resume_md5': '',              # MD5 hash for caching
    'has_resume': False,
    'mode': 'anthropic',           # 'anthropic', 'demo', or 'ollama'
    'api_key': '',                 # Anthropic API key
    'ollama_model': 'llama3.2',
    'job_titles': '',
    'location': '',
    'threshold': 75,               # Auto-apply score threshold
    'max_apps': 20,                # Max applications per run
    'max_scrape_jobs': 20,         # NEW: Customizable job scrape limit (1-100)
    'cover_letter': False,
    'experience_levels': [...],
    'education_filter': ["Bachelor's", "Unknown"],  # UPDATED: Now includes "Bachelor's" + Unknown
    'include_unknown_education': True,
    'citizenship_filter': 'Exclude required',
    'use_simplify': True,          # Use SimplifyJobs scraper
    'days_old': 30,                # Job posting age filter
    'llm_score_limit': 10,         # NEW: Limit LLM scoring to top N jobs (configurable)
    'blacklist': '',               # Companies to skip
    'whitelist': '',               # Companies to prioritize
}
```

**SSE Handler (`_run_phase_sse`):**

- Runs phase in background thread
- Captures logs via `_LogCapture` wrapper
- Streams progress updates every 0.2s
- Yields final result with elapsed time

**Phase Re-running (`_clear_phases_after`):**

- Clears all phases downstream of target phase (n+1 through 7)
- Removes cached results, errors, elapsed times
- Allows users to iterate on individual phases without resetting entire pipeline

## Frontend Architecture (`frontend/index.html`)

**React Component Structure:**

- Main `App` component manages state for:
  - `cfg` — Pipeline configuration
  - `phaseDone`, `phaseRunning`, `phaseTimes`, `phaseErrors`, `phaseResults` — Phase tracking
  - `resume` — Uploaded resume info
  - `screen` — Current UI screen ('onboard', 'pipeline', etc.)

**SSE Handler (`runPhaseSSE`):**

- Opens EventSource to `/api/phase/{n}/run` or `/api/phase/{n}/rerun`
- Handles 'start', 'done', 'error' message types
- Calls callbacks for UI updates

**Phase Display:**

- Running phases show hardcoded CLI animations (CLI_LINES)
- Results display real data from backend (`phaseResults`)
- Re-run button appears when phase is completed
- Advanced settings sidebar includes:
  - Max scrape jobs slider (1-100)
  - LLM score limit slider (1-50)
  - All other configuration options

**Animation Tuning:**

- CLI loading bar: 5.6s sweep
- CLI lines: 0.72s per line
- Phase spinner: 20-second intervals

## Phase Functions (`pipeline/phases.py`)

| Function | Phase | Input | Output | Optimizations |
|---|---|---|---|---|
| `phase1_ingest_resume()` | 1 | Resume text | Profile dict | MD5 cache: instant re-extraction for same resume |
| `phase2_discover_jobs()` | 2 | Config | Job list | Clears cache each run, respects max_scrape_jobs |
| `phase3_score_jobs()` | 3 | Jobs + Profile | Scored jobs | Two-tier: fast_score() filter → LLM top N only |
| `phase4_tailor_resume()` | 4 | Top jobs + Resume | Tailored resumes | Generates TeX + PDF per job |
| `phase5_simulate_submission()` | 5 | Scored jobs | Submission log | Demo mode only; simulates submission |
| `phase6_update_tracker()` | 6 | Results | Excel tracker | openpyxl; 17-column schema |
| `phase7` (inline) | 7 | All data | Report markdown | Summary stats + recommendations |

### Key Optimizations

**Phase 1 Caching (MD5-based):**

```python
resume_md5 = hashlib.md5(resume_text.encode()).hexdigest()
cache_file = _PROJECT_ROOT / f"pipeline_cache_{resume_md5}.json"
if cache_file.exists():
    return json.load(open(cache_file))
# Extract profile...
json.dump(profile, open(cache_file, 'w'))
```

- Same resume content processed in milliseconds on repeat runs
- Different resumes produce different MD5, fresh extraction

**Phase 2 Cache Clearing:**

- `sample_jobs.json` deleted at START of phase2_discover_jobs()
- Cache is NOT re-saved at end of phase
- Ensures fresh job discovery on every run, respects max_scrape_jobs limit

**Phase 3 Two-Tier Scoring:**

1. **Fast Heuristic (`fast_score()`):** O(n) keyword matching
   - Filters by experience level, education, citizenship
   - Matches skills from profile against job requirements
   - Returns 0-100 score without LLM

2. **Selective LLM Scoring:** Only top jobs from fast_score()
   - Configurable limit (`llm_score_limit`, default 10)
   - Detailed evaluation of most-promising candidates
   - Reduces Phase 3 runtime by ~80%

**Scoring weights** (Phase 3):

- Skills match: 30% | Title alignment: 25% | Experience: 15%
- Education: 10% | Industry: 10% | Location: 10%

## Provider Abstraction (`pipeline/providers.py`)

Three pluggable LLM backends:

### AnthropicProvider

- Uses `claude-opus-4-6` (latest high-capability model)
- Structured tool_use calls for JSON output
- Enables `thinking` mode for resume parsing and tailoring
- Requires `ANTHROPIC_API_KEY` environment variable

### DemoProvider

- Pure Python regex/keyword matching
- Zero LLM cost, fully offline
- Uses hardcoded `DEMO_JOBS` list (8 semiconductor/EE internship postings)
- Perfect for testing without API keys

### OllamaProvider

- Calls any local Ollama model via OpenAI-compatible `/v1` endpoint
- Default model: `llama3.2`
- Requires Ollama running at `localhost:11434`
- Optional dependency (`openai` package)

---

# Agent Behavior Rules

- **Clarify:** Always ask clarifying questions before starting a complex task.
- **Plan-first:** Show a short plan and steps before executing any multi-step task.
- **Concise outputs:** Prefer bullet-point summaries over long paragraphs.
- **Save outputs:** Save all final deliverables to the `output/` folder.
- **Citations:** Cite sources when doing research (include links and brief notes).
- **Permissions:** Ask before accessing or modifying private accounts or external services.
- **Next Steps:** Include suggestion whenever beneficial.

## Job Application Agent Configuration

- **Auto-apply threshold:** match score ≥ 75 (brief pause for user approval before submitting)
- **Manual review:** score 60–74
- **Skip:** score < 60
- **Default job search date range:** last 14 days
- **Max automated applications per run:** 20
- **Max jobs to scrape:** 1–100 (default 20, user-customizable)
- **LLM score limit:** 1–50 (default 10, user-customizable)
- **Tracker filename:** `Job_Applications_Tracker_YYYY-MM.xlsx` (saved to `output/`)
- **Resume cache:** MD5-based, auto-cleared and regenerated per session

---

# Output Conventions

- **Folder:** `output/`
- **Filename pattern:** `YYYYMMDD_task_short-description.ext` (e.g., `20260504_resume_CompanyX.pdf`)
- **Tailored resumes:** `[UserName]_Resume_[CompanyName]_[JobTitle].pdf` (append `_v2` etc. for duplicates)
- **Cover letters:** `[UserName]_CoverLetter_[CompanyName].pdf`
- **Tracker:** `Job_Applications_Tracker_YYYY-MM.xlsx`
- **Reports:** Markdown files with phase summaries
- **Drafts:** Markdown; final resumes: PDF; data artifacts in named subfolders

---

# Workflow Reference

- `Workflow/job-application-agent.md` — canonical master prompt spec (7 phases, scoring rubric, tracker schema, startup checklist). Read this before modifying phase logic in `agent.py`.

---

# Known Issues & Limitations

1. **Rich console output capture:** Wrapping stdout for log streaming interferes with Rich console functionality, causing black screen. Currently using hardcoded CLI animations as workaround.
2. **Ollama performance:** Local models are slower than Claude for complex resume tailoring. Recommend Claude (Anthropic) for production use.
3. **Job board authentication:** Some job boards (LinkedIn, etc.) require auth; SimplifyJobs scraper handles this. Direct scraping is limited.
4. **Resume format:** Currently expects plain text. PDF support requires text extraction preprocessing.

---

# Future Roadmap (SaaS Vision)

## Phase 1: Multi-user platform

- User authentication (OAuth2 with Google/GitHub)
- User dashboard with application history
- Resume library per user (versioning, templates)
- Job preference profiles

## Phase 2: Enhanced job curation

- Real-time job board integrations
- Saved job filters and alerts
- Candidate fit scoring breakdown
- Employer research (Glassdoor ratings, salary data)

## Phase 3: Smart application strategy

- A/B testing different resume formats
- Application result analytics
- Interview prep materials (company-specific)
- Salary negotiation guidance

## Phase 4: Full SaaS deployment

- Multi-region infrastructure
- Email notifications and summaries
- Mobile app (React Native)
- API for third-party integrations

---

# Performance Targets

- **Phase 1 (Resume Parsing):** < 5s (< 1s with MD5 cache hit)
- **Phase 2 (Job Discovery):** 10–30s (depends on scraper sources)
- **Phase 3 (Scoring):** 15–45s (depends on llm_score_limit)
- **Phase 4 (Tailoring):** 30–120s (depends on job count)
- **Total pipeline:** 2–5 minutes with Anthropic Claude

---

# Development Notes

## Testing the Full Pipeline

1. **With demo jobs (no API key):**

   ```bash
   python agent.py --demo
   ```

2. **With sample resume (test caching):**
   - Upload resume in UI
   - Run Phase 1 twice → second run should be instant (cache hit)

3. **Testing max_scrape_jobs:**
   - Set slider to 5 in UI advanced settings
   - Run Phase 2 → verify only ~5 jobs discovered

4. **Testing llm_score_limit:**
   - Set slider to 3 in UI advanced settings
   - Run Phase 3 → verify LLM only scores top 3 jobs (others use fast_score)

## Frontend Development

- React imported via CDN (no build step needed)
- Tailwind CSS for styling
- Changes live-reload when `frontend/index.html` is saved
- Open browser DevTools (F12) to inspect network SSE streams

## Backend Development

- FastAPI auto-reloads on file save (use `--reload` flag if needed)
- SSE streaming is connection-based; check browser network tab for `/api/phase/{n}/run` streams
- State persists in memory; call `/api/reset` to clear

---

# About Me

<!-- TODO: Replace this section with a brief description of yourself -->

**Developer:**

**Background:**

## Goals

- **Short-term:** Optimize pipeline performance (Phase 3 two-tier scoring ✅, Phase 1 caching ✅, Phase 2 cache clearing ✅)
- **Medium-term:** Launch as functional SaaS (multi-user, auth, job tracking dashboard)
- **Long-term:** Compete with jobright.ai as primary job search automation platform

## Technical Interests

- AI/LLM integration (Claude API)
- Full-stack web development (React, FastAPI)
- Job board automation and scraping
- Data pipeline optimization

---

# Key Contacts & Resources

- **GitHub:** <https://github.com/Sao-06/Job_App>
- **Main branch:** `main` (production)
- **Development branch:** `job-finder-fix` (active feature development)
- **API Key:** Set `ANTHROPIC_API_KEY` environment variable for Anthropic Claude backend

---

# Recent Changes (Latest Session)

**Commit:** `9c9cd60` — "Optimize pipeline performance and improve job discovery"

**Major additions:**

- Two-tier scoring (Phase 3): fast heuristic pre-filter + selective LLM
- MD5-based profile caching (Phase 1): instant re-extraction for same resume
- Max scrape jobs customization (Phase 2): 1–100, default 20
- Phase re-running: individual phases can be re-executed without pipeline reset
- LLM score limit slider in advanced settings (UI)
- Improved animation timing for better UX

**Files modified:** app.py, frontend/index.html, pipeline/phases.py, pipeline/config.py, pipeline/providers.py

**Performance gains:**

- Phase 1: instant (with cache)
- Phase 3: ~80% faster (selective LLM)
- Overall pipeline: 2–5 minutes typical

---

# Fillable Template (Copy & Customize)

- **Full name:**
- **LinkedIn URL:** [Your LinkedIn]
- **Current university and major:**
- **Current year:**
- **Top 3 technical interests:** AI/ML, Hardware Design, Automation
- **Key skills & tools:**
  - Python, JavaScript, React, FastAPI
  - Verilog, SPICE, PCB design
  - LLM APIs (Anthropic Claude, Ollama)
- **Primary internship/job targets:**
  - Primary: FPGA Engineer, IC Design Intern — NVIDIA, Intel, Qualcomm
  - Secondary: Hardware Engineer — Apple, Microsoft, Samsung Semiconductors
- **Job Boards:** LinkedIn, Indeed, Glassdoor, ZipRecruiter, Wellfound, Dice, Handshake
- **Preferred resume format:** 1 page
- **Permissions:** Agent may create/commit files: YES

# Environment & Preferences

- **OS:** Windows 11
- **Timezone:**
- **Tone:** Professional, concise, and helpful
- **Languages & tools:** Python, JavaScript/React, FastAPI, Git, Anthropic Claude API

# Contact & Ownership

- **GitHub:** <https://github.com/Sao-06/Job_App>

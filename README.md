# Jobs AI — Job Application Agent

An autonomous, 7-phase Python agent that ingests your resume, discovers matching jobs, scores them against your profile, tailors a resume per posting (with optional cover letter), simulates or submits applications, and produces a color-coded Excel tracker plus a plain-language run report.

The agent ships with three runtime surfaces:

1. **Web app** — FastAPI backend + React SPA. Multi-user sessions with per-session state, real-time SSE log streaming, and an integrated Dev Ops console. *(Recommended.)*
2. **CLI** — `agent.py` runs the full pipeline interactively from a terminal.
3. **Flask dashboard** — `dashboard/app.py` is a lightweight tracker viewer with one-click "Approve" for manual rows.

Three LLM backends are supported with no architecture changes: **Anthropic Claude** (highest quality), **local Ollama** (free, private), and **Demo mode** (zero cost, zero setup, works offline).

---

## Table of Contents

- [Quickstart](#quickstart)
- [Architecture at a Glance](#architecture-at-a-glance)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Choosing an LLM Backend](#choosing-an-llm-backend)
- [Running the Web App](#running-the-web-app)
- [Running the CLI](#running-the-cli)
- [Running the Tracker Dashboard](#running-the-tracker-dashboard)
- [The 7-Phase Pipeline](#the-7-phase-pipeline)
- [Configuration Reference](#configuration-reference)
- [Output Files](#output-files)
- [REST API Reference](#rest-api-reference)
- [Dev Ops Console](#dev-ops-console)
- [Persistence & Sessions](#persistence--sessions)
- [Troubleshooting](#troubleshooting)
- [Project Layout](#project-layout)
- [Known Limitations](#known-limitations)

---

## Quickstart

```bash
# 1. Install
pip install -r requirements.txt

# 2. Pick one:
python app.py                          # Web app at http://localhost:8000
python agent.py --demo                 # CLI, zero-setup demo
python agent.py --ollama               # CLI with local Ollama
python agent.py                        # CLI with Anthropic (needs ANTHROPIC_API_KEY)
```

Web app:

1. Open `http://localhost:8000` → click into the app at `/app`.
2. Sign up (email + password ≥ 6 chars) **or** sign in with Google **or** click "Try the demo profile."
3. Upload a resume (PDF / DOCX / TXT / TEX). Profile extraction runs in the background.
4. Open the **Agent** page and run phases 1 → 7. Logs stream in real time. Output files appear in the sidebar.

---

## Architecture at a Glance

```
┌───────────────────────────────────────────────────────────────────────┐
│  frontend/index.html  +  frontend/app.jsx  (React SPA, Babel-in-browser)│
│  Pages: Dashboard · Jobs · Resume · Profile · Agent · Settings ·       │
│         Feedback · Dev · Auth · Onboarding                              │
└────────────────────────────────┬──────────────────────────────────────┘
                                 │  REST + SSE
┌────────────────────────────────▼──────────────────────────────────────┐
│  app.py            FastAPI server, session middleware, SSE streaming   │
│  auth_utils.py     bcrypt password hashing + Google OAuth helpers      │
│  session_store.py  SQLite-backed user/session/state persistence        │
└────────────────────────────────┬──────────────────────────────────────┘
                                 │
┌────────────────────────────────▼──────────────────────────────────────┐
│  pipeline/                                                              │
│  ├── phases.py        7 phase functions + Playwright submitter         │
│  ├── providers.py     Anthropic / Ollama / Demo LLM providers          │
│  ├── resume.py        PDF/DOCX/TEX extraction + LaTeX/PDF generation   │
│  ├── scrapers.py      JobSpy, SimplifyJobs, Jobright, Internlist,      │
│  │                    Himalayas, Remotive, Arbeitnow                    │
│  ├── profile_audit.py Post-extraction validation & ranking             │
│  ├── latex.py         LaTeX detection + pdflatex compilation           │
│  ├── helpers.py       Date/string utilities, dedupe, filters           │
│  └── config.py        Constants (OUTPUT_DIR, MAX_SCRAPE_JOBS, etc.)    │
│                                                                         │
│  agent.py             CLI entry point (re-exports pipeline.*)          │
│  dashboard/app.py     Standalone Flask tracker viewer (port 5000)      │
└─────────────────────────────────────────────────────────────────────────┘
```

State and outputs:

- **Session DB:** `output/jobs_ai_sessions.sqlite3` (users, sessions, state JSON)
- **Per-session output:** `output/sessions/<session_id>/` (resumes, tracker, report)
- **LLM caches:** `resources/profile_cache_*.json`, `resources/sample_jobs_quick.json`, `resources/sample_jobs_deep.json`

---

## Prerequisites

- **Python 3.9+**
- **pip**
- *(Optional)* **Ollama** — for free local inference. Install from <https://ollama.com>.
- *(Optional)* **pdflatex** — for highest-quality PDF resume output. Without it, `reportlab` is used as a pure-Python fallback.
- *(Optional)* **Playwright browsers** — only needed for `--real-apply` mode. Run `playwright install chromium` after pip install.

---

## Installation

```bash
git clone <your-fork>
cd Job_App
pip install -r requirements.txt
```

`requirements.txt` installs everything needed for all three backends and the full web app:

| Package | Used for |
|---|---|
| `anthropic` | Claude provider |
| `openai` | Ollama provider (Ollama exposes an OpenAI-compatible endpoint) |
| `fastapi`, `uvicorn`, `python-multipart` | Web server |
| `bcrypt`, `google-auth`, `google-auth-oauthlib` | Auth |
| `pdfplumber`, `python-docx` | Resume extraction |
| `reportlab` | PDF resume fallback |
| `openpyxl` | Excel tracker |
| `python-jobspy` | LinkedIn / Indeed / Glassdoor scraping |
| `playwright` | Real form submission (Greenhouse boards) |
| `flask` | Tracker dashboard + standalone Flask UI |
| `pandas`, `streamlit` | Legacy Streamlit UI (optional) |
| `pyyaml` | `config/skill_keywords.yaml` |
| `rich` | Terminal output |

---

## Choosing an LLM Backend

| Mode | Quality | Cost | Setup | Internet required |
|---|---|---|---|---|
| **Anthropic Claude** *(default)* | Highest — uses tool-use for strict JSON, plus thinking mode | API usage | Set `ANTHROPIC_API_KEY` | Yes |
| **Ollama (local)** | Good (varies by model) | Free | Install Ollama + pull a model | Only to pull models |
| **Demo** | Heuristic / regex only | Free | None | No |

### Anthropic (default)

Uses model **`claude-opus-4-6`** via the official SDK. Resume extraction and tailoring use forced tool-call output to guarantee JSON schema compliance.

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=sk-ant-...

# Windows PowerShell
$env:ANTHROPIC_API_KEY="sk-ant-..."

# Windows CMD
set ANTHROPIC_API_KEY=sk-ant-...
```

In the web app, the API key can also be entered through **Settings**; it lives in volatile session state and is never written to disk.

### Ollama (local, free)

```bash
# 1. Install Ollama: https://ollama.com
ollama pull llama3.2          # ~2 GB — recommended default
# or:  ollama pull mistral
# or:  ollama pull gemma3

# 2. Make sure ollama is running
ollama serve
```

The web app talks to `http://localhost:11434/v1` (OpenAI-compatible). The **Settings** page exposes a status indicator and a `Pull model` button that streams `ollama pull <model>` output back to the UI. JSON quality varies by model — `llama3.2` and `mistral` are the most reliable.

### Demo

Pure Python: regex/keyword matching plus a hardcoded list of 8 EE/semiconductor internship postings (NVIDIA, Apple, Intel, Lumentum, Micron, Microsoft, IBM Research, Samsung). No API key, no network, fully offline. Good for testing the pipeline plumbing end-to-end.

Skill keywords used by Demo mode are configurable in `config/skill_keywords.yaml`.

---

## Running the Web App

```bash
python app.py
# or:
uvicorn app:app --reload --port 8000
```

The server binds `0.0.0.0:8000`. Routes:

- `GET /` → marketing landing page (`frontend/landing.html`)
- `GET /app` → the SPA shell
- `GET /frontend/<file>` → static assets
- `GET /output/<path>` → generated files (locked to the requesting session's folder)
- `GET/POST/DELETE /api/...` → JSON API (see [REST API Reference](#rest-api-reference))

### First-run flow

1. **Auth.** First-time visitors land on the auth page (`/app#auth`). Three options:
   - **Sign up** with email + password (≥ 6 chars). Stored in SQLite with bcrypt hash.
   - **Sign in with Google** — requires `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` env vars; otherwise a dev stub flow returns a `dev@example.com` test user.
   - **Onboarding** — accepts a resume upload and walks you through demo mode.
2. **Resume upload.** Upload triggers Phase 1 (profile extraction) automatically in a background thread. Multiple resumes are supported; one is marked **primary** and feeds the rest of the pipeline.
3. **Run the agent.** From the **Agent** page, run each phase or use the run-all button. Each phase emits SSE log events that render in real time.

### Frontend pages

| Page | Purpose |
|---|---|
| **Dashboard** | Profile snapshot, score histogram, skill donut, activity feed, tip card |
| **Jobs** | Scored job list with like/hide, deep-search trigger, infinite scroll |
| **Resume** | Multi-resume manager: upload, edit text, set primary, rename, delete, re-extract |
| **Profile** | Editable extracted profile (titles, skills, education, work/research roles, projects) |
| **Agent** | Phase-by-phase runner with progress rings and log streams |
| **Settings** | LLM backend, model, search params, filters, threshold, cover-letter mode |
| **Feedback** | Submit feedback to the Dev Ops console |
| **Dev** *(developers only)* | Sessions, impersonation, restricted CLI, UI tweaks |
| **Auth** | Login / signup / Google OAuth |
| **Onboarding** | First-time guided flow |

---

## Running the CLI

```bash
python agent.py [flags]
```

`agent.py` walks you through an interactive 10-question startup checklist (resume path, target titles, location, threshold, salary floor, blacklist, whitelist, cover-letter mode, max apps), then runs the full 7-phase pipeline with live progress in the terminal.

### CLI flags

| Flag | Default | Description |
|---|---|---|
| `--demo` | off | Run with `DemoProvider` — no API key, no network |
| `--ollama` | off | Use local Ollama LLM |
| `--model NAME` | `llama3.2` | Ollama model name |
| `--section-order LIST` | profile-derived | Comma-separated resume section order, e.g. `Summary,Skills,Experience,Projects,Education` |
| `--real-apply` | off | Use `PlaywrightSubmitter` for real Greenhouse form submission; falls back to simulation otherwise |
| `--dashboard` | off | After scoring, write a preliminary tracker and launch the Flask dashboard at `localhost:5000` for review before submission |
| `--experience LIST` | `internship,entry-level` | Comma-separated experience levels to include |
| `--education LIST` | `bachelors,masters` | Education levels you hold (drives Phase 3 fit) |
| `--include-unknown-education` | off | Keep jobs whose required education couldn't be inferred |
| `--citizenship` | `all` | One of `all`, `exclude_required`, `only_required` |
| `--no-simplify` | off | Skip the SimplifyJobs / GitHub internship listings scraper |

### CLI examples

```bash
# Zero-setup demo run with all defaults
python agent.py --demo

# Free local LLM
python agent.py --ollama --model mistral

# Real submissions to Greenhouse boards (requires playwright install chromium)
python agent.py --real-apply

# Restrict to remote-friendly jobs and review the tracker before submission
python agent.py --ollama --dashboard
```

If `ANTHROPIC_API_KEY` is unset and neither `--demo` nor `--ollama` is passed, the CLI exits with a setup error.

---

## Running the Tracker Dashboard

A lightweight Flask UI for reviewing the current month's tracker:

```bash
python dashboard/app.py        # http://localhost:5000
```

Or auto-launch it after Phase 3 in the CLI:

```bash
python agent.py --demo --dashboard
```

Features:

- Tabular view of all jobs with status pills (Applied / Manual Review / Approved / Skipped)
- One-click **Approve** for `Manual Required` rows — writes "Approved" status back to the Excel tracker and recolors the row
- Reads the active month's `Job_Applications_Tracker_YYYY-MM.xlsx` from `output/`

---

## The 7-Phase Pipeline

Each phase is a standalone function in `pipeline/phases.py`. The web app exposes each as `GET /api/phase/<n>/run` (SSE) and `/api/phase/<n>/rerun`. The CLI orchestrator runs them in sequence in `agent.py:run_agent`.

| # | Phase | Function | Inputs | Outputs |
|---|---|---|---|---|
| 1 | **Ingest Resume** | `phase1_ingest_resume` | resume text | `profile` dict (name, contact, target titles, top hard/soft skills, education, experience, projects, gaps). Cached to `resources/profile_cache_<hash>.json` |
| 2 | **Discover Jobs** | `phase2_discover_jobs` | profile, titles, location | List of job dicts. Sources: JobSpy (LinkedIn/Indeed/Glassdoor), SimplifyJobs, Jobright, InternList, Himalayas, Remotive, Arbeitnow. Cached to `resources/sample_jobs_quick.json` and `sample_jobs_deep.json` |
| 3 | **Score Jobs** | `phase3_score_jobs` | jobs, profile | Each job tagged with `score` (0–100), `score_breakdown`, `matching_skills`, `missing_skills`, `filter_status` (`passed` / `below_threshold` / `filtered_*`) |
| 4 | **Tailor Resume** | `phase4_tailor_resume` | each `passed` job, profile | Rewritten summary, reordered skills, ATS keyword analysis, optional cover letter. Saves `<Name>_Resume_<Company>_<Title>.tex` and `.pdf` |
| 5 | **Submit** | `phase5_simulate_submission` *(or `PlaywrightSubmitter` with `--real-apply`)* | tailored applications | Each application gets a `status` (`Applied` / `Manual Required` / `Skipped`) and confirmation number |
| 6 | **Update Tracker** | `phase6_update_tracker` | applications | `Job_Applications_Tracker_YYYY-MM.xlsx` — 17 columns, color-coded rows, frozen header, Summary Dashboard tab |
| 7 | **Run Report** | `phase7_run_report` | applications, tracker path | `YYYYMMDD_job-application-run-report.md` — plain-language summary, top 3 jobs, manual items, next steps |

### Scoring rubric (Phase 3)

Each job is scored 0–100 against three weighted categories:

| Category | Weight |
|---|---|
| Required skills match | 50 |
| Industry / domain overlap | 30 |
| Location & seniority fit | 20 |

Scores feed three thresholds (configurable):

| Score | Default action |
|---|---|
| ≥ `threshold` (default 75) | Auto-eligible — tailored and submitted up to `max_apps` |
| 60 – `threshold` | Tailored, but submitted as **Manual Required** |
| < 60 | Logged as **Skipped** with a reason |

Filters that can drop a job *before* the 60-point cutoff: education-required mismatch (when `include_unknown_education=False`), citizenship mismatch (`citizenship=exclude_required` drops `Required`-only roles), experience-level mismatch, and explicit blacklist matches.

---

## Configuration Reference

### Environment variables

| Variable | Required for | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic mode | `sk-ant-...` API key |
| `GOOGLE_CLIENT_ID` | Google sign-in | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google sign-in | Google OAuth secret |
| `OAUTHLIB_INSECURE_TRANSPORT` | Local OAuth dev | Auto-set to `1` when redirect URI is `localhost`/`127.0.0.1` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `NOTIFY_EMAIL` | Optional | If all set, Phase 7 emails the run report |

### `config/skill_keywords.yaml`

Drives `DemoProvider` matching. Edit freely — all top-level groups are flattened at runtime.

```yaml
hardware:
  - verilog
  - fpga
  - cmos
  # ...
software:
  - python
  - matlab
  # ...
domain:
  - photolithography
  - cleanroom
  # ...
```

To target a different field (data, ML, web, etc.), replace the keys/values with relevant terms — no Python change required.

### Web-app settings (per session)

These are stored in session state and editable from the **Settings** page or via `POST /api/config`:

| Key | Default | Description |
|---|---|---|
| `mode` | `ollama` | `demo` / `ollama` / `anthropic` |
| `api_key` | `""` | Anthropic API key (volatile only) |
| `ollama_model` | `llama3.2` | Ollama model name |
| `threshold` | `75` | Auto-apply score floor |
| `job_titles` | `Engineer` | Comma-separated target titles |
| `location` | `United States` | Search location |
| `max_apps` | `10` | Max submissions per run |
| `max_scrape_jobs` | `20` | Cap on Phase 2 results |
| `days_old` | `30` | Drop postings older than this |
| `cover_letter` | `false` | Generate cover letters |
| `blacklist` | `""` | Comma-separated companies to drop |
| `whitelist` | `"NVIDIA, Apple, Microsoft, Intel, IBM, Micron, Samsung, TSMC"` | Always surfaced regardless of score |
| `experience_levels` | `["internship", "entry-level"]` | Allowed seniority bands |
| `education_filter` | `["bachelors"]` | Education levels you hold |
| `include_unknown_education` | `true` | Keep jobs with unknown education req. |
| `citizenship_filter` | `exclude_required` | `all` / `exclude_required` / `only_required` |
| `use_simplify` | `true` | Enable SimplifyJobs source |
| `llm_score_limit` | `10` | How many top fast-matched jobs get LLM scoring |
| `force_customer_mode` | `false` | Hide dev tools (set on a developer account to preview the customer view) |
| `light_mode` | `false` | UI theme toggle |

---

## Output Files

All outputs land in `output/` (root) and `output/sessions/<session_id>/` (per session):

| File | Description |
|---|---|
| `Job_Applications_Tracker_YYYY-MM.xlsx` | 17-column tracker with color-coded statuses, frozen header, auto-fit columns, Summary Dashboard tab |
| `<Name>_Resume_<Company>_<Title>.tex` | Tailored LaTeX resume |
| `<Name>_Resume_<Company>_<Title>.pdf` | Compiled PDF (via `pdflatex`, falls back to `reportlab`) |
| `<Name>_CoverLetter_<Company>.txt` | Cover letter (when enabled) |
| `YYYYMMDD_job-application-run-report.md` | Plain-language run summary |

Tracker color coding:

| Color | Status |
|---|---|
| Green | Applied |
| Yellow | Manual Required |
| Light blue | Approved (set via the Flask dashboard) |
| Red | Skipped (low match) |
| Gray | Error |

---

## REST API Reference

All endpoints live under `/api/`. Cookies (`jobs_ai_session`, `jobs_ai_auth`, `dev_impersonate_id`) are HTTP-only and `SameSite=Lax`. Phase routes return `text/event-stream`; everything else is JSON.

### Auth

| Method | Path | Description |
|---|---|---|
| POST | `/api/auth/signup` | `{email, password}` (≥ 6 chars). Creates user, sets cookies. |
| POST | `/api/auth/login` | `{email, password}` |
| POST | `/api/auth/logout` | Clears auth cookie, starts a fresh session |
| GET | `/api/auth/google` | Returns OAuth authorization URL |
| GET | `/api/auth/google/callback` | OAuth landing — redirects back to `/app` |

### Resume

| Method | Path | Description |
|---|---|---|
| POST | `/api/resume/upload` | Multipart `file` upload (PDF/DOCX/TXT/TEX). First upload becomes primary. Triggers Phase 1 in background. |
| POST | `/api/resume/demo` | Loads built-in demo resume |
| GET | `/api/resume/content?id=<id>` | Returns the raw text of a resume |
| POST | `/api/resume/text` | `{id, text}` — overwrite text and clear stored profile |
| POST | `/api/resume/primary/<id>` | Mark a resume as primary |
| POST | `/api/resume/rename/<id>` | `{filename}` |
| DELETE | `/api/resume/<id>` | Remove a resume; promotes next one to primary |

### Profile

| Method | Path | Description |
|---|---|---|
| GET | `/api/profile` | Current extracted profile |
| POST | `/api/profile` | Merge fields into the profile |
| POST | `/api/profile/extract` | Re-run extraction. Body: `{resume_id?, preferred_titles?, force?}` |

### Pipeline phases

| Method | Path | Description |
|---|---|---|
| GET | `/api/phase/1/run` (SSE) | Run Phase 1 (Ingest) |
| GET | `/api/phase/2/run?deep=1&append=1&force=1` (SSE) | Run Phase 2 (Discover). `deep` enables JobSpy LinkedIn deep search; `append` merges with existing results |
| GET | `/api/phase/3/run?fast=1` (SSE) | Run Phase 3 (Score). `fast=1` skips the LLM rerank |
| GET | `/api/phase/4/run` (SSE) | Run Phase 4 (Tailor) |
| GET | `/api/phase/5/run` (SSE) | Run Phase 5 (Submit / Simulate) |
| GET | `/api/phase/6/run` (SSE) | Run Phase 6 (Tracker) |
| GET | `/api/phase/7/run` (SSE) | Run Phase 7 (Report) |
| GET | `/api/phase/<n>/rerun` | Same as `/run` but clears downstream phase state first |
| GET | `/api/phase/2/cache` | Inspect Phase 2 cache (counts + age) |
| DELETE | `/api/phase/2/cache` | Clear Phase 2 cache and downstream phases |

### State, jobs, feedback, reset

| Method | Path | Description |
|---|---|---|
| GET | `/api/state` | Full session state used by the SPA on every refresh |
| POST | `/api/config` | Merge config keys (see [Web-app settings](#web-app-settings-per-session)) |
| POST | `/api/jobs/action` | `{action: like|unlike|hide|unhide, job_id}` |
| POST | `/api/feedback` | `{message}` — visible in the Dev Ops console |
| POST | `/api/reset` | Wipe pipeline state + per-session output files. Preserves auth, prefs, mode |

### Ollama helpers

| Method | Path | Description |
|---|---|---|
| GET | `/api/ollama/status` | Probes `localhost:11434` and returns running/pulled state plus local models |
| GET | `/api/ollama/pull` (SSE) | Streams `ollama pull <model>` output |

### Dev Ops *(developers only — see below)*

| Method | Path | Description |
|---|---|---|
| GET | `/api/dev/overview` | All sessions, summary stats, app status |
| GET | `/api/dev/session/<id>` | Full state of a session (resume text capped at 2 KB) |
| POST | `/api/dev/session/<id>/reset` | Reset a session's pipeline state |
| DELETE | `/api/dev/session/<id>` | Delete a session entirely |
| POST | `/api/dev/session/<id>/impersonate` | Set `dev_impersonate_id` cookie |
| POST | `/api/dev/session/stop-impersonating` | Clear impersonation |
| POST | `/api/dev/session/<id>/feedback/read` | Mark feedback read |
| POST | `/api/dev/cli` | `{command}` — one of `git_status`, `recent_outputs`, `session_db`, `pip_freeze` |
| POST | `/api/dev/tweaks` | Merge UI tweaks (banner text, density, etc.) |

---

## Dev Ops Console

The `/app` SPA exposes a **Dev** tab to developer sessions. A session is treated as developer when the authenticated user has `users.is_developer = 1` in the SQLite store. Flip the flag with `session_store.set_user_developer(user_id, True)`.

For local debugging only, set the env var `LOCAL_DEV_BYPASS=1` to additionally grant dev access to loopback callers without the DB flag.

`force_customer_mode` overrides the dev flag and forces the customer view — useful for screen-sharing or testing the non-dev experience.

The console exposes:

- **Sessions table:** all users, with applied/manual/error counts, feedback unread badges, last-updated timestamps.
- **Session detail:** raw state, profile, scored jobs, applications.
- **Impersonation:** "View as user" — sets the `dev_impersonate_id` cookie so subsequent requests use that session's state. Local-only.
- **Restricted CLI:** runs one of four whitelisted commands and returns up to 4 KB of output.
- **UI tweaks:** dev banner text, color/layout overrides, promo strip toggle.
- **Feedback inbox:** unread badge driven by `session_state.feedback[].read`.

> ⚠️ **Known issue (tracked):** `/api/resume/upload` currently has no auth gate. Anonymous uploads create a fresh session and a "ghost profile" visible in the Dev Ops console. Plan to add auth-before-upload before exposing the app publicly.

---

## Persistence & Sessions

`session_store.py` provides `SQLiteSessionStore`, backed by `output/jobs_ai_sessions.sqlite3`. Three tables:

- `users(id, email, password_hash, google_id, created_at)`
- `sessions(id, user_id, created_at, updated_at)`
- `session_state(session_id, state_json, updated_at)`

State serialization (`json_default` / `normalize_state`) handles `set` and `pathlib.Path` objects, and rehydrates `done` / `liked_ids` / `hidden_ids` / `error` / `elapsed` correctly on load. WAL mode is enabled where the OS allows it.

Cookies:

| Cookie | Purpose |
|---|---|
| `jobs_ai_session` | Maps the browser to a session row |
| `jobs_ai_auth` | Bearer-style token (random 32-byte URL-safe string) → user record |
| `dev_impersonate_id` | When set on a localhost request, overrides `jobs_ai_session` for the dev "view as user" feature |

If the SQLite store fails to initialize (e.g. permissions), the app falls back to a process-local `_memory_sessions` dict so the server still boots.

---

## Troubleshooting

**Phase 1 returns an empty profile.** Make sure your resume parses correctly: `python -c "from pipeline.resume import _read_resume; print(_read_resume('path/to/resume.pdf')[0][:500])"`. Extraction tries `pypdfium2 → pdfplumber → pypdf → pdfminer.six`.

**Ollama "model not pulled".** Open **Settings** → click **Pull**, or run `ollama pull <model>` in a terminal. The status indicator polls `localhost:11434/api/tags`.

**`ANTHROPIC_API_KEY not set` on CLI.** Either pass `--demo`/`--ollama`, or export the key (see above). The web app accepts the key from the Settings page.

**PDF resume output looks plain.** Install `pdflatex` (TeX Live / MiKTeX). The `reportlab` fallback uses a generic template.

**Phase 2 returns nothing.** Hit `DELETE /api/phase/2/cache` (or use the **Settings** "Clear cache" button), then rerun. Some sources are rate-limited; try `--no-simplify` or lower `max_scrape_jobs`.

**`--real-apply` errors.** Run `playwright install chromium` once. Only Greenhouse-hosted boards are supported; everything else falls back to simulation.

**Windows + Rich emoji crash.** `app.py` and `pipeline/config.py` force UTF-8 stdout. If you still see `cp1252` errors, run with `PYTHONIOENCODING=utf-8`.

---

## Project Layout

```
Job_App/
├── agent.py                 CLI entry point + argparse
├── app.py                   FastAPI server
├── auth_utils.py            bcrypt + Google OAuth helpers
├── session_store.py         SQLite-backed session/user persistence
├── requirements.txt
├── jobs.db                  (legacy, currently unused)
├── CLAUDE.md                Detailed technical spec (companion to this README)
├── config/
│   └── skill_keywords.yaml  DemoProvider keyword groups
├── dashboard/
│   └── app.py               Standalone Flask tracker viewer
├── frontend/
│   ├── index.html           SPA shell
│   ├── app.jsx              React app (single file, ~2.6 K lines)
│   ├── landing.html         Marketing landing page
│   └── hero-bg.png
├── pipeline/
│   ├── __init__.py
│   ├── config.py            Constants, console, demo jobs
│   ├── helpers.py           Date/string utils, dedupe, filters
│   ├── latex.py             LaTeX detection + pdflatex compile
│   ├── phases.py            7 phase functions, Playwright submitter, tracker
│   ├── profile_audit.py     Post-extraction profile validation
│   ├── providers.py         Anthropic / Ollama / Demo
│   ├── resume.py            PDF/DOCX/TEX extraction + resume generation
│   └── scrapers.py          7 scraper clients
├── resources/               Profile + job caches (auto-created)
├── output/                  Generated trackers, resumes, reports
│   ├── jobs_ai_sessions.sqlite3
│   └── sessions/<id>/       Per-session output isolation
└── Workflow/
    └── job-application-agent.md   Canonical phase spec
```

---

## Known Limitations

- **Auth gate before upload is not yet enforced.** Anonymous uploads create ghost profiles in Dev Ops; plan to fix before public deployment.
- **Real submissions are Greenhouse-only.** `--real-apply` works against Greenhouse-hosted boards; other ATS systems fall back to simulated submissions.
- **Demo provider is EE/semiconductor-tuned out of the box.** `DEMO_JOBS` and `config/skill_keywords.yaml` reflect electrical-engineering internships. Edit the YAML and `pipeline/config.py:DEMO_JOBS` for other fields.
- **No cross-run dedup against the tracker.** Phase 5 dedupes against the in-memory shortlist; the Excel tracker is not read back on subsequent runs.
- **Ollama JSON quality is model-dependent.** Smaller / heavily quantized models can produce malformed JSON; the provider degrades gracefully but scoring will be coarser.
- **Live job-board coverage is best-effort.** JobSpy and SimplifyJobs change frequently; expect periodic breakage.

---

## License

This project is provided as-is. Add your preferred license here.

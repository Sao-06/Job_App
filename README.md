# Jobs AI — Autonomous Job Application Platform

A multi-user web app + 7-phase Python pipeline that ingests your resume, ranks live jobs from a continuously-refreshed local index, tailors a resume per posting (with optional cover letter), curates the top picks for review, and produces an Excel tracker plus a plain-language run report.

Runtime surfaces:

1. **Web app** — FastAPI backend + React 18 SPA (Babel-in-browser, no build step). Multi-user sessions, real-time SSE phase streaming, live job ingestion via APScheduler, integrated Dev Ops console, optional Stripe Pro billing. *(Recommended.)*
2. **CLI** — `agent.py` runs the full pipeline interactively from a terminal. Optional Playwright real-submission mode for Greenhouse boards via `--real-apply`.
3. **Flask dashboard** — `dashboard/app.py` is a lightweight tracker viewer (port 5000) with one-click "Approve" for manual rows.

**LLM backends.** Two tiers, both running through the same Ollama daemon hosted on the deployment server (no install needed for end users):
- **Free — local Ollama**: small open-weight models (llama / mistral / qwen / gemma) pulled to disk on the Pi. Fast, private, low-quality reasoning.
- **Pro — cloud Ollama**: `*-cloud` model names are transparently proxied through the local daemon to Ollama Turbo's hosted servers (frontier-class quality, much sharper scoring & tailoring).

**Anthropic Claude** is under active development and reserved for developer testing — it will land in Pro at no extra cost when it launches publicly. The Demo / heuristic provider is no longer user-selectable but still powers the regex baseline + the in-app fallback when Ollama is offline.

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
│  frontend/                                                              │
│  ├── landing.html    Marketing page (scroll-driven scrubber)            │
│  ├── index.html      SPA shell + CSS tokens                             │
│  └── app.jsx         React 18 SPA — Babel-in-browser, no build step.   │
│  Pages: Home · Jobs · Resume · Profile · Agent · Settings · Plans ·    │
│         Feedback · Dev · Auth · Onboarding                              │
└────────────────────────────────┬──────────────────────────────────────┘
                                 │  REST + SSE (auth-cookie gated)
┌────────────────────────────────▼──────────────────────────────────────┐
│  app.py              FastAPI server, contextvars-backed session proxy, │
│                      SSE phase streaming, server log mirror, Stripe    │
│                      webhook, psutil dev-ops metrics.                  │
│  auth_utils.py       bcrypt + Google OAuth helpers.                    │
│  session_store.py    SQLite-backed users / sessions / state /          │
│                      auth_tokens (SHA-256 digested) + Stripe customer. │
└────────────────────────────────┬──────────────────────────────────────┘
                                 │
┌────────────────────────────────▼──────────────────────────────────────┐
│  pipeline/                                                              │
│  ├── phases.py            7 phase functions + tracker writer +         │
│  │                        PlaywrightSubmitter (CLI --real-apply only). │
│  ├── providers.py         Anthropic / Ollama / Demo +                  │
│  │                        deterministic compute_skill_coverage.        │
│  ├── profile_extractor.py Heuristic-first scanner (regex + sections).  │
│  ├── profile_audit.py     Flatten / quarantine / retention / verify.   │
│  ├── heuristic_tailor.py  LLM-free tailoring + structural validator.   │
│  ├── resume.py            Multi-lib PDF chain + LaTeX + reportlab.     │
│  ├── resume_insights.py   Deterministic resume metrics + AI verify.    │
│  ├── pdf_format.py        pdfplumber layout fingerprint (cols/font/   │
│  │                        accent) — generated PDFs mirror source.      │
│  ├── latex.py             Detect / sanitize / compile.                 │
│  ├── helpers.py           Inference helpers + URL validation +         │
│  │                        infer_job_category.                          │
│  ├── config.py            Constants + DB-path migration.               │
│  ├── job_repo.py          job_postings + FTS5 + source_runs schema +   │
│  │                        canonical_url + upsert + mark_missing.       │
│  ├── job_search.py        BM25 + skill_overlap + freshness +           │
│  │                        title_match rerank, dedupe-by-listing,       │
│  │                        round-robin company / category diversify.    │
│  ├── ingest.py            APScheduler background worker — parallel     │
│  │                        boot backfill + per-source IntervalTriggers. │
│  ├── migrations.py        ensure_column / ensure_index — SINGLE source │
│  │                        of truth for SQLite schema additions.        │
│  ├── stripe_billing.py    Lazy SDK wrapper (Customer / Checkout /      │
│  │                        Portal / verify_webhook).                    │
│  └── sources/             20+ pluggable JobSource providers — auto-    │
│                           register on import. Keyless + keyed APIs +   │
│                           Greenhouse / Lever / Ashby / Workable ATS.   │
│                                                                         │
│  agent.py            CLI entry point (re-exports pipeline.*).          │
│  dashboard/app.py    Standalone Flask tracker viewer (port 5000).      │
└─────────────────────────────────────────────────────────────────────────┘
```

State and outputs:

- **Persistent state**: `data/jobs_ai_sessions.sqlite3` — users + sessions + session_state + auth_tokens + job_postings + FTS5 + source_runs. Lives outside `output/` so the static-file route can never serve it. The legacy path `output/jobs_ai_sessions.sqlite3` is auto-renamed at startup by `pipeline/config.py:migrate_db_path()`.
- **User-visible artifacts**: `output/` — served by `GET /output/{path}` with a suffix allow-list and per-session auth check. Per-session generated files live under `output/sessions/<session_id>/`.
- **LLM caches**: `resources/profile_cache_*.json` (phase-1 cache keyed on resume + provider + model + titles).
- **Tests**: `tests/unit/` + `tests/integration/` — 487+ pytest tests with FastAPI TestClient + a Stripe SDK fake. CI runs on every push (`.github/workflows/test.yml`).

---

## Prerequisites

- **Python 3.10+** (the codebase uses match statements / PEP 604 unions; tested on 3.10–3.12).
- **pip**.
- *(Optional)* **Ollama** — for free local inference. Install from <https://ollama.com>. The Free-tier default is `smollm2:135m` (small, fast, runs anywhere); Pro upgrades unlock `gemma4:31b-cloud` (Ollama Turbo). Override with `DEFAULT_OLLAMA_MODEL` / point at a remote host with `OLLAMA_URL`.
- *(Optional)* **pdflatex** (TeX Live or MiKTeX) — best-quality tailored PDFs. Without it, `reportlab` is the pure-Python fallback.
- *(Optional)* **Playwright browsers** — only for `--real-apply`. Run `playwright install chromium` after `pip install`.
- *(Optional)* **psutil** — present in `requirements.txt`. Drives the live htop-style Dev Ops panels (CPU / memory / temp / processes). Without it, those panels render as "psutil not installed".
- *(Optional)* **Stripe CLI** — only when developing or testing the Pro billing flow. `stripe listen --forward-to http://localhost:8000/api/webhooks/stripe` prints the dev `whsec_…` signing secret.

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
| `anthropic` | Claude provider (under development — dev-only until launch) |
| `openai` | Ollama client (Ollama exposes an OpenAI-compatible endpoint) |
| `fastapi`, `uvicorn`, `python-multipart` | Web server |
| `apscheduler` | Background job-ingestion scheduler |
| `bcrypt`, `google-auth`, `google-auth-oauthlib` | Auth |
| `python-dotenv` | Auto-load `.env` at startup |
| `psutil` | Live Dev Ops metrics (CPU / memory / temp / top processes) |
| `pdfplumber`, `python-docx` | Resume extraction (+ pypdfium2 / pypdf / pdfminer.six fall-backs) |
| `reportlab` | PDF resume fallback when `pdflatex` is unavailable |
| `openpyxl` | Excel tracker (CLI flow) |
| `python-jobspy` | Legacy live scrapers (`pipeline/scrapers.py`, no longer wired in) |
| `playwright` | Real form submission (Greenhouse boards) — CLI `--real-apply` only |
| `stripe` | Stripe Pro billing — Checkout / Portal / Webhook |
| `flask` | Standalone tracker dashboard |
| `pyyaml` | `config/skill_keywords.yaml` |
| `rich` | Terminal output |
| `pandas`, `streamlit` | ⚠ Legacy / unused — Streamlit UI was removed; `streamlit_app.py` no longer exists. |

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

### Ollama (local / Turbo)

```bash
# 1. Install Ollama: https://ollama.com
ollama pull smollm2:135m          # Free-tier default (LOCAL_OLLAMA_MODEL)
ollama pull gemma4:31b-cloud      # Pro-tier default (CLOUD_OLLAMA_MODEL, Ollama Turbo)

# 2. Make sure ollama is running
ollama serve
```

The two canonical model names are pinned in `app.py` as `LOCAL_OLLAMA_MODEL` / `CLOUD_OLLAMA_MODEL`. Both must be `ollama pull`-ed on the daemon ahead of time — the boot auto-pull handles the local one for free.

The web app talks to `${OLLAMA_URL}/v1` (OpenAI-compatible; default `http://localhost:11434`). The **Settings** page exposes a status indicator + a `Pull model` button that streams `ollama pull <model>` output back to the UI via `/api/ollama/pull` SSE. `*-cloud` / `:cloud` models bypass the local-pull check (Ollama Turbo) — these are the **Pro-tier high-quality cloud models**; non-Pro users get snapped to a local default by `_load_session_state` and `SettingsPage`.

`POST /api/ollama/ensure` is idempotent — pulls the session model if missing, no-ops if already pulled. The boot-time auto-pull at startup ensures the default model is available before the first user lands on Settings.

### Demo / heuristic mode

Pure Python: regex/section-aware extraction + lexicon-driven keyword matching. **Soft skills** are extracted from text via `_scan_soft_skills` (no hardcoded list — the previous "Teamwork / Problem-solving / …" list was a bug). Hard skills come from `config/skill_keywords.yaml`. Eight built-in `DEMO_JOBS` cover EE/semiconductor internships for testing pipeline plumbing.

DemoProvider is no longer a user-selectable mode (only `ollama` and `anthropic` are accepted by `/api/config`), but the class still serves as the heuristic baseline for Phase 1's `scan_profile` AND as the safety-net fallback when Ollama is offline or returns malformed JSON.

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
| **Home** | Hero + CockpitStrip HUD (status / greeting / live clock / streak / phase / high-fit count) + mission-control quick actions + Resume Intelligence dossier |
| **Jobs** | Live feed against the local index (`/api/jobs/feed`), facet filters (industry / location / company), 65-entry curated location quick-pick, debounced search, per-job Ask Atlas drawer + Tailor-for-this-job drawer, 25 s polling tick for newly-ingested rows |
| **Resume** | Multi-resume manager: drag/drop upload, original-PDF iframe OR generated preview PDF, AI insights tabs, edit text, set primary, rename, delete, Re-scan |
| **Profile** | Auto-save (700 ms debounce) form. Five-state AutoSaveBadge. ProfileSelect for the 17 canonical US work-auth options |
| **Agent** | Phase-by-phase runner with progress rings + SSE log streams; inline `max_scrape_jobs` slider; career-wide Atlas chat sidebar (`/api/atlas/chat/stream`) |
| **Settings** | LLM backend + Ollama model, search prefs, filters, threshold, cover-letter mode, Job Discovery sliders |
| **Plans** | Free / Pro tier display. Currently uses the `requestUpgrade` feedback stub; Stripe `startCheckout` / `openPortal` versions are commented out (one-line swap to go live) |
| **Feedback** | Textarea → `POST /api/feedback` |
| **Dev** *(developers only)* | Sessions table, live CPU/memory/temp panels, live server log SSE, plan-tier editor, restricted CLI, UI tweaks |
| **Auth** | Login / signup / Google OAuth (currently labelled "under development" — flow stays wired but users are routed to email login) |
| **Onboarding** | First-time guided flow — resume upload + demo |

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
| `--model NAME` | `smollm2:135m` | Ollama model name (CLI default = the Free-tier `LOCAL_OLLAMA_MODEL`; pass `gemma4:31b-cloud` for the Pro cloud model) |
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

| # | Phase | Function | What it does |
|---|---|---|---|
| 1 | **Ingest Resume** | `phase1_ingest_resume` | **Heuristic-first.** `pipeline.profile_extractor.scan_profile` runs deterministic regex+section parsing → LLM `extract_profile(text, …, heuristic_hint=…)` verifies & corrects → `merge_profiles` → `pipeline.profile_audit.audit_profile` (flatten / quarantine misplaced soft-skills / retention audit / verify evidence / rerank titles) → `pipeline.resume_insights.analyze_resume`. Cached to `resources/profile_cache_<md5>.json`. |
| 2 | **Discover Jobs** | `phase2_discover_jobs` | **Reads from the local job index** (`pipeline.job_search.search`), NOT live scraping. Every user filter is pushed through to the SQL `WHERE`. `force_live=True` (set by `?force=1` / `?append=1`) triggers a synchronous `pipeline.ingest.force_run()`. Empty index → falls back to provider-generated demo jobs. |
| 3 | **Score Jobs** | `phase3_score_jobs` | Two-stage. **`compute_skill_coverage` is deterministic** — the LLM cannot override the 50-point skill-coverage dimension. Fast-score every job; LLM-rerank top `llm_score_limit` (default 10) for the qualitative `industry` + `location_seniority` dims. `?fast=1` skips the LLM rerank. Pre-filters by experience and citizenship. Filter status: `passed | below_threshold | filtered_experience | filtered_citizenship`. |
| 4 | **Tailor Resume** | `phase4_tailor_resume` | Always runs the deterministic `heuristic_tailor` baseline; validates the LLM output via `validate_tailoring`; on shape failure retries once, then falls back to heuristic; merges hybrid mode. **Anti-fabrication**: `skills_reordered` only reorders existing skills; missing JD keywords go in `ats_keywords_missing`. ATS scoring before/after. Output: `<Name>_Resume_<Company>_<Title>.tex` + `.pdf`. |
| 5 | **Curate** | `phase5_simulate_submission` | **Web flow: curation step**, NOT auto-submit. Top-N (`llm_score_limit`) high-confidence picks → `Manual Required`; the rest → `Skipped`. The randomized "Applied" coin-flip stub was deliberately removed. CLI can opt into `PlaywrightSubmitter` via `--real-apply` (Greenhouse boards only, falls through to simulation otherwise). |
| 6 | **Update Tracker** | `phase6_update_tracker` | Web flow: `write_file=False` — returns `{month, columns, rows, summary}` for in-page rendering. CLI: writes `Job_Applications_Tracker_YYYY-MM.xlsx` (18 columns, color-coded, frozen header, auto-fit, Summary Dashboard tab). |
| 7 | **Run Report** | `phase7_run_report` | `provider.generate_report` → markdown. Web flow: `write_file=False` (renders inline). CLI: `YYYYMMDD_job-application-run-report.md`. SMTP notification fires only when ALL of `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `NOTIFY_EMAIL` are set. |

### Scoring rubric (Phase 3)

Each job is scored 0–100 against three weighted categories. The first is computed deterministically by `compute_skill_coverage`; the LLM only judges the latter two:

| Category | Weight | Source |
|---|---|---|
| Required skills match | 50 | Deterministic — `compute_skill_coverage(job, profile)` |
| Industry / domain overlap | 30 | LLM (Anthropic / Ollama with json_mode) |
| Location & seniority fit | 20 | LLM, with location-string + seniority-band signals |

Scores feed three thresholds (configurable):

| Score | Default action |
|---|---|
| ≥ `threshold` (default 75) | Auto-eligible — surfaced as the top picks for review |
| 50 – `threshold` | Below threshold but kept visible so the user can inspect "why" |
| < 50 | Skipped or filtered |

Filters applied *before* scoring drop the job to a `filtered_*` status: experience-level mismatch (with `include_unknown_experience=True` passthrough — most ingested rows are metadata-only), citizenship mismatch (`citizenship_filter=exclude_required` drops jobs with hard "U.S. citizenship required" / clearance signals), explicit blacklist matches (applied at the SQL layer in Phase 2 already). `whitelist` companies are NOT a hard inclusion gate — they're a +0.25 ranking boost in the search rerank so they surface first when present.

### Job ingestion (continuous background)

A **separate sub-system** runs continuously to keep the local index fresh — phase 2 just reads from it. See `pipeline/sources/`, `pipeline/ingest.py`, `pipeline/job_repo.py`, `pipeline/job_search.py`. Highlights:

- **20+ pluggable sources** — `JobSource` Protocol implementations under `pipeline/sources/`. Auto-register on import. Keyless: `github_readme` (Simplify-style READMEs), `api_themuse`, `api_remoteok`, `api_jobicy`, `api_himalayas`, `api_remotive`, `api_arbeitnow`, `api_weworkremotely`. Keyed (skip silently when env var missing): `api_usajobs`, `api_adzuna`, `api_reed`, `api_jooble`, `api_findwork`. ATS readers (curated international slugs): `ats_greenhouse`, `ats_lever`, `ats_ashby`, `ats_workable`.
- **APScheduler `BackgroundScheduler`** in `pipeline/ingest.py` — one `IntervalTrigger` per source at its `cadence_seconds`. Per-source `threading.Lock` prevents reentrancy. Boot-time parallel backfill across all sources (60 s wall clock), skipped if the index already has fresh rows. Set `JOBS_AI_DISABLE_INGESTION=1` in tests.
- **`job_postings` schema** with FTS5 + soft-delete (`miss_count >= 3 → deleted=1`). Canonical-URL UNIQUE strips utm/fbclid/gclid/etc.
- **Search rerank**: `0.45*bm25 + 0.30*skill_overlap + 0.15*freshness + 0.10*title_match`. Whitelist boosts +0.25. Cross-source / multi-city dupes collapse via `_dedupe_by_listing`. Cold (no-profile) feed runs `_diversify_by_category` so a brand-new visitor sees a cross-industry sample. `_diversify_by_company` round-robin (one entry per company per round) keeps a single dominant employer from monopolizing page 1.

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

### Environment variables (continued)

Beyond the auth/SMTP set listed above, the app reads several optional ingestion API keys (each source skips registration silently when its key is missing) and Stripe keys:

| Variable | Required for | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic mode | `sk-ant-...` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google sign-in | OAuth credentials |
| `GOOGLE_OAUTH_DEV_DUMMY` | Local OAuth dev | When `=1`, the dummy "dev@example.com" flow fires (only without real Google credentials) |
| `LOCAL_DEV_BYPASS` | Local dev | When `=1`, loopback callers are auto-promoted to dev (NEVER set behind a proxy) |
| `OLLAMA_URL` | Custom Ollama host | Default `http://localhost:11434` |
| `DEFAULT_OLLAMA_MODEL` | New session default | Default `smollm2:135m` (the Free-tier `LOCAL_OLLAMA_MODEL`) |
| `JOBS_AI_DISABLE_INGESTION` | Test harness | When set, skips the APScheduler boot + parallel backfill |
| `JOBS_AI_SKIP_MIGRATION` | Test harness | When set, skips the import-time DB-path migration |
| `PRODUCTION` | Prod | When `=1`, marks auth cookies `Secure` so browsers reject them over plain HTTP |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `NOTIFY_EMAIL` | Optional | If ALL set, Phase 7 emails the report |
| `USAJOBS_USER_AGENT` / `USAJOBS_API_KEY` | USAJobs.gov | Free dev account |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Adzuna | Free dev account |
| `REED_API_KEY` | Reed.co.uk | Free dev account |
| `JOOBLE_API_KEY` | Jooble | Free dev account |
| `FINDWORK_API_KEY` | Findwork.dev | Free dev account |
| `STRIPE_SECRET_KEY` | Pro billing | `sk_test_…` (dev) or `sk_live_…` (prod) |
| `STRIPE_PRICE_ID_PRO_MONTHLY` | Pro billing | Set after running `python scripts/setup_stripe.py` |
| `STRIPE_WEBHOOK_SECRET` | Pro billing | `whsec_…` from `stripe listen` (dev) or dashboard (prod) — they are DIFFERENT |
| `PUBLIC_BASE_URL` | Behind proxy | Public origin for Checkout / Portal redirects |

### Web-app settings (per session)

These are stored in session state and editable from the **Settings** page or via `POST /api/config`. The `/api/config` endpoint enforces a strict whitelist — keys not in the list below are dropped silently.

| Key | Default | Description |
|---|---|---|
| `mode` | `ollama` | `ollama` / `anthropic` (the `demo` mode was retired; the class is the heuristic baseline / Ollama fallback now). 402 returned for `anthropic` from non-Pro non-dev callers. |
| `api_key` | `""` | Anthropic API key (volatile session state only — never written to disk) |
| `ollama_model` | `smollm2:135m` | Ollama model name (Free default = `LOCAL_OLLAMA_MODEL`; switch to `gemma4:31b-cloud` for Pro). `*-cloud` / `:cloud` models gated to Pro. |
| `threshold` | `75` | Auto-eligible score floor |
| `job_titles` | `""` | Comma-separated target titles. Empty by default — filled from the resume after Phase 1 |
| `location` | `""` | Search location. Empty by default — filled from the resume |
| `max_apps` | `10` | Max submissions per run (CLI mostly) |
| `max_scrape_jobs` | `50` | Cap on Phase 2 results from the local index |
| `days_old` | `30` | Drop postings older than this |
| `cover_letter` | `false` | Generate cover letters |
| `blacklist` | `""` | Comma-separated companies to drop (SQL filter, not post-cap) |
| `whitelist` | `""` | Comma-separated priority companies — +0.25 ranking boost (NOT a hard inclusion gate) |
| `experience_levels` | `[]` | Allowed seniority bands. Empty by default — filled from the resume |
| `education_filter` | `[]` | Education levels you hold. Empty by default — filled from the resume |
| `include_unknown_education` | `true` | Keep jobs with unknown education req |
| `include_unknown_experience` | `true` | Keep jobs with unknown experience level (most ingested rows are metadata-only) |
| `citizenship_filter` | `all` | `all` / `exclude_required` / `only_required` |
| `use_simplify` | `true` | Vestigial — kept for compatibility; current ingestion always includes the SimplifyJobs README |
| `llm_score_limit` | `10` | How many top fast-matched jobs get LLM scoring (Phase 3) — also caps Phase 5 picks |
| `force_customer_mode` | `false` | Dev escape: hide Dev Ops UI without flipping the DB flag |
| `light_mode` | `false` | UI theme toggle |

---

## Output Files

User-visible artifacts only land in `output/` (CLI flow) or `output/sessions/<session_id>/` (web flow). The web flow renders trackers and reports inline by default; pass `write_file=True` if you want a file too. Persistent state (DB, auth tokens, job index) lives in `data/` outside `output/` so it can never be served.

| File | Description |
|---|---|
| `Job_Applications_Tracker_YYYY-MM.xlsx` | 18-column tracker (CLI only): status / score / industry / location / URL / company website / portal / resume version / cover letter / confirmation / notes / follow-up / response received. Color-coded rows, frozen header, auto-fit columns, Summary Dashboard tab |
| `<Name>_Resume_<Company>_<Title>.tex` | Tailored LaTeX resume |
| `<Name>_Resume_<Company>_<Title>.pdf` | Compiled PDF — `pdflatex` first (sanitized for `\input` / `\write18` / etc.), `reportlab` fallback. Honours per-resume layout fingerprint (column count / font sizes / accent color from `pipeline/pdf_format.py`) |
| `<Name>_CoverLetter_<Company>.txt` | Cover letter (when enabled) |
| `YYYYMMDD_job-application-run-report.md` | Plain-language run summary (CLI only by default) |
| `output/sessions/<sid>/uploads/<id>{.pdf,.tex,.txt,.md,.docx}` | Original uploaded resume bytes — embedded in the Resume page iframe |
| `output/sessions/<sid>/uploads/<id>_preview.pdf` | Generated polished preview PDF for non-PDF uploads (renders the resume profile via reportlab) |

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

### Resume (continued)

| POST | `/api/resume/tailor` | `{job_id, cover_letter?}` — per-job on-demand tailoring. Same item shape as one Phase 4 row |
| POST | `/api/resume/<id>/render-preview` | Back-fill the polished preview PDF for legacy records that lack one |

### Profile (continued)

| GET | `/api/profile/diagnose?id=` | Heuristic-only diagnostic — returns the regex/section parser output WITHOUT calling the LLM. Useful for debugging "why is this section empty?" |

### Pipeline phases

| Method | Path | Description |
|---|---|---|
| GET | `/api/phase/1/run` (SSE) | Run Phase 1 (Ingest) |
| GET | `/api/phase/2/run?deep=1&append=1&force=1` (SSE) | Run Phase 2 (Discover from local index). `deep` widens posting age to 90 days. `append` merges with existing results AND triggers `force=1` (re-tick every source synchronously) |
| GET | `/api/phase/3/run?fast=1` (SSE) | Run Phase 3 (Score). `fast=1` skips the LLM rerank entirely |
| GET | `/api/phase/4/run` (SSE) | Run Phase 4 (Tailor) |
| GET | `/api/phase/5/run` (SSE) | Run Phase 5 (Curate top picks) |
| GET | `/api/phase/6/run` (SSE) | Run Phase 6 (Tracker) |
| GET | `/api/phase/7/run` (SSE) | Run Phase 7 (Report) |
| GET | `/api/phase/<n>/rerun` | Same as `/run` but clears downstream phase state first |
| GET | `/api/phase/2/cache` | Inspect legacy Phase 2 JSON cache (counts + age). Largely vestigial now that the live index is the source. |
| DELETE | `/api/phase/2/cache` | Clear legacy cache + downstream phases |

### Live job feed (the actual Jobs page)

| Method | Path | Description |
|---|---|---|
| GET | `/api/jobs/feed?cursor=&limit=&q=&exp=&edu=&cit=&remote=&days=&location=&industry=&since_id=&blacklist=&whitelist=` | Cursor-paginated, profile-ranked feed against `job_postings` |
| GET | `/api/jobs/facets?kind={industry,location,company}&q=&limit=` | **PUBLIC** — pure aggregate counts. No PII. Used by the chip dropdowns + landing page stats |
| POST | `/api/jobs/action` | `{action: like|unlike|hide|unhide, job_id}` |
| GET | `/api/jobs/source-status` | Per-source health snapshot for Dev Ops |
| POST | `/api/jobs/source-status` | Force one source (or all) to re-tick. Dev only |
| POST | `/api/jobs/ask` | Per-job Ask Atlas advisor — `{job_id, message, history?}` → `{reply}` |
| POST | `/api/atlas/chat/stream` | Career-wide Atlas streamer — same body, returns SSE chunks `{type: 'start' \| 'delta' \| 'done' \| 'error'}` |

### State, config, feedback, reset

| Method | Path | Description |
|---|---|---|
| GET | `/api/state` | Full session state. SPA polls every 2 s while resume extraction is running, 8 s otherwise. Includes `is_dev`, `dev_simulating`, `plan_tier`, `is_pro`, `billing_configured`, `has_billing_customer` |
| POST | `/api/config` | Merge config keys. Strict whitelist enforced; non-whitelisted keys are dropped silently. 402 for `mode='anthropic'` or `*-cloud` Ollama models from non-Pro non-dev callers |
| POST | `/api/feedback` | `{message}` — visible in Dev Ops console |
| POST | `/api/reset` | Wipe pipeline state + per-session output files. Preserves `user`, `dev_tweaks`, `mode`, `api_key`, `ollama_model`, `light_mode`, `force_customer_mode` |

### Ollama helpers

| Method | Path | Description |
|---|---|---|
| GET | `/api/ollama/status` | Probes `${OLLAMA_URL}` and returns running/pulled state plus local models. Includes any in-flight pull state |
| POST | `/api/ollama/ensure` | Idempotent — pull session model if not already present |
| GET | `/api/ollama/pull` (SSE) | Streams `ollama pull <model>` output |

### Billing (Stripe)

| Method | Path | Description |
|---|---|---|
| POST | `/api/billing/checkout` | Auth-gated. Subscription-mode Checkout Session. 409 if already Pro. 503 until `STRIPE_PRICE_ID_PRO_MONTHLY` is set |
| POST | `/api/billing/portal` | Auth-gated. Stripe Customer Portal redirect. 404 if no `stripe_customer_id` yet |
| POST | `/api/webhooks/stripe` | HMAC-verified, NOT cookie-authenticated. Source of truth for `plan_tier` flips. In middleware `skip_save` list |

### Dev Ops *(developers only)*

| Method | Path | Description |
|---|---|---|
| GET | `/api/dev/overview` | All sessions, summary stats, app status, live system metrics |
| GET | `/api/dev/metrics?with_processes=1` | Fast-path psutil snapshot — CPU per-core, memory, temp, top processes |
| GET | `/api/dev/logs?since=&limit=` | Recent server stdout/stderr/`logging` records (3000-line ring buffer) |
| GET | `/api/dev/logs/stream` (SSE) | Live tail of new log records |
| GET | `/api/dev/users` | All users with plan_tier — Dev Ops Users panel |
| POST | `/api/dev/users/<user_id>/plan` | `{tier}` — manually grant or revoke Pro |
| GET | `/api/dev/session/<id>` | Full state of a session (resume text capped at 2 KB) |
| POST | `/api/dev/session/<id>/reset` | Reset a session's pipeline state |
| DELETE | `/api/dev/session/<id>` | Delete a session entirely |
| POST | `/api/dev/session/<id>/impersonate` | Set `dev_impersonate_id` cookie |
| POST | `/api/dev/session/stop-impersonating` | Clear impersonation |
| POST | `/api/dev/session/<id>/feedback/read` | Mark feedback read |
| POST | `/api/dev/cli` | `{command}` — one of `git_status`, `recent_outputs`, `session_db`, `pip_freeze` |
| POST | `/api/dev/tweaks` | Merge UI tweaks (banner text, density, etc.) |
| GET | `/api/dev/runtime` | Read server-wide runtime knobs (`maintenance`, `verbose_logs`) |
| POST | `/api/dev/runtime` | Toggle runtime knobs |
| POST | `/api/dev/reload-env` | Re-read `.env` from disk without restarting the server |

---

## Dev Ops Console

The `/app` SPA exposes a **Dev** tab to developer sessions. A session is treated as developer when:

- The authenticated user has `users.is_developer = 1` in the SQLite store, OR
- The authenticated user's email is in the **`_DEV_EMAILS` allow-list** inside `_is_underlying_dev_request` (currently `jonnyliu4@gmail.com` and `saosithisak@gmail.com` — auto-promoted on signin without flipping the DB flag).

Flip the DB flag with `_user_store.set_user_developer(user_id, True)`. For local debugging only, set `LOCAL_DEV_BYPASS=1` to additionally grant dev to loopback callers — NEVER set this in prod behind a proxy.

**`force_customer_mode`** is a session-state flag that hides the Dev Ops UI but keeps `_is_underlying_dev_request` true. That pair powers the "Test as Customer" pill so a dev can preview the customer view without locking themselves out. Cleared automatically on every fresh login.

The console exposes:

- **Overview**: sessions table (applied/manual/error counts, feedback unread badges), live system metrics (CPU per-core / memory / temp at 2 s tick), disk + DB stats.
- **Session detail**: raw state, profile, scored jobs, applications, feedback. "View as user" sets the `dev_impersonate_id` cookie (localhost-only).
- **Server logs**: 3000-line ring buffer of stdout / stderr / `logging` records, with a live SSE feed (`/api/dev/logs/stream`).
- **Users**: roster with plan_tier editor (`POST /api/dev/users/<id>/plan` with `{tier}`).
- **Restricted CLI**: runs one of four whitelisted commands and returns up to 4 KB of output.
- **Runtime knobs**: `maintenance` mode (rejects all phase runs), `verbose_logs` (echoes SSE log lines to stderr), `reload-env` (re-read `.env` without restart).
- **UI tweaks**: dev banner text, color/layout overrides, promo strip toggle (CSS custom properties injected on `<html>` every poll).

---

## Persistence & Sessions

`session_store.py` provides `SQLiteSessionStore`, backed by **`data/jobs_ai_sessions.sqlite3`** (NOT `output/` — kept outside the static-file route so it can never be served). Tables:

- `users(id PK, email UNIQUE, password_hash, google_id, is_developer, plan_tier, stripe_customer_id, stripe_subscription_id, created_at)`
- `sessions(id PK, user_id FK, created_at, updated_at)`
- `session_state(session_id PK, state_json TEXT, updated_at)`
- `auth_tokens(token PK [SHA-256 digest], user_id FK, user_json, created_at)`
- `job_postings(id PK, canonical_url UNIQUE, …)` + FTS5 virtual table + triggers
- `source_runs(source, started_at, finished_at, ok, fetched, inserted, updated, error)`

State serialization (`json_default` / `normalize_state`) handles `set` and `pathlib.Path` objects, and rehydrates `done` / `liked_ids` / `hidden_ids` / `extracting_ids` / `error` / `elapsed` correctly on load. WAL mode is enabled where the OS allows it.

**Schema migrations** live in `pipeline/migrations.py`. Adding a new column means **one** `ensure_column(...)` line — never inline a fresh `try: ALTER…; except: pass` block (that's the pattern that bricked the Pi). Migrations run at every boot via `_ensure_schema_migrations` (belt-and-suspenders with the session_store constructor); `python scripts/migrate_db.py [--check]` is the standalone CLI for emergency / out-of-band runs.

**DB corruption recovery**: `_quarantine_if_corrupt` runs `PRAGMA quick_check` before opening; on failure renames the malformed file with a `.corrupt-{YYYYMMDD-HHMMSS}` suffix (preserving WAL/SHM siblings) and rebuilds from scratch. Recovery cost: users + auth tokens + session state are gone (re-sign-in; profiles re-extract; index re-fills within minutes). Better than every `/api/state` 500ing.

**Anonymous sessions never INSERT**: `_save_bound_state` early-returns to `_memory_sessions` when `state.user.id|email` is unset. `peek_state` is read-only. `list_sessions` filters `WHERE user_id IS NOT NULL` to hide legacy ghost rows. This is what kills the "ghost profiles in Dev Ops" failure mode.

**Auth-token storage**: `auth_tokens.token` stores the SHA-256 **digest** (`session_store._hash_token`), never the raw cookie. Boot-time purge deletes any row whose `token` is not exactly 64 hex chars (forces one re-login from every legacy session). A DB read alone cannot impersonate a user.

Cookies (all `HttpOnly`, `SameSite=Lax`, `Secure` only when `PRODUCTION=1`):

| Cookie | Purpose |
|---|---|
| `jobs_ai_session` | Maps the browser to a session_state row |
| `jobs_ai_auth` | Bearer token (32 random URL-safe bytes); looked up against the SHA-256 digest in the DB |
| `dev_impersonate_id` | When set on a localhost request, overrides `jobs_ai_session` for the dev "view as user" feature |

If the SQLite store fails to initialize (e.g. permissions), the app falls back to a process-local `_memory_sessions` dict so the server still boots — auth/sessions degrade to in-memory only.

---

## Troubleshooting

**Phase 1 returns an empty profile.** Make sure your resume parses correctly: `python -c "from pipeline.resume import _read_resume; print(_read_resume('path/to/resume.pdf')[0][:500])"`. Extraction tries `pypdfium2 → pdfplumber → pypdf → pdfminer.six`. The Profile page also has a heuristic-only diagnostic: `GET /api/profile/diagnose?id=<resume_id>` returns the regex/section parser output without calling the LLM, so you can see exactly which sections were detected.

**Sidebar resume returned a city as the name.** Known fixed bug (`cf59a3e`). The name extractor is now section-aware (only the "header" zone before the first section is eligible) with a standalone-city blacklist. Update by re-running extraction ("Re-scan" on the Resume page).

**Ollama "model not pulled".** Open **Settings** → click **Pull**, or run `ollama pull <model>`. `*-cloud` / `:cloud` (Ollama Turbo) models bypass the local-pull check. The status indicator polls `${OLLAMA_URL}/api/tags`.

**Ollama returns scores around 50 for everything.** That used to be the symptom of malformed-JSON fallback to neutral 0.5/0.5/0.5 — now `score_job` uses `json_mode=True`. If still happening, check `journalctl` / Dev Ops logs for explicit JSON parse errors and consider switching to a more capable model.

**`ANTHROPIC_API_KEY not set` on CLI.** Either pass `--demo`/`--ollama`, or export the key. The web app accepts the key from the Settings page, but the field is **developer-only** until Anthropic Claude launches publicly — non-devs see a "Coming soon" disabled option in the mode picker.

**PDF resume output looks plain.** Install `pdflatex` (TeX Live / MiKTeX). The `reportlab` fallback honours the source PDF's layout fingerprint (column count, font sizes, accent color) so it should still match the original aesthetic — but `pdflatex` produces tighter output.

**Phase 2 returns nothing.** The local index might be empty on first boot. Wait for the parallel backfill (60 s budget at boot), or `POST /api/jobs/source-status` to force a full re-tick. The legacy JSON cache (`DELETE /api/phase/2/cache`) is mostly vestigial — the live `job_postings` table is the source.

**`--real-apply` errors.** Run `playwright install chromium` once. Only Greenhouse-hosted boards are supported; everything else falls back to simulation.

**Windows + Rich emoji crash.** `app.py` and `pipeline/config.py` force UTF-8 stdout. If you still see `cp1252` errors, run with `PYTHONIOENCODING=utf-8`.

**`no such column: jp.job_category` (or similar) on the Pi after `git pull`.** The migration framework should self-heal — `_ensure_schema_migrations` runs at FastAPI startup and prints each applied step to stderr. Check `journalctl -u jobapp -n 50` for "[migrations]" lines. If the migration didn't run, try `python scripts/migrate_db.py` standalone. NEVER inline `sqlite3 ... "ALTER TABLE..."` manually — that's how the silent-failure pattern returned.

**Auth cookie present but `/api/state` returns no user.** Auth tokens were rotated (legacy raw-token rows purged on boot to enforce SHA-256 digest storage). One re-login fixes it. The server logs `[api/state] auth cookie present but lookup returned None` for these.

**`/api/jobs/facets` returning 401 spam in the log.** That used to happen pre-`a37575a` because the SPA polled before `/api/state` resolved auth. The endpoint is now PUBLIC — pure aggregate counts, no PII. If you still see 401s, you're on an older build.

**Stripe webhook never fires.** Verify the webhook URL is publicly reachable (Stripe can't hit `localhost`). Use `stripe listen --forward-to http://localhost:8000/api/webhooks/stripe` for local dev — its `whsec_…` is DIFFERENT from the dashboard's. Match the env var to the active source. Behind Tailscale, you need Funnel (plain Tailnet alone won't work).

---

## Project Layout

See **CLAUDE.md** for the canonical, fully-detailed map. Quick orientation:

```
Job_App/
├── agent.py                       CLI entry point + argparse + interactive checklist
├── app.py                         FastAPI server (~5500 lines)
├── auth_utils.py                  bcrypt + Google OAuth
├── session_store.py               SQLite store (users / sessions / state / auth_tokens / Stripe)
├── requirements.txt / pytest.ini / .env.example
├── CLAUDE.md / AGENTS.md          Architecture map + fast on-ramp
│
├── pipeline/
│   ├── __init__.py / config.py / helpers.py / latex.py / pdf_format.py / resume.py
│   ├── phases.py                  7 phase functions + tracker writer + Playwright submitter
│   ├── providers.py               Anthropic / Ollama / Demo + deterministic compute_skill_coverage
│   ├── profile_extractor.py       Heuristic-first scanner
│   ├── profile_audit.py           Post-extraction validation
│   ├── heuristic_tailor.py        LLM-free tailoring + structural validator
│   ├── resume_insights.py         Resume metrics + AI verification
│   ├── job_repo.py                job_postings + FTS5 + source_runs schema
│   ├── job_search.py              BM25 + multi-signal rerank + dedupe + diversification
│   ├── ingest.py                  APScheduler background worker
│   ├── migrations.py              Single source of truth for schema migrations
│   ├── stripe_billing.py          Lazy SDK wrapper
│   ├── scrapers.py                ⚠ STALE — no importers; superseded by sources/
│   └── sources/                   20+ pluggable JobSource providers (auto-register on import)
│
├── frontend/
│   ├── landing.html               Marketing page + scroll-driven scrubber
│   ├── index.html                 SPA shell + CSS tokens
│   ├── app.jsx                    React 18 SPA (~9 K lines, Babel-in-browser, no build step)
│   ├── landing/                   Frozen demo run JSON used by the scrubber
│   └── hero-bg.png
│
├── dashboard/app.py               Standalone Flask tracker viewer (port 5000)
├── Workflow/job-application-agent.md   Canonical 7-phase spec
├── config/skill_keywords.yaml     DemoProvider keyword groups (flattened at runtime)
├── scripts/
│   ├── migrate_db.py              Standalone DB-migration CLI
│   └── setup_stripe.py            One-shot Product + Price creator
├── tools/freeze_landing_demo.py   Generate demo-run.json from a real run
├── tests/                         pytest suite (unit + integration + Stripe fakes, 487+ tests)
├── data/                          Server-private state — DB lives here, not in output/
│   └── jobs_ai_sessions.sqlite3
├── resources/profile_cache_*.json    LLM caches
├── output/                        User-visible artifacts only
│   └── sessions/<id>/             Per-session resumes + uploaded original previews
└── .github/workflows/test.yml     CI
```

---

## Known Limitations

- **Stripe Pro frontend is parked.** Backend is fully wired and tested; the SPA Plans page deliberately uses the legacy `requestUpgrade` feedback stub at `frontend/app.jsx:7113`. Admins flip `plan_tier` manually via Dev Ops Sessions → PLAN panel. Switching to live billing is a one-line JSX swap; see CLAUDE.md §8 for the playbook.
- **Real submissions are Greenhouse-only.** `--real-apply` works against Greenhouse-hosted boards; other ATS systems fall back to simulated submissions. The `phase5_simulate_submission` web flow is curation-only — it never auto-applies.
- **Demo provider is EE/semiconductor-tuned out of the box.** `DEMO_JOBS` and `config/skill_keywords.yaml` reflect electrical-engineering internships. Edit the YAML and `pipeline/config.py:DEMO_JOBS` for other fields. Note: Demo is no longer a user-selectable mode (`/api/config` accepts `ollama` and `anthropic` only); the class still serves as the heuristic baseline / Ollama fallback.
- **No cross-run dedup against the tracker.** Phase 5 dedupes against the current month's tracker only; cross-month dedup is not implemented.
- **Ollama JSON quality is model-dependent.** Smaller / heavily quantized models can produce malformed JSON; `score_job` uses `json_mode=True` to fail loud, and `validate_tailoring` rejects bad shapes from `tailor_resume`. Heavy fallbacks to the deterministic heuristic protect output quality.
- **Google OAuth is labelled "under development"** in the SPA. The flow stays wired but users are routed to email login. Production rollout requires Google Cloud OAuth client credentials AND `PRODUCTION=1` for cookie security.
- **Self-host quirks**: behind a proxy (Tailscale Funnel, Cloudflare), set `PUBLIC_BASE_URL` so Stripe / OAuth redirects use the public origin, and `PRODUCTION=1` so auth cookies are marked `Secure`. The Stripe webhook URL must be publicly reachable.
- **Live job-board coverage is best-effort.** JobSpy and SimplifyJobs change frequently; expect periodic breakage.

---

## License

This project is provided as-is. Add your preferred license here.

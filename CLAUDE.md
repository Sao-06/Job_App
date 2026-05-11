# Jobs AI — Architecture & Agent Guide

Canonical map of the codebase, auto-loaded by Claude Code each session. Keep accurate; deep reference (Stripe runbook, full Bug History, agent hygiene) lives in `docs/CLAUDE_REFERENCE.md`. Fast on-ramp: `AGENTS.md`. Per-task playbooks: §6 below.

---

## 1. Repo Layout (current, verified)

**Top-level entry points**: `agent.py` (CLI orchestrator: `--demo / --ollama / --real-apply / --dashboard`), `app.py` (~5500-line FastAPI backend, primary entry for web UI), `auth_utils.py` (bcrypt + Google OAuth), `session_store.py` (SQLite multi-user persistence + Stripe customer linkage). Config: `requirements.txt`, `pytest.ini`, `.env.example` (Anthropic / Google / SMTP / Stripe / ingestion keys).

**`pipeline/` — CORE LOGIC**:
- `config.py` — `OWNER_NAME`, `OUTPUT_DIR`, `RESOURCES_DIR`, `DATA_DIR`, `DB_PATH`, `DEMO_JOBS`, `migrate_db_path()` (one-shot output→data DB rename at import).
- `helpers.py` — dedup, date/edu/citizenship inference, `infer_job_category`, URL validation.
- `latex.py` — LaTeX detection / plaintext conversion / sanitize + `pdflatex` compile.
- `pdf_format.py` — pdfplumber layout fingerprint (columns / font sizes / accent).
- `phases.py` — `phase1_ingest_resume … phase7_run_report`, `TRACKER_COLUMNS`, `PlaywrightSubmitter` (CLI `--real-apply` only), email notifier.
- `profile_extractor.py` — heuristic-first profile scanner (regex + section parser).
- `profile_audit.py` — post-extraction flatten / quarantine / retention / verify / rerank.
- `heuristic_tailor.py` — LLM-free deterministic tailoring + structural validator (safety net).
- `resume.py` — PDF/DOCX/TEX extraction chain; tailored-resume LaTeX + reportlab render.
- `resume_insights.py` — deterministic resume metrics + optional LLM verification.
- `providers.py` — `BaseProvider`, `AnthropicProvider`, `OllamaProvider`, `DemoProvider`, `compute_skill_coverage` (deterministic, anchors LLM rubric scorer).
- `job_repo.py` — `job_postings` + FTS5 + `source_runs` schema + upsert/mark_missing/lookup.
- `job_search.py` — `SearchFilters`, `search()` — BM25 + skill_overlap + freshness + title_match rerank, dedupe-by-listing, round-robin diversification.
- `ingest.py` — APScheduler background ingestion worker; parallel boot backfill.
- `user_scoring.py` — persistent per-(user, job) scorer. `score_jobs_for_user(conn, user_id, profile)` bulk-scores every live job for one user using the same `compute_skill_coverage` + RUBRIC_WEIGHTS the lazy `/api/jobs/score-batch` path uses; `score_new_jobs_for_user(...)` is the incremental variant fired by the periodic `_user_scoring_loop` daemon. Writes to `user_job_scores`, read by `job_search.search` so the feed sorts by stored score when present. Triggered by `_kick_user_scoring` on every primary-resume change (`_sync_primary_scalars` and the extraction-bg success path). Coalesced per-user via `_USER_SCORING_LOCKS` so duplicate kicks don't pile up workers.
- `migrations.py` — `ensure_column` / `ensure_index` / `apply_all_migrations` — single source of truth for SQLite schema additions.
- `stripe_billing.py` — lazy Stripe SDK wrapper (Customer, Checkout, Portal, Webhook verify).
- `scrapers.py` — ⚠ STALE; nothing imports it.
- `sources/` — pluggable JobSource providers, auto-register on import. `base.py` (Protocol + `infer_metadata` + `QueryRotator` + `GENERAL_QUERIES`), `registry.py`, `_http.py`. **Keyless**: `github_readme`, `api_{themuse,remoteok,jobicy,himalayas,remotive,arbeitnow,weworkremotely}`. **Keyed (silently skip when env var missing)**: `api_{usajobs,adzuna,reed,jooble,findwork}`. **ATS readers**: `ats_{greenhouse,lever,ashby,workable}`.

**`frontend/`** — `landing.html` (marketing, GET `/`, ~1800 lines, scroll-driven scrubber), `index.html` (SPA shell at GET `/app`, ~6100 lines mostly CSS tokens), `app.jsx` (React 18 SPA, ~9000 lines, **Babel-transpiled IN-BROWSER, no build step**), `hero-bg.png`, `landing/` (frozen demo JSON + sample resume). ⚠ STALE: `index.original.html`, `preview.html`.

**Other**: `dashboard/app.py` (standalone Flask Excel-tracker approval flow, port 5000), `Workflow/job-application-agent.md` (canonical 7-phase / scoring spec), `config/skill_keywords.yaml` (DemoProvider keyword groups), `scripts/{migrate_db.py,setup_stripe.py}`, `tools/freeze_landing_demo.py`, `tests/` (pytest unit + integration + Stripe fakes, 487+ tests).

**Runtime dirs**:
- `data/jobs_ai_sessions.sqlite3` — ALL persistent state (users, sessions, session_state, auth_tokens, job_postings + FTS5, source_runs, user_job_scores). Lives OUTSIDE `OUTPUT_DIR` so `/output/{path}` can't serve it.
- `resources/profile_cache_*.json` — phase-1 cache keyed on md5(resume_text + provider + model + titles).
- `output/` — user-visible artifacts ONLY (suffix-allow-listed by `GET /output/{path}`); `Job_Applications_Tracker_*.xlsx`, `*_run-report.md` (CLI-only), `sessions/{session_id}/` (per-session tailored resumes + uploaded previews).
- `.claude/` (local config), `.github/workflows/test.yml` (CI: pytest on push/PR).

> ⚠ Stale files (kept for git history, not wired): `db.py`, `jobs.db`, `check_errors.py`, `pipeline/scrapers.py`, `frontend/index.original.html`, `frontend/preview.html`. README's `streamlit_app.py` reference is stale — UI is FastAPI.
>
> ⚠ DB path migration: SQLite moved `output/` → `data/` (commit `4debfc9` era). `migrate_db_path()` runs at import. `JOBS_AI_SKIP_MIGRATION=1` to neutralize in tests.

---

## 2. Backend (`app.py`)

### Session model
- `_SessionStateProxy` (`_S`) — `MutableMapping` over `contextvars.ContextVar` for per-request isolation; routes use `_S["…"]` like a dict.
- `_bind_request_state(request)` (in `session_state_middleware`) resolves `dev_impersonate_id` (localhost only) → `jobs_ai_session` cookie → fresh UUID, then `_load_session_state` (memory for anonymous; SQLite via `peek_state` for known sessions — never INSERTs for an anonymous request).
- `_default_state()` is the schema. Defaults: `mode="ollama"`, `ollama_model=DEFAULT_OLLAMA_MODEL` (resolves to the `DEFAULT_OLLAMA_MODEL` env var if set, otherwise `CLOUD_OLLAMA_MODEL = "gemma4:31b-cloud"` while we're in the everyone-is-Pro testing phase), `citizenship_filter="all"` (broadest, least-presumptuous), `max_scrape_jobs=50`. Search prefs (`job_titles`, `location`, `experience_levels`, `education_filter`) start **EMPTY** and are filled from the resume after Phase 1. Canonical model constants at top of `app.py`: `LOCAL_OLLAMA_MODEL` and `CLOUD_OLLAMA_MODEL` both equal `"gemma4:31b-cloud"` during the testing phase — once the auto-promote-to-Pro migration is lifted, point `LOCAL_OLLAMA_MODEL` back at a small open-weight model so the free tier has a real local fallback.
- `_save_bound_state()` only persists when `state.user.id|email` is set. Anonymous sessions stay in `_memory_sessions` (kills the ghost-profiles failure mode). Non-GET writes flush at middleware exit; GETs, `/api/state`, `/api/webhooks/stripe` are in `skip_save`.
- `json_default` handles `set`/`Path`; `normalize_state` re-hydrates set-typed keys (`done`, `liked_ids`, `hidden_ids`, `extracting_ids`) and int-keyed dicts (`error`, `elapsed`) on load.

### Auth (`auth_utils.py` + `auth_tokens` table)
- Email/password: bcrypt hash in `users.password_hash`. Session bearer token is **SHA-256 digested** before storage (`session_store._hash_token`) — a DB read alone cannot impersonate. Boot purge deletes `auth_tokens.token` rows that aren't 64 hex chars (legacy raw tokens force one re-login).
- Cookies (`HttpOnly`, `SameSite=Lax`, `Secure` only when `PRODUCTION` truthy): `jobs_ai_auth` (bearer, looked up via hashed digest), `jobs_ai_session` (state id), `dev_impersonate_id` (localhost-only "view as").
- Google OAuth in `auth_utils.py`. Dummy dev flow only fires when `GOOGLE_OAUTH_DEV_DUMMY=1` is explicitly set (it used to fire whenever `GOOGLE_CLIENT_ID` was unset — unsafe). Callback verifies `state` via `secrets.compare_digest`. `/api/auth/google` calls `_save_bound_state` explicitly (GET handlers skip the middleware save).
- **Server-side invariant**: `_require_auth_user(request)` 401s any caller without a valid `jobs_ai_auth` cookie. Applied to every write endpoint AND every read endpoint returning user-specific data (`/api/resume/*`, `/api/profile{,/extract,/diagnose}`, `/api/config`, `/api/reset`, `/api/jobs/{action,ask,feed}`, `/api/atlas/chat/stream`, `/api/feedback`, `/api/billing/{checkout,portal}`). Public: `/api/state`, `/api/jobs/facets`, `/api/auth/*`, `/api/webhooks/stripe`. Frontend Auth gate is necessary but NOT sufficient — server enforces independently.

### Dev mode
- A request is "dev" iff `users.is_developer=1` OR caller's email is in `_DEV_EMAILS` inside `_is_underlying_dev_request` (currently `jonnyliu4@gmail.com`, `saosithisak@gmail.com` — auto-promoted on signin without flipping the DB flag).
- `LOCAL_DEV_BYPASS=1` grants dev to loopback callers (LOCAL ONLY — would grant dev to anyone behind a prod proxy).
- `force_customer_mode` hides Dev Ops UI but keeps `_is_underlying_dev_request` true — powers "Test as Customer" without locking the dev out. Cleared on every fresh login.
- `/api/dev/*` gated by `_is_dev_request` (or `_is_underlying_dev_request` for endpoints a customer-simulating dev still needs, e.g. `/api/dev/runtime`). No `/api/dev/toggle-role` endpoint — flip the DB flag via `_user_store.set_user_developer(uid, True)`; use Dev Ops Sessions → PLAN panel for plan tier.

### Plan tier (billing)
- `users.plan_tier ∈ {'free','pro'}` (default `free`). Mirrors `is_developer`: DB → `auth_user` dict → `/api/state` → `state.plan_tier`/`state.is_pro`.
- **Pricing model**: Free = local Ollama (small open-weight). Pro = cloud Ollama (same daemon proxies `*-cloud` model names to Ollama Turbo's hosted servers).
- **Gates**: `POST /api/config` returns **402 `plan_required`** for non-Pro/non-dev setting `*-cloud` Ollama models, and **503 `coming_soon`** for non-dev setting `mode='anthropic'`. Belt-and-suspenders mirror gates inside `_run_phase_sse` and `POST /api/resume/tailor`. `_load_session_state` migrates non-dev sessions off `mode='anthropic'` and free users off `*-cloud` models on every load.
- **Anthropic launch state (2026-05)**: the underlying integration is launch-quality (Opus 4.7 + adaptive thinking + prompt caching + `output_config.effort` — see Providers §3). The customer-facing gate stays **dev-only** for now — no plan-tier flip yet. To open Anthropic to Pro tier later: replace the `is_developer` check in the four gate sites (`_load_session_state`, `update_config`, `_run_phase_sse`, `resume_tailor`) with `is_developer or plan_tier == 'pro'`, update the SPA `Soon` pill in TailorDrawer, and update the `Plans` page Anthropic copy. Sanity-check the configured key with `python scripts/check_anthropic_key.py` before each deploy — it hits `models.retrieve` (free, no completion tokens) and reports failure modes.
- **Manual Pro flip** (ops escape): `_user_store.set_user_plan_tier(uid,'pro')` or Dev Ops Sessions → PLAN → GRANT PRO (`POST /api/dev/users/{user_id}/plan` with `{tier}`). Both go through `_apply_plan_change(uid, tier)`, the single source of truth that writes the DB column AND refreshes every cached `auth_tokens.user_json` so the next `/api/state` poll reflects the change without re-login.

### Stripe billing (Pro $4/month) — ⚠️ backend wired, frontend parked
- Backend fully implemented in `pipeline/stripe_billing.py` (lazy SDK wrap) + `/api/billing/{checkout,portal}` + `POST /api/webhooks/stripe` (HMAC-verified, in middleware `skip_save` list). SPA Plans page still calls the `requestUpgrade` stub at `frontend/app.jsx:7113`; swap to `startCheckout`/`openPortal` to go live.
- **Webhook is the source of truth for `plan_tier`** — not the success-URL redirect. `/api/state` reports `free` until the webhook fires (~5s). `_apply_plan_change` writes the DB column AND refreshes every cached `auth_tokens.user_json`.
- Full setup runbook + endpoint contract + handler semantics live in `docs/CLAUDE_REFERENCE.md` §8.

### Route inventory (high-level)
- **Static**: `GET /` (landing), `GET /app` (SPA, JSX url stamped with file mtime), `GET /frontend/{path}` (path-traversal-clipped + `no-cache`), `GET /output/{path}` (sandboxed: suffix allow-list `.pdf/.tex/.docx/.txt/.md/.log/.xlsx/.xls/.csv/.png/.jpg/.svg/.html`, block-list `.sqlite*/.env/.pem/.key/.json/.yaml`, per-session paths require auth+session match).
- **Auth**: `POST /api/auth/{login,signup,logout}`, `GET /api/auth/google[/callback]`.
- **Resume**: `POST /api/resume/{upload,demo,text,primary/{id},rename/{id},tailor,{id}/render-preview}`, `GET /api/resume/content?id=`, `DELETE /api/resume/{id}`. `tailor` = per-job on-demand (same shape as one phase-4 row); `render-preview` back-fills the polished preview PDF for legacy records.
- **Config / state**: `POST /api/config` (whitelisted keys only — see below), `GET /api/state`, `POST /api/reset`.
- **Profile**: `GET /api/profile`, `POST /api/profile`, `POST /api/profile/extract` (`{resume_id?, preferred_titles?, force?}`), `GET /api/profile/diagnose?id=` (heuristic-only diagnostic — no LLM call).
- **Phases (SSE)**: `GET /api/phase/{1..7}/{run,rerun}`. Phase 2 accepts `?deep=1&append=1&force=1` query params; Phase 3 accepts `?fast=1`. Phase 2 also has `GET /api/phase/2/cache` and `DELETE /api/phase/2/cache` for the legacy quick/deep JSON caches under `resources/`.
- **Jobs (live index)**: `GET /api/jobs/feed?cursor=&limit=&q=&exp=&edu=&cit=&remote=&days=&location=&industry=&since_id=&blacklist=&whitelist=` (cursor-paginated, profile-ranked, pulls from `job_postings` via `pipeline.job_search.search`). For authenticated users with a primary resume the rerank prefers the stored 0–100 score from `user_job_scores` (populated by `pipeline.user_scoring`) so the very top matches surface first regardless of recency / BM25 noise. Falls back to the BM25 + skill_overlap + freshness + title_match composite for users without stored rows yet. `GET /api/jobs/facets?kind={industry,location,company}&q=&limit=` (PUBLIC — pure aggregate counts, no PII). `POST /api/jobs/action` (`like|unlike|hide|unhide`). `GET /api/jobs/source-status` / `POST /api/jobs/source-status` (force one or all sources to re-tick — dev-only).
- **Ask Atlas**: `POST /api/jobs/ask` (per-job advisor — `{job_id, message, history?}` → `{reply}`, synchronous). `POST /api/atlas/chat/stream` (career-wide streamer — same body, returns SSE chunks `{type: 'start' | 'delta' | 'done' | 'error'}`).
- **Feedback**: `POST /api/feedback` (`{message}`).
- **Ollama**: `GET /api/ollama/status` (probe daemon + list pulled models + report any in-flight pull), `POST /api/ollama/ensure` (idempotent — pull session model if missing), `GET /api/ollama/pull` (SSE — stream a pull for the session-configured model).
- **Billing (Stripe)**: `POST /api/billing/checkout` (auth-gated; 503 until `STRIPE_PRICE_ID_PRO_MONTHLY` set; 409 if already Pro), `POST /api/billing/portal` (404 if no `stripe_customer_id`), `POST /api/webhooks/stripe` (HMAC-verified; in middleware `skip_save` list).
- **Dev Ops** (`/api/dev/*`): `overview`, `metrics?with_processes=1` (psutil), `logs?since=&limit=` + `logs/stream` (SSE), `users`, `POST users/{user_id}/plan` (`{tier}`), `session/{id}` (GET/reset/delete/impersonate/`feedback/read`), `POST session/stop-impersonating`, `cli` (whitelist: `git_status`, `pip_freeze`, `recent_outputs`, `session_db`), `tweaks`, `GET/POST runtime` (toggle `maintenance`/`verbose_logs`), `POST reload-env`.

### `/api/config` whitelist
Adding a tunable setting requires updating BOTH `_default_state()` AND the whitelist tuple in `update_config`. Current keys: `mode`, `api_key`, `ollama_model`, `threshold`, `job_titles`, `location`, `max_apps`, `max_scrape_jobs`, `days_old`, `cover_letter`, `blacklist`, `whitelist`, `experience_levels`, `education_filter`, `include_unknown_education`, `include_unknown_experience`, `citizenship_filter`, `use_simplify`, `llm_score_limit`, `force_customer_mode`, `light_mode`. Plan-gate: `mode='anthropic'` and any `*-cloud` Ollama model are 402'd for non-Pro non-dev callers (also gated belt-and-suspenders inside `_run_phase_sse`).

### `/api/reset` semantics
Wipes session state to defaults but **preserves** `user`, `dev_tweaks`, `mode`, `api_key`, `ollama_model`, `light_mode`, `force_customer_mode`. Also `shutil.rmtree`s `output/sessions/{session_id}/`. User stays logged in. Holds the per-session lock for the entire clear+persist.

### Server-side log mirror & live metrics
`_LOG_RING` is a 3000-line `collections.deque` fed by `_StreamTee` (wraps `sys.stdout/stderr`) and `_RingLogHandler` (Python `logging`). `/api/dev/logs/stream` fans out new records via SSE; `/api/dev/logs` returns a windowed snapshot. CPU/memory/top-procs/temp come from psutil with 800ms/2.5s snapshot caches. Per-core sampler primes in `@app.on_event("startup")` (NOT at import — that ran during pytest collection).

---

## 3. Pipeline (`pipeline/phases.py`)

Each phase is a function that mutates `_S` (via the proxy) and emits log lines to the SSE stream. SSE plumbing in `_run_phase_sse` claims a per-session phase slot (cap 1), runs `fn` in a daemon thread with stdout teed into a queue, and emits `start | log | done | error` events. Pre-checks: `_RUNTIME["maintenance"]` short-circuits with an error event; an `mode='anthropic'` caller who isn't a developer is rejected with `code: "coming_soon"` (belt-and-suspenders against the `/api/config` 503 — Claude is still under development).

| Phase | Function | What it does |
|---|---|---|
| 1 | `phase1_ingest_resume` | Heuristic-first: `profile_extractor.scan_profile` (regex+section parse) → LLM `extract_profile(text, preferred_titles, heuristic_hint=…)` verifies/corrects → `merge_profiles` → `profile_audit.audit_profile` (flatten / quarantine misplaced soft-skills / retention / verify evidence / rerank titles) → `resume_insights.analyze_resume` (deterministic metrics + optional LLM verify). Cached by md5(resume_text + provider + model + titles) under `resources/profile_cache_*.json`. |
| 2 | `phase2_discover_jobs` | **Reads the local `job_postings` index** via `job_search.search` — does NOT scrape live. `?force=1` or `?append=1` runs `pipeline.ingest.force_run()` synchronously first. Empty index → `provider.generate_demo_jobs` fallback. All user filters (blacklist/whitelist/citizenship/experience/education/days_old/location) are pushed into the SQL WHERE so the cap reflects post-filter results. |
| 3 | `phase3_score_jobs` | Two-stage. `compute_skill_coverage(job, profile)` is deterministic and the LLM **cannot** override it (50%-weighted, pure math: token-aware scan of title+requirements+description, requirements are the denominator). `_fast_score` ranks all jobs via the same helper; top `llm_score_limit` (default 10) get `provider.score_job` for qualitative `industry`+`location_seniority`, glued via `_build_rubric_result`. Pre-filters experience (with `include_unknown_experience` passthrough since most ingested rows are metadata-only) and citizenship. `?fast=1` skips LLM rerank. Status: `passed \| below_threshold \| filtered_experience \| filtered_citizenship`. |
| 4 | `phase4_tailor_resume` | Always computes `heuristic_tailor.heuristic_tailor_resume` baseline first. LLM result runs through `validate_tailoring` (rejects wrong-shape JSON from low-end Ollama models); reject→retry once→fallback to heuristic. `merge_with_heuristic` keeps LLM's good fields, backfills empties. **Anti-fabrication**: `skills_reordered` only reorders existing skills (missing JD keywords → `ats_keywords_missing`); `experience_bullets` only reorders existing bullets, never invents. Before/after ATS via `_ats_score`. Output via `_save_tailored_resume` (`.tex`+`.pdf`). |
| 5 | `phase5_simulate_submission` | **Web flow = curation, NOT auto-submit.** Top-N (`llm_score_limit`) labeled `Manual Required`; rest `Skipped`. Already-applied items skipped via current month's tracker. The randomized "Applied" coin-flip stub was deliberately removed; never reintroduce. CLI `--real-apply` opts into `PlaywrightSubmitter` (Greenhouse only, falls through to simulation). |
| 6 | `phase6_update_tracker` | Returns `{month, columns, rows, summary}` from `TRACKER_COLUMNS` (single source for both UI and xlsx). Web: `write_file=False`, renders inline. CLI: writes `Job_Applications_Tracker_{YYYY-MM}.xlsx` via openpyxl with status color-coding + frozen header + auto-fit columns + Summary Dashboard tab. |
| 7 | `phase7_run_report` | `provider.generate_report(summary_data)` → markdown. Web inline; CLI writes `{YYYYMMDD}_job-application-run-report.md`. SMTP notification fires only when ALL of `SMTP_HOST/PORT/USER/PASS/NOTIFY_EMAIL` are set. |

### Providers (`pipeline/providers.py`)
- **`BaseProvider`** methods: `extract_profile(text, preferred_titles, heuristic_hint)`, `score_job(job, profile)`, `tailor_resume(job, profile, text)`, `generate_cover_letter`, `generate_report`, `generate_demo_jobs`, `chat(system, messages, max_tokens, json_mode)`. The `heuristic_hint` kwarg anchors the heuristic-first design — LLM providers use it as a verified baseline.
- **`AnthropicProvider`** — `claude-opus-4-7` via SDK. Adaptive thinking only (`budget_tokens` is removed on 4.7). `output_config.effort` defaults to `"high"`; tailoring uses `"xhigh"` (best balance for agentic / long-output rewriting on 4.7). Forced tool-calling for strict JSON in `extract_profile`/`score_job`/`tailor_resume`. Tailoring system block carries `cache_control: ephemeral` so per-job tailoring inside a session reads the cached prefix from the second job onward. JSON mode in `chat()` uses `output_config.format = json_object` (the legacy assistant-turn `{` prefill is removed — Opus 4.6+ returns 400 on it). Streaming used by `_stream_provider_chat` for career-wide Atlas.
- **`OllamaProvider`** — OpenAI-compatible client against `OLLAMA_URL` (default `http://localhost:11434`). `_check_ollama` bypasses the local-pull check for `*-cloud`/`:cloud` models (Ollama Turbo). `score_job` uses `json_mode=True` so parse failures stop degrading to neutral 0.5/0.5.
- **`DemoProvider`** — pure-Python regex/section parsing (`_split_sections`, `_parse_{education,experience,projects}_block` — reused by `profile_extractor` for the heuristic baseline). Hard skills from `config/skill_keywords.yaml`; soft skills from text via `_scan_soft_skills` (no hardcoded list — previous "Teamwork/Problem-solving/…" default was a bug, see Bug History).

### Resume extraction fallback chain (`pipeline/resume.py`)
`pypdfium2` → `pdfplumber` → `pypdf` → `pdfminer.six`. Always verify `pypdfium2` import is intact when touching this file. `_normalise_pdf_text` cleans ligatures (`ﬁ → fi`), curly quotes, and re-joins lines broken mid-word. `.docx` reads paragraphs AND table cells (sidebar layouts put content in tables).

### PDF generation
1. `pdflatex` if on `PATH` — preceded by `pipeline.latex._sanitize_latex_source` which strips `\input`, `\write18`, `\openin`, `\catcode`, `\directlua`, etc. so a malicious LaTeX upload can't read `/etc/passwd` or shell out.
2. `pipeline.resume._render_resume_pdf_reportlab` fallback. Honours `pdf_format` fingerprint (column count, body/header font sizes, accent color) computed by `pipeline.pdf_format.detect_format_profile` at upload time so generated tailored PDFs mirror the source aesthetic.

## 3a. Job ingestion subsystem

A background sub-system keeps `data/jobs_ai_sessions.sqlite3:job_postings` fresh.

**Sources (`pipeline/sources/`)** — Each provider implements the `JobSource` Protocol (`name`, `cadence_seconds`, `timeout_seconds`, `fetch(since) -> Iterator[RawJob]`) and self-registers on import. Keyed sources skip registration silently when env var absent. ATS readers iterate a curated slug list (~60 international + 19 Hong Kong). `QueryRotator` cycles a long query list `batch_size` at a time so a keyed source covers many job families without burning daily quota. `infer_metadata` runs experience/education/citizenship/category inference before upsert.

**Worker (`pipeline/ingest.py`)** — `start_scheduler(connect)` is wired into FastAPI startup. Spawns an APScheduler `BackgroundScheduler` (UTC) with one `IntervalTrigger` per source. Per-source `threading.Lock` prevents reentrancy. `run_one(source)`: fetch → `infer_metadata` → `job_repo.upsert_many` → `mark_missing` (`miss_count >= 3` flips `deleted=1`) → `record_source_run`. Failures isolated per source. Boot-time backfill is parallel across sources with a 60 s wall clock, **skipped** when `source_runs.MAX(started_at) < 60 min ago` (restarts shouldn't re-fire 8 concurrent upserters). `JOBS_AI_DISABLE_INGESTION=1` skips the scheduler entirely.

**Repo (`pipeline/job_repo.py`)** — `canonical_url(url)` lowercases host, strips utm/gh_src/fbclid/gclid, drops fragment + trailing slash; `id = sha256(canonical_url)[:16]`. Upserts COALESCE thinner refreshes so they don't overwrite richer prior data; salary CASEs to ignore `Unknown`. FTS5 virtual `job_postings_fts` with porter+unicode61 tokenizer over `title, company, requirements`, kept in sync via three triggers. `mark_missing` is per-source so an offline provider can't soft-delete its peers' rows.

**Search (`pipeline/job_search.py`)** — `search(conn, filters, profile, cursor, limit, rank_pool, dedupe, per_round, user_id=None)`. Pipeline: (1) SQL WHERE on `job_postings` (deleted=0 + experience/education/remote/posted_at/location/citizenship/blacklist/categories). (2) Optional FTS5 MATCH from user `q` + profile terms (target_titles + top_hard_skills, ≤24 tokens). (3) When `user_id` is supplied, LEFT JOIN `user_job_scores` and prepend `ujs.score DESC` to the ORDER BY so the SQL pool surfaces stored-score rows first. (4) `ORDER BY [ujs.score DESC,] bm25_score, posted_at DESC` with `LIMIT max(rank_pool, (cap+offset)*6)` capped at 8000. (5) Python rerank: when stored `user_score` present, `0.80*user_score + 0.10*freshness + 0.10*bm25`; otherwise fall back to `0.45*bm25 + 0.30*skill_overlap + 0.15*freshness + 0.10*title_match` (+0.25 on whitelist substring match). (6) `_dedupe_by_listing` collapses cross-source / multi-city dupes by `(company_norm, title_norm, location_first_token)`; title-norm strips year/season/work-model/intern markers. (7) `_diversify_by_category` (cold no-profile feed only) round-robins industries so first page isn't all-tech. (8) `_diversify_by_company` round-robin (default `max_per_round=1`). (9) Cursor = base64 `{score, last_id, page_offset}`; `newer_than(top_id)` powers the 25 s SPA poll.

**Per-user scoring (`pipeline/user_scoring.py`)** — Runs in parallel with ingestion as a separate daemon thread. `_kick_user_scoring(uid, profile, only_new=False)` is fired on every primary-resume change (`_sync_primary_scalars` and the extraction-bg success path); it spawns a daemon thread that calls `score_jobs_for_user(conn, uid, profile)` to write a 0–100 INTEGER row per (user, job) into `user_job_scores`. Coalesced per-user via a `threading.Lock` dict so duplicate kicks don't pile up workers. The `_user_scoring_loop` daemon runs `score_new_jobs_for_user` for every user with at least one stored row every 10 minutes, picking up jobs the ingestion scheduler has added since the user's last refresh. Gated by `JOBS_AI_DISABLE_USER_SCORING=1` for tests. The same `compute_skill_coverage` + RUBRIC_WEIGHTS the lazy `/api/jobs/score-batch` endpoint uses; title_match uses the broadened target_titles fallback (work_experience titles when LLM extraction yielded none), and location_seniority compares against the profile's actual location + configured `experience_levels` rather than a flat 0.5.

**Migrations (`pipeline/migrations.py`)** — The `try: ALTER TABLE…; except sqlite3.OperationalError: pass` pattern is **forbidden** — it silently swallowed real failures on the Pi for months. `apply_all_migrations(conn)` is the single source of truth: `ensure_column(conn, table, col, type_decl, default=…)` uses `PRAGMA table_info`, ALTERs only when missing, re-raises anything that's not a duplicate-column race. Runs both in `SQLiteSessionStore._init_db` and `app.py`'s `_ensure_schema_migrations` startup hook (belt-and-suspenders). Failures are loud (stderr → journalctl). `scripts/migrate_db.py [--check]` for out-of-band runs.

---

## 4. Frontend (`frontend/app.jsx` — ~9000 lines)

- React 18 SPA, **Babel-standalone transpiled in-browser** (`<script type="text/babel">`). No build step — edits to `app.jsx` go live after a HARD refresh. `/app` stamps the JSX URL with file mtime (`?v={mtime}`) to bust caches across restarts.
- Single root state from `/api/state`, refreshed via `refresh()` on an **adaptive poll**: 2 s while any resume is extracting, 8 s otherwise. **Polling is gated on `state.user`** — pre-login races would clobber OAuth state set by a concurrent `/api/auth/google`.
- **Hash-based routing** via `history.pushState`. Pages: `home | jobs | resume | profile | agent | settings | feedback | dev | plans | auth | onboarding`. Empty hash = home. Auth gate routes anonymous to `auth`; onboarding gate routes authed-without-resume to `onboarding`.
- `api` helper wraps `fetch` with `AbortController` (30 s default, 90 s for `/api/resume/tailor`), unwraps `{detail, error, message}`, exposes `get/post/upload/delete`.
- `runPhaseSSE(n, …)` opens `EventSource` against `/api/phase/n/{run|rerun}` and dispatches `start|log|done|error`; cancels on unmount/navigation.
- `/api/atlas/chat/stream` uses **manual `ReadableStream` parsing** (NOT `EventSource`) so it can be cancelled mid-flight via the same `AbortController`.

### Pages
- **home** — hero + CockpitStrip HUD (status, greeting, live clock, streak, phase, high-fit count), quick actions, Resume Intelligence dossier.
- **jobs** — feed (paginates `/api/jobs/feed`), facets via `/api/jobs/facets` (industry/location/company), 65-entry curated location quick-pick, 250 ms-debounced search, per-job AskAtlas + "Tailor for this job" drawers (90 s timeout), 25 s polling tick via `since_id`.
- **resume** — multi-resume manager: upload (drag/drop/paste), original PDF or generated preview, AI insights tabs (overview/metrics/insights/deep-dive), edit/set-primary/rename/delete/re-scan. Per-upload extraction polls tracked in a Set, cleared on unmount.
- **profile** — auto-save (700 ms debounce) form. `AutoSaveBadge` (idle/pending/saving/saved/error). `inFlightRef` + save-snapshot diff handles the keep-typing-during-save race; unmount flushes. `ProfileSelect` covers the 17 canonical US work-auth options.
- **agent** — 7-phase orchestrator with circular progress ring. Inline `agent-tune` slider for `max_scrape_jobs`. Career-wide AtlasChat sidebar via `/api/atlas/chat/stream`.
- **settings** — LLM backend, Ollama model picker (`/api/ollama/status`), search prefs, filters, threshold, cover-letter mode, job-discovery section (max_scrape_jobs, days_old).
- **plans** — Free (local Ollama) vs Pro (cloud Ollama) cards; Anthropic shown as "coming soon" in both. Currently calls `requestUpgrade` stub (admin manually flips `plan_tier`); swap to `startCheckout`/`openPortal` inside `PlansPage` to go live.
- **feedback** — textarea → `POST /api/feedback`.
- **dev** — sessions table, live metrics (2 s tick), live server log SSE, plan-tier editor, restricted CLI. Hidden from non-dev users by `_is_dev_request` server-side AND `state.is_dev` client-side.
- **auth** — email/password + Google OAuth redirect (labelled "under development").
- **onboarding** — first-time guided flow (resume upload + demo). Requires `state.user` first.

### Shared utilities
- **`api`** helper, **`runPhaseSSE`**, **`streamAtlasChat`** (manual SSE parser).
- **`Icon`** wraps `lucide` icons via `data-lucide` + `window.lucide.createIcons()`.
- **`Rail`** — sidebar nav, mobile drawer (Escape / click-outside / body-scroll lock).
- **`CompanyLogo`** — Clearbit → Google favicon → letter monogram fallback.
- **`Markdown`** — used by AskAtlas replies and the Phase 7 report.
- **`ChipToggle`** — multi-select filter chip component.
- **`ProfileSelect`** — single-select with custom chevron + light-theme override.

### Theming
- All colors are CSS custom properties on `:root` in `index.html` and `landing.html`. `:root[data-theme="light"]` overrides them; `light_mode` toggles `document.documentElement.dataset.theme` via a `useEffect` that skips no-op writes (polling doesn't churn the attribute).
- Brand (deep ink): `--accent: #7c5cff` (electric violet), `--accent2: #22e5ff` (cyan), `--accent3: #ff3d9a` (magenta). Status: `--good: #3dff9a`, `--warn: #ffd23d`, `--bad: #ff4d6d`.
- `state.dev_tweaks` injects CSS custom properties on `<html>` per poll (banner / density / accent override / experiment flag).
- **No horizontal page scrolling, anywhere.** Set `overflow-x: hidden` AND `min-width: 0` on grid children — `overflow-y: auto` alone auto-promotes the other axis to `auto` per the CSS Overflow spec (Bug History).

### Gating logic (App component)
1. **Booted gate** — wait for first `/api/state` response.
2. **Auth gate** — every page requires `state.user`. Anonymous visitors only ever see `<AuthPage/>`.
3. **Onboarding gate** — authenticated users without a resume see `<Onboarding/>`. **Must require `state.user` first**, otherwise unauthenticated uploads create ghost profiles in the Dev Ops list. The server enforces this independently via `_require_auth_user` on `/api/resume/{upload,demo}`.

---

## 5. Persistence (`session_store.py`)

- SQLite at **`data/jobs_ai_sessions.sqlite3`** (WAL mode where supported). Lives outside `OUTPUT_DIR` so the static route can never serve it. `migrate_db_path()` renames legacy `output/jobs_ai_sessions.sqlite3*` (with WAL/SHM siblings) at import — bails out if a live writer is detected.
- **Corruption quarantine**: `_quarantine_if_corrupt` runs `PRAGMA quick_check` before opening; on failure renames the file with a `.corrupt-{ts}` suffix and rebuilds. Recovery cost: users + auth tokens + session state lost (re-sign-in, resume re-extract, job index re-fills in minutes). Better than every `/api/state` 500ing.
- **Tables**:
  - `users(id PK, email UNIQUE, password_hash, google_id, is_developer, plan_tier, stripe_customer_id, stripe_subscription_id, created_at)`
  - `sessions(id PK, user_id FK, created_at, updated_at)`
  - `session_state(session_id PK→sessions, state_json TEXT, updated_at)`
  - `auth_tokens(token PK [SHA-256 digest], user_id FK, user_json, created_at)`
  - `job_postings(id PK, canonical_url UNIQUE, source, company, company_norm, title, title_norm, location, remote, requirements_json, salary_range, experience_level, education_required, citizenship_required, job_category, posted_at, fetched_at, last_seen_at, miss_count, deleted)` + indexes (deleted+last_seen_at / +posted_at / +experience+education+remote / +job_category, plus company_norm, source) + FTS5 virtual `job_postings_fts(title, company, requirements)` with `_ai/_ad/_au` triggers.
  - `source_runs(source, started_at, finished_at, ok, fetched, inserted, updated, error, PK(source, started_at))` + index `(source, started_at DESC)`.
  - `user_job_scores(user_id, job_id, score, coverage, title_match, loc_sen, matched_json, missing_json, profile_hash, computed_at, PRIMARY KEY(user_id, job_id))` + indexes `(user_id, score DESC)` / `(user_id, computed_at DESC)` / `(job_id)`. Persistent per-user match cache; `0..100` INTEGER scores written by `pipeline.user_scoring.score_jobs_for_user`. `/api/jobs/feed` LEFT JOINs this table by `user_id` and prefers the stored score over the BM25 composite when present.
- **Plan-tier propagation** (`refresh_user_plan_in_tokens`): on Stripe webhook flip, every persisted `auth_tokens.user_json` for that user is rewritten so the next `/api/state` poll reflects it without re-login. `_AUTH_SESSIONS_FALLBACK` cache refreshed in lockstep.
- **State proxy contract**: `default_state_factory = _default_state` is injected at construction — adding a new state key only requires updating that one function. Adding a new SET-typed key requires updating both `normalize_state` and `_load_session_state` so it survives the JSON round-trip.
- **Anonymous sessions never INSERT**: `peek_state()` is read-only; `_save_bound_state` early-returns to `_memory_sessions` when `user.id|email` unset. `list_sessions` filters `WHERE user_id IS NOT NULL`.

---

## 6. Common Tasks (Playbooks)

### Add a new user-tunable setting
1. Add the key + default to `_default_state()` in `app.py`.
2. Add the key to the whitelist tuple inside `update_config` (`POST /api/config`).
3. Add the key to the dict returned from `GET /api/state`.
4. Add the UI in `frontend/app.jsx` `SettingsPage` — usually a `<Toggle field="…"/>`. Toggle persists via `update({ key: value })` which posts `/api/config` and triggers a refresh.

### Add a new phase or modify scoring
1. Edit the relevant function in `pipeline/phases.py`.
2. If LLM-shape changes, update the JSON schema in `pipeline/providers.py` for **all three** providers.
3. If output files change, update the file-listing block in `GET /api/state` (it globs `_session_output_dir()`).

### Add a new SPA page
1. Add `case 'newpage': return <NewPage …/>` in the page switch in `App()`.
2. Add a rail item; the `Rail` component reads counts from props.
3. If the page hits a new endpoint, add it under `/api/…` in `app.py`.

### Touch the Auth gate
- Don't loosen it without checking the *Onboarding gate* — they cooperate (see §4).
- Frontend `<AuthPage/>` is necessary but NOT sufficient. The server enforces `_require_auth_user(request)` independently on every write endpoint AND every read endpoint that returns user-specific data. If you add a new endpoint that touches `_S` for anything other than read-only public state, gate it with `_require_auth_user`.
- New webhooks (HMAC-verified, no cookie) MUST go in the middleware `skip_save` list so anonymous deliveries don't churn `session_state` rows.

### Add a new column to a SQLite table (so existing prod DBs auto-migrate)
1. Add the column to the `CREATE TABLE` in either `session_store._init_db` (for `users` / `sessions` / `auth_tokens` / `session_state`) or `pipeline/job_repo._SCHEMA_SQL` (for `job_postings` / `source_runs`). This handles fresh DBs.
2. **Add a matching `ensure_column(...)` call to `pipeline/migrations.py:apply_all_migrations`.** This handles existing prod DBs (the Pi).
3. If the column needs an INDEX, add `ensure_index(...)` AFTER the `ensure_column` in the same function. NEVER put the CREATE INDEX in `_SCHEMA_SQL` if it references a not-yet-migrated column — it'll crash on stale DBs (`CREATE INDEX IF NOT EXISTS` still validates referenced columns).
4. Deploy: `git pull && sudo systemctl restart jobapp`. The startup hook `_ensure_schema_migrations` in `app.py` runs migrations on boot and logs each step to journalctl. NEVER manually `sqlite3 ... "ALTER TABLE…"` on the Pi after the first time — the migration framework owns this now.
5. To verify post-deploy: `python scripts/migrate_db.py --check` shows the live schema; without `--check` it runs migrations and reports steps.

---

## 7. Operational Mandates

1. **Secrets**: never commit `ANTHROPIC_API_KEY`, `GOOGLE_CLIENT_SECRET`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, or any `sk-…`/`whsec_…` literal. Anthropic key lives in session state or env var only. `.gitignore` covers `data/*.sqlite3*`, `*.corrupt-*`, `resources/profile_cache_*.json`, `test_cookies.txt`, `test_signup_resp.json`, `server.log`, `.coverage`.
2. **Cross-platform paths**: derive from `Path(__file__)` (Windows + Linux Pi). No hardcoded separators.
3. **Frontend has no build step**: don't introduce webpack/vite/tsc unless you also commit the artefact and update `index.html`. JSX = in-browser Babel; edits live on hard refresh.
4. **PDF engine order**: `pypdfium2` primary. Reordering requires updating `pipeline/resume.py` AND this doc.
5. **Run the server before reporting frontend changes done**: `python app.py` or `uvicorn app:app --reload --port 8000`; exercise at `http://localhost:8000/app`.
6. **Migration discipline (THE LOAD-BEARING ONE)**: never inline `try: ALTER TABLE…; except sqlite3.OperationalError: pass`. Always go through `pipeline/migrations.py:ensure_column` / `ensure_index`. Real failures must surface in `journalctl -u jobapp` — silent swallowing is what bricked the Pi for months.
7. **Provider parity**: changing the JSON schema for `extract_profile` / `score_job` / `tailor_resume` requires updating ALL THREE providers in `pipeline/providers.py`. The validator in `heuristic_tailor.validate_tailoring` is the safety net for low-end models — keep it in sync.
8. **No fabrication in tailoring**: `skills_reordered` only ever reorders existing user skills. Missing JD requirements MUST go in `ats_keywords_missing`, never silently appended to the user's skill list. Same for `experience_bullets` — reorder only, never invent.
9. **No hardcoded defaults that lie about the user**: search prefs (`job_titles`, `location`, `experience_levels`, `education_filter`) start EMPTY and are filled from the resume. Never reintroduce the legacy `Engineer` / `United States` / `bachelors` placeholders — they leaked into SQL filters even when the resume said otherwise.
10. **Phase 5 is curation, not auto-submission** in the web flow. The randomized "Applied" coin-flip stub was deliberately removed; never reintroduce it.
11. **Anonymous sessions never persist**: `_save_bound_state` early-returns to memory when `user.id|email` is unset; `peek_state` is read-only. Touching either of these to "fix" something else will reintroduce the ghost-profiles failure.
12. **Auth-token storage**: `auth_tokens.token` stores the SHA-256 digest, never the raw cookie value. The 64-hex purge on boot enforces this — don't re-introduce raw-token rows.
13. **Stripe webhook is the source of truth** for `plan_tier`, NOT the success-URL redirect. Until the webhook fires, `/api/state` reports `free`. The webhook is HMAC-verified, sits in the middleware `skip_save` list, and routes through `_apply_plan_change` (DB column + every cached `user_json`).
14. **Stale files** kept for git history but not wired in: `db.py`, `jobs.db`, `check_errors.py`, `pipeline/scrapers.py`, `frontend/index.original.html`, `frontend/preview.html`. Leave them alone unless the user asks for cleanup.
15. **Document your work**: detailed commit (root cause / what changed / verification / dead-ends, `═══` or `───` separators per recent commits), CLAUDE.md update for any non-trivial change, Bug History entry for regressions worth remembering. Don't write standalone `SESSION_NOTES.md` / `WORK_LOG.md` — signal goes in commits + CLAUDE.md where `git log` / grep find it.

---

## 8–10. Deep reference (`docs/CLAUDE_REFERENCE.md`)

The detailed Stripe billing runbook (§8), full Bug History with ~16 commit-hash → invariant entries (§9), and the agent session-hygiene playbook (§10) live in **`docs/CLAUDE_REFERENCE.md`** so this file stays under the auto-load threshold. Load that file on demand when:
- Touching Stripe / billing / `plan_tier` — full endpoint contract + webhook handlers + setup steps.
- Triaging a bug that smells like a reintroduced regression — search the Bug History before debugging.
- Preparing a non-trivial commit — the hygiene checklist maps change-type → which docs to update.

When you fix a bug worth remembering, add an entry to the Bug History in `docs/CLAUDE_REFERENCE.md`: commit hash, symptom, root cause, invariant. The point is to keep the same bug from being reintroduced six months from now.


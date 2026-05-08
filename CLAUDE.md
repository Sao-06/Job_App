# Jobs AI — Architecture & Agent Guide

This file is the canonical map of the codebase. It is consumed by Claude Code (and other AI agents) on every session. Keep it accurate when you change architecture; do not let it drift.

For a fast "where do I start" guide, see `AGENTS.md`. For per-task playbooks, see the *Common Tasks* section below.

---

## 1. Repo Layout (current, verified)

```text
Job_App/
├── agent.py                    # CLI orchestrator (--demo / --ollama / --real-apply / --dashboard).
├── app.py                      # FastAPI backend (~5500 lines). Primary entry point for the web UI.
├── auth_utils.py               # bcrypt password hashing + Google OAuth helpers.
├── session_store.py            # SQLite-backed multi-user session/state persistence + Stripe customer.
├── requirements.txt
├── pytest.ini
├── .env.example                # Anthropic / Google / SMTP / Stripe / ingestion API keys.
│
├── pipeline/                   # CORE LOGIC.
│   ├── __init__.py             # Public re-exports.
│   ├── config.py               # OWNER_NAME, OUTPUT_DIR, RESOURCES_DIR, DATA_DIR, DB_PATH, console, DEMO_JOBS,
│   │                           # migrate_db_path() (one-shot output→data DB rename).
│   ├── helpers.py              # Dedup, date/edu/citizenship inference, infer_job_category, URL validation.
│   ├── latex.py                # LaTeX detection / plaintext conversion / sanitize + pdflatex compile.
│   ├── pdf_format.py           # pdfplumber-based layout fingerprint (columns / font sizes / accent).
│   ├── phases.py               # phase1_ingest_resume … phase7_run_report, TRACKER_COLUMNS,
│   │                           # PlaywrightSubmitter (CLI --real-apply only), email notifier.
│   ├── profile_extractor.py    # Heuristic-first profile scanner (regex + section parser).
│   ├── profile_audit.py        # Post-extraction flatten / quarantine / retention / verify / rerank.
│   ├── heuristic_tailor.py     # LLM-free deterministic tailoring + structural validator (safety net).
│   ├── resume.py               # PDF/DOCX/TEX extraction chain; tailored-resume LaTeX + reportlab render.
│   ├── resume_insights.py      # Deterministic resume metrics + optional LLM verification (insights v1).
│   ├── providers.py            # BaseProvider, AnthropicProvider, OllamaProvider, DemoProvider,
│   │                           # compute_skill_coverage (deterministic, anchors LLM rubric scorer).
│   ├── job_repo.py             # job_postings + FTS5 + source_runs schema + upsert/mark_missing/lookup.
│   ├── job_search.py           # SearchFilters, search() — BM25 + skill_overlap + freshness + title_match
│   │                           # rerank, dedupe-by-listing, round-robin diversification.
│   ├── ingest.py               # APScheduler background ingestion worker; parallel boot backfill.
│   ├── migrations.py           # ensure_column / ensure_index / apply_all_migrations — single source of
│   │                           # truth for SQLite schema additions.
│   ├── stripe_billing.py       # Lazy Stripe SDK wrapper (Customer, Checkout, Portal, Webhook verify).
│   ├── scrapers.py             # ⚠ STALE — early JobSpy/SimplifyJobs/Jobright/InternList scrapers.
│   │                           # Nothing imports it; superseded by pipeline/sources/.
│   └── sources/                # Pluggable JobSource providers — auto-register on import.
│       ├── __init__.py         # Eager-imports every source module so register() runs.
│       ├── base.py             # JobSource Protocol, RawJob TypedDict, infer_metadata, QueryRotator,
│       │                       # GENERAL_QUERIES (cross-industry catalogue).
│       ├── registry.py         # _REGISTRY dict + register() / registry() / get(name).
│       ├── _http.py            # http_get_json / http_post_json / basic_auth_header helpers.
│       ├── github_readme.py    # SimplifyJobs / Vansh new-grad / etc. (no key).
│       ├── api_themuse.py      ├── api_remoteok.py    ├── api_jobicy.py
│       ├── api_himalayas.py    ├── api_remotive.py    ├── api_arbeitnow.py
│       ├── api_weworkremotely.py                       # All keyless.
│       ├── api_usajobs.py      ├── api_adzuna.py      ├── api_reed.py
│       ├── api_jooble.py       ├── api_findwork.py    # Keyed — silently skip when env var missing.
│       └── ats_greenhouse.py   ├── ats_lever.py       ├── ats_ashby.py    └── ats_workable.py
│
├── frontend/
│   ├── landing.html            # Marketing page served at GET / (~1800 lines, scroll-driven scrubber).
│   ├── index.html              # SPA shell served at GET /app (~6100 lines, mostly CSS tokens).
│   ├── app.jsx                 # React 18 SPA (~9000 lines). Babel-transpiled IN-BROWSER — no build step.
│   ├── hero-bg.png             # Asset.
│   ├── landing/                # Frozen demo run JSON + sample resume used by the landing scrubber.
│   ├── index.original.html     # ⚠ STALE backup.
│   └── preview.html            # ⚠ STALE preview tool.
│
├── dashboard/app.py            # Standalone Flask app for Excel tracker approval flow (port 5000).
├── Workflow/                   # job-application-agent.md — canonical 7-phase / scoring spec.
├── config/skill_keywords.yaml  # DemoProvider keyword groups (flattened at runtime).
├── scripts/
│   ├── migrate_db.py           # Standalone CLI: `python scripts/migrate_db.py [--check]`.
│   └── setup_stripe.py         # One-shot Product + recurring Price creator.
├── tools/freeze_landing_demo.py   # Generates frontend/landing/demo-run.json from a real run.
├── tests/                      # pytest suite — unit + integration + Stripe fakes (487+ tests).
│   ├── conftest.py / fakes.py / stripe_helpers.py
│   ├── unit/                   # pipeline/* + auth_utils + session_store + stripe_billing.
│   └── integration/            # FastAPI TestClient: atlas, auth, billing, dev, jobs feed, middleware,
│                               # phases SSE, resume, state/config, static, dashboard.
├── data/                       # Server-private state (kept OUT of OUTPUT_DIR so /output/{path} can't serve it).
│   └── jobs_ai_sessions.sqlite3   # ALL persistent state: users, sessions, session_state, auth_tokens,
│                                  # job_postings + job_postings_fts + source_runs.
├── resources/                  # LLM caches.
│   └── profile_cache_*.json    # Phase-1 cache keyed on (resume_text + provider + model + titles) md5.
├── output/                     # User-visible artifacts ONLY (served by GET /output/{path}, suffix-allow-listed).
│   ├── Job_Applications_Tracker_*.xlsx     # CLI-only outputs (web flow renders inline).
│   ├── *_run-report.md
│   └── sessions/{session_id}/  # Per-session tailored resumes + uploaded original previews.
├── .claude/                    # Local Claude Code config (settings, agents/).
└── .github/workflows/test.yml  # CI: pytest on push/PR.
```

> ⚠ **Stale files** kept for git history but no longer wired in: `db.py`, `jobs.db`, `check_errors.py`,
> `pipeline/scrapers.py`, `frontend/index.original.html`, `frontend/preview.html`. Don't delete unless asked.
> README's old reference to `streamlit_app.py` is also stale — Streamlit was removed; the web UI is FastAPI.
>
> ⚠ **DB path migration**: the SQLite file was moved from `output/jobs_ai_sessions.sqlite3` to
> `data/jobs_ai_sessions.sqlite3` (commit `4debfc9` era). `pipeline/config.py:migrate_db_path()` runs at
> import time and renames the legacy file across the WAL/SHM siblings. Set `JOBS_AI_SKIP_MIGRATION=1` in
> tests to neutralize that side effect.

---

## 2. Backend (`app.py`)

### Session model
- `_SessionStateProxy` (`_S`) is a `MutableMapping` backed by `contextvars.ContextVar` so per-request state is isolated. Routes use `_S["…"]` like a normal dict.
- `_bind_request_state(request)` runs in `session_state_middleware`: resolves `dev_impersonate_id` cookie (localhost only) → `jobs_ai_session` cookie → fresh UUID, loads state via `_load_session_state` (memory cache for anonymous, SQLite for known sessions via `peek_state` — never INSERTs for an anonymous request).
- `_default_state()` defines the schema. **`mode` defaults to `"ollama"`**, **`ollama_model` defaults to `LOCAL_OLLAMA_MODEL` (`smollm2:135m`, the Free-tier local model — overridable via `DEFAULT_OLLAMA_MODEL` env var)**, **`citizenship_filter` defaults to `"all"`** (broadest, least-presumptuous), **`max_scrape_jobs` defaults to `50`**, search-pref strings (`job_titles`, `location`, `experience_levels`, `education_filter`) start EMPTY and are filled from the resume after Phase 1. The two canonical model constants live at the top of `app.py`: `LOCAL_OLLAMA_MODEL = "smollm2:135m"` (Free) and `CLOUD_OLLAMA_MODEL = "gemma4:31b-cloud"` (Pro).
- `_save_bound_state()` only persists when `state.user.id` or `state.user.email` is set. Anonymous sessions stay in `_memory_sessions`; this is what kills the "ghost profiles in Dev Ops" failure mode. Non-GET requests on authenticated sessions write through to SQLite at the end of the middleware; GETs and `/api/state` and `/api/webhooks/stripe` are in `skip_save` (would otherwise clobber concurrent writes).
- `json_default` handles `set` and `Path`. `normalize_state` re-hydrates set-typed keys (`done`, `liked_ids`, `hidden_ids`, `extracting_ids`) and int-keyed dicts (`error`, `elapsed`) on load.

### Auth (`auth_utils.py` + `auth_tokens` table)
- Email/password: bcrypt hash stored in `users.password_hash`. Session bearer token is **SHA-256 digested** before storage (`session_store._hash_token`) — a DB read alone cannot impersonate. Boot-time purge deletes any `auth_tokens.token` not exactly 64 hex chars (legacy raw tokens force one re-login).
- Cookies (all `HttpOnly`, `SameSite=Lax`, `Secure` only when `PRODUCTION` env var is truthy):
  - `jobs_ai_auth` — bearer token, looked up via `_auth_token_lookup` against the hashed digest.
  - `jobs_ai_session` — state-session id.
  - `dev_impersonate_id` — localhost-only "view as user" override.
- Google OAuth: `get_google_auth_url()` / `verify_google_token()` in `auth_utils.py`. The dummy dev flow only fires when `GOOGLE_OAUTH_DEV_DUMMY=1` is explicitly set (it used to fire whenever `GOOGLE_CLIENT_ID` was unset, which was unsafe). Callback verifies `state` via `secrets.compare_digest`; missing/mismatched state aborts. `/api/auth/google` saves state explicitly because GET-handlers skip the middleware save.
- **Server-side invariant:** `_require_auth_user(request)` 401s any caller without a valid `jobs_ai_auth` cookie. Applied to every write endpoint AND every read endpoint that returns user-specific data (`/api/resume/{upload,demo,content,text,delete,primary,rename,tailor,render-preview}`, `/api/profile{,/extract,/diagnose}`, `/api/config`, `/api/reset`, `/api/jobs/{action,ask,feed}`, `/api/atlas/chat/stream`, `/api/feedback`, `/api/billing/{checkout,portal}`). Public exceptions: `/api/state`, `/api/jobs/facets`, `/api/auth/*`, `/api/webhooks/stripe`. The frontend Auth gate is necessary but not sufficient — the server enforces it independently.

### Dev mode
- A request is "dev" iff the authenticated caller has `users.is_developer = 1` OR their email is in the **`_DEV_EMAILS` allow-list** inside `_is_underlying_dev_request` (currently `jonnyliu4@gmail.com`, `saosithisak@gmail.com` — auto-promoted on signin without flipping the DB flag).
- For local-only debugging set `LOCAL_DEV_BYPASS=1` to additionally grant dev to loopback callers without the flag. Never set this in prod behind a proxy — it'd grant dev to anyone.
- `force_customer_mode` in session state hides Dev Ops UI but keeps `_is_underlying_dev_request` true; that pair powers the "Test as Customer" pill so a dev can preview the customer view without locking themselves out. Cleared automatically on every fresh login.
- Dev Ops endpoints live under `/api/dev/*` and are gated by `_is_dev_request` (or `_is_underlying_dev_request` for endpoints a customer-simulating dev still needs to reach, like `/api/dev/runtime`).
- There is no `/api/dev/toggle-role` endpoint; toggle the DB flag with `_user_store.set_user_developer(uid, True)` or use Dev Ops Sessions → PLAN panel for plan tier.

### Plan tier (billing)
- `users.plan_tier` is `'free'` | `'pro'` (default `'free'`). Mirrors the `is_developer` end-to-end pattern: DB column → `auth_user` dict → `/api/state` → `state.plan_tier` / `state.is_pro`.
- **Pricing model**: Free runs **local Ollama** models hosted on the Pi server (small open-weight, low quality). Pro unlocks **cloud Ollama** models — the same daemon transparently proxies `*-cloud` model names to Ollama Turbo's hosted servers, so users get frontier-class quality without installing anything.
- **Gates**:
  - `POST /api/config` returns **402 + `plan_required`** if the caller is non-Pro/non-dev and tries to set an `ollama_model` ending in `cloud`.
  - `POST /api/config` returns **503 + `coming_soon`** if the caller is non-dev and tries `body.mode === 'anthropic'`. **Anthropic Claude is under active development** and reserved for developer testing until launch — the schema still accepts the value so devs can exercise the integration. Belt-and-suspenders mirror gates inside `_run_phase_sse` and `POST /api/resume/tailor`.
  - `_load_session_state` migrates non-dev users off `mode='anthropic'` and free users off cloud Ollama models (`*-cloud` suffix) on every load — prevents a saved-but-now-blocked config from sticking after a downgrade or after Claude moved to coming-soon. Replacement model is the canonical `LOCAL_OLLAMA_MODEL` constant.
- **Manual flip** (ops escape, comp Pro for testers): `_user_store.set_user_plan_tier(user_id, 'pro')` or Dev Ops Sessions → PLAN panel → GRANT PRO. Dev endpoint is `POST /api/dev/users/{user_id}/plan` with `{tier}`. The shared helper `_apply_plan_change(user_id, tier)` in `app.py` is the single source of truth — it writes the DB column AND refreshes every cached `auth_tokens.user_json` so the next `/api/state` poll reflects the change without re-login.

### Stripe billing (Pro $4/month) — ⚠️ backend wired, frontend parked
- **Status**: the entire server-side flow exists and compiles, but the SPA Plans page deliberately still uses the legacy `requestUpgrade` feedback stub (admin manually flips `plan_tier`). Flipping to live billing is purely a frontend swap once the Stripe account is provisioned — no backend changes required.
- **Setup once** (when ready to go live): `pip install stripe`, set `STRIPE_SECRET_KEY` in `.env`, run `python scripts/setup_stripe.py` to create the Product + recurring Price. Paste the printed `STRIPE_PRICE_ID_PRO_MONTHLY` and the `STRIPE_WEBHOOK_SECRET` (from `stripe listen` for dev, dashboard for prod) into `.env`. Then in `frontend/app.jsx` `PlansPage`, swap `requestUpgrade` for `startCheckout`/`openPortal` (the comment in the code marks the spot).
- **Endpoints** (in `app.py`, all auth-gated except the webhook):
  - `POST /api/billing/checkout` → creates subscription-mode Checkout Session, returns `{url}` for the SPA to redirect to. Already-Pro callers get 409. Returns 503 until `STRIPE_PRICE_ID_PRO_MONTHLY` is set.
  - `POST /api/billing/portal` → returns a Stripe Customer Portal URL. 404s for users with no `stripe_customer_id` yet.
  - `POST /api/webhooks/stripe` → HMAC-signature verified. NOT cookie-authenticated. Handles `checkout.session.completed` (links customer/subscription IDs to user only — does NOT flip plan_tier), `customer.subscription.created` / `.updated` (drives plan_tier from `subscription.status`), `customer.subscription.deleted` (flips back to free).
- **Source of truth**: the Stripe webhook is what flips `plan_tier`. The success URL redirect alone is just a UI hint — `/api/state` keeps reporting `free` until the webhook fires (typically <5s).
- **State exposure**: `/api/state` includes `billing_configured` (server-side feature flag — false when `STRIPE_PRICE_ID_PRO_MONTHLY` is unset) and `has_billing_customer` (true when the user has gone through Checkout at least once — gates the Manage Subscription button on the Plans page once that UI is wired back in).
- **Module layout**: SDK is wrapped in `pipeline/stripe_billing.py` (lazy import, raises `RuntimeError` when key/SDK missing). User store gained `stripe_customer_id` + `stripe_subscription_id` columns and helpers (`set_user_stripe_customer`, `set_user_subscription`, `get_user_by_stripe_customer`, `refresh_user_plan_in_tokens`).
- **Webhook URL must be publicly reachable.** For local dev: `stripe listen --forward-to http://localhost:8000/api/webhooks/stripe`. For Tailscale Funnel: enable Funnel on the node (plain Tailnet alone won't reach Stripe). The dev `stripe listen` webhook secret is DIFFERENT from the dashboard endpoint secret — match the env to the active source of events.
- **Middleware**: `/api/webhooks/stripe` is in the `skip_save` list so Stripe deliveries don't churn anonymous session_state rows.

### Route inventory (high-level)
- **Static**: `GET /` (landing), `GET /app` (SPA — JSX url stamped with file mtime to bust browser cache), `GET /frontend/{path:path}` (path-traversal-clipped + `no-cache` headers), `GET /output/{path}` (sandboxed: suffix allow-list `.pdf/.tex/.docx/.txt/.md/.log/.xlsx/.xls/.csv/.png/.jpg/.svg/.html`, suffix block-list `.sqlite*/.env/.pem/.key/.json/.yaml`, per-session paths require auth + session match).
- **Auth**: `POST /api/auth/{login,signup,logout}`, `GET /api/auth/google`, `GET /api/auth/google/callback`.
- **Resume**: `POST /api/resume/upload` (multipart), `POST /api/resume/demo`, `GET /api/resume/content?id=`, `POST /api/resume/text`, `POST /api/resume/primary/{id}`, `POST /api/resume/rename/{id}`, `DELETE /api/resume/{id}`, `POST /api/resume/tailor` (per-job on-demand tailoring; same item shape as one phase-4 row), `POST /api/resume/{id}/render-preview` (back-fill polished preview PDF for legacy records).
- **Config / state**: `POST /api/config` (whitelisted keys only — see below), `GET /api/state`, `POST /api/reset`.
- **Profile**: `GET /api/profile`, `POST /api/profile`, `POST /api/profile/extract` (`{resume_id?, preferred_titles?, force?}`), `GET /api/profile/diagnose?id=` (heuristic-only diagnostic — no LLM call).
- **Phases (SSE)**: `GET /api/phase/{1..7}/{run,rerun}`. Phase 2 accepts `?deep=1&append=1&force=1` query params; Phase 3 accepts `?fast=1`. Phase 2 also has `GET /api/phase/2/cache` and `DELETE /api/phase/2/cache` for the legacy quick/deep JSON caches under `resources/`.
- **Jobs (live index)**: `GET /api/jobs/feed?cursor=&limit=&q=&exp=&edu=&cit=&remote=&days=&location=&industry=&since_id=&blacklist=&whitelist=` (cursor-paginated, profile-ranked, pulls from `job_postings` via `pipeline.job_search.search`). `GET /api/jobs/facets?kind={industry,location,company}&q=&limit=` (PUBLIC — pure aggregate counts, no PII). `POST /api/jobs/action` (`like|unlike|hide|unhide`). `GET /api/jobs/source-status` / `POST /api/jobs/source-status` (force one or all sources to re-tick — dev-only).
- **Ask Atlas**: `POST /api/jobs/ask` (per-job advisor — `{job_id, message, history?}` → `{reply}`, synchronous). `POST /api/atlas/chat/stream` (career-wide streamer — same body, returns SSE chunks `{type: 'start' | 'delta' | 'done' | 'error'}`).
- **Feedback**: `POST /api/feedback` (`{message}`).
- **Ollama**: `GET /api/ollama/status` (probe daemon + list pulled models + report any in-flight pull), `POST /api/ollama/ensure` (idempotent — pull session model if missing), `GET /api/ollama/pull` (SSE — stream a pull for the session-configured model).
- **Billing (Stripe)**: `POST /api/billing/checkout` (auth-gated; 503 until `STRIPE_PRICE_ID_PRO_MONTHLY` set; 409 if already Pro), `POST /api/billing/portal` (404 if no `stripe_customer_id`), `POST /api/webhooks/stripe` (HMAC-verified; in middleware `skip_save` list).
- **Dev Ops**: `/api/dev/overview`, `/api/dev/metrics?with_processes=1` (psutil snapshot), `/api/dev/logs?since=&limit=` + `/api/dev/logs/stream` (SSE feed of stdout/stderr/`logging` ring), `/api/dev/users`, `POST /api/dev/users/{user_id}/plan` (`{tier}`), `/api/dev/session/{id}` (GET / reset / delete / impersonate / `feedback/read`), `POST /api/dev/session/stop-impersonating`, `/api/dev/cli` (whitelist: `git_status`, `pip_freeze`, `recent_outputs`, `session_db`), `/api/dev/tweaks`, `GET /api/dev/runtime` + `POST /api/dev/runtime` (toggle `maintenance` / `verbose_logs`), `POST /api/dev/reload-env` (re-read `.env` without restart).

### `/api/config` whitelist
The config endpoint only accepts a fixed set of keys. When you add a new user-tunable setting, add it to **both** `_default_state()` and the whitelist tuple in `update_config`. Current whitelist: `mode`, `api_key`, `ollama_model`, `threshold`, `job_titles`, `location`, `max_apps`, `max_scrape_jobs`, `days_old`, `cover_letter`, `blacklist`, `whitelist`, `experience_levels`, `education_filter`, `include_unknown_education`, `include_unknown_experience`, `citizenship_filter`, `use_simplify`, `llm_score_limit`, `force_customer_mode`, `light_mode`. Plan-gate: `mode='anthropic'` and any `*-cloud` Ollama model are 402'd for non-Pro non-dev callers (Anthropic is also gated belt-and-suspenders inside `_run_phase_sse`).

### `/api/reset` semantics
Wipes session state to defaults but **preserves** `user`, `dev_tweaks`, `mode`, `api_key`, `ollama_model`, `light_mode`, `force_customer_mode`. Also `shutil.rmtree`s `output/sessions/{session_id}/` to clear generated files. The user stays logged in. Holds the per-session lock for the entire clear+persist so a concurrent extraction thread can't race in.

### Server-side log mirror & live metrics
`_LOG_RING` is a 3000-line `collections.deque` fed by `_StreamTee` (wraps `sys.stdout`/`sys.stderr`) and `_RingLogHandler` (Python `logging`). `/api/dev/logs/stream` fans out new records to SSE subscribers; `/api/dev/logs` returns a windowed snapshot. CPU per-core, memory, top processes, CPU temperature come from psutil with 800 ms / 2.5 s snapshot caches so the SPA's 1–2 s polls don't blow out the request loop. The per-core sampler is primed in a `@app.on_event("startup")` hook (NOT at module import — that previously ran during `pytest` collection).

---

## 3. Pipeline (`pipeline/phases.py`)

Each phase is a function that mutates `_S` (via the proxy) and emits log lines to the SSE stream. SSE plumbing in `_run_phase_sse` claims a per-session phase slot (cap 1), runs `fn` in a daemon thread with stdout teed into a queue, and emits `start | log | done | error` events. Pre-checks: `_RUNTIME["maintenance"]` short-circuits with an error event; an `mode='anthropic'` caller who isn't a developer is rejected with `code: "coming_soon"` (belt-and-suspenders against the `/api/config` 503 — Claude is still under development).

| Phase | Function | What it does |
|---|---|---|
| 1 | `phase1_ingest_resume` | Heuristic-first: `pipeline.profile_extractor.scan_profile` runs deterministic regex+section parsing → LLM `extract_profile(text, preferred_titles, heuristic_hint=...)` verifies and corrects → `merge_profiles` → `pipeline.profile_audit.audit_profile` (flatten / quarantine misplaced soft-skills / retention audit / verify evidence / rerank titles) → `pipeline.resume_insights.analyze_resume` (deterministic metrics + optional LLM verification). Result cached by md5 of `(resume_text, provider, model, preferred_titles)` under `resources/profile_cache_*.json`. |
| 2 | `phase2_discover_jobs` | **Reads from the local `job_postings` index** via `pipeline.job_search.search` — does NOT scrape live. `force_live=True` (set by `?force=1` or `append=1`) triggers a synchronous `pipeline.ingest.force_run()` first. Empty index → falls back to `provider.generate_demo_jobs`. Pushes every user filter (blacklist, whitelist, citizenship, experience levels, education, days_old, location) into the SQL WHERE so the cap reflects post-filter results. |
| 3 | `phase3_score_jobs` | Two-stage. **`compute_skill_coverage(job, profile)` is deterministic** and the LLM cannot override it — that's the heaviest 50%-weighted dimension and it's pure math (token-aware, scans title + requirements + description, requirements are the denominator). `_fast_score` ranks every job using the same coverage helper; the top `llm_score_limit` (default 10) get `provider.score_job` for the qualitative `industry` + `location_seniority` dims, glued back together via `_build_rubric_result`. Pre-filters experience level (with `include_unknown_experience` passthrough — most ingested rows are metadata-only) and citizenship (regex sweep + inferred-field check). `?fast=1` skips the LLM rerank entirely. Filter status: `passed | below_threshold | filtered_experience | filtered_citizenship`. |
| 4 | `phase4_tailor_resume` | Always computes the deterministic `pipeline.heuristic_tailor.heuristic_tailor_resume` baseline first. Then asks the provider; runs the result through `validate_tailoring` (which rejects wrong-shape JSON from low-end Ollama models). On reject, retries once; still bad → falls back to heuristic. Otherwise `merge_with_heuristic` keeps the LLM's good fields and backfills empties. **Anti-fabrication invariants**: `skills_reordered` only reorders existing skills (missing JD keywords go in `ats_keywords_missing`); `experience_bullets` only reorders existing bullets, never invents. ATS scoring runs before/after via `_ats_score`. Output goes through `_save_tailored_resume` (`.tex` + `.pdf`). |
| 5 | `phase5_simulate_submission` | **Web flow: curation step**, NOT auto-submit. Picks the top-N (`llm_score_limit`) high-confidence applications and labels them `Manual Required`; the rest are `Skipped`. Already-applied items (cross-checked against current month's tracker) are skipped. **The randomized "Applied" coin-flip stub was deliberately removed** so users aren't misled by fake confirmations — never reintroduce it. CLI flow can opt into `PlaywrightSubmitter` via `--real-apply` (Greenhouse boards only, falls through to simulation otherwise). |
| 6 | `phase6_update_tracker` | Returns `{month, columns, rows, summary}` from `TRACKER_COLUMNS` (single source of truth for both UI render and `.xlsx` export). Web flow passes `write_file=False` and renders inline; CLI writes `Job_Applications_Tracker_{YYYY-MM}.xlsx` via `openpyxl` with status color-coding + frozen header + auto-fit columns + Summary Dashboard tab. |
| 7 | `phase7_run_report` | `provider.generate_report(summary_data)` → markdown. `write_file=False` for the web flow (renders inline); CLI writes `{YYYYMMDD}_job-application-run-report.md`. SMTP notification fires only when ALL of `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `NOTIFY_EMAIL` are set. |

### Providers (`pipeline/providers.py`)
- **`BaseProvider`**: `extract_profile(text, preferred_titles, heuristic_hint)`, `score_job(job, profile)`, `tailor_resume(job, profile, text)`, `generate_cover_letter`, `generate_report`, `generate_demo_jobs`, `chat(system, messages, max_tokens, json_mode)`. The `heuristic_hint` kwarg is what the heuristic-first design hangs on — providers that take an LLM use it as a verified baseline; providers that ignore it fall back to old behavior.
- **`AnthropicProvider`** — `claude-opus-4-6` via the SDK. Forced tool-calling for strict JSON in `extract_profile` / `score_job` / `tailor_resume`; thinking mode enabled where it pays. JSON mode in `chat()` prefills the assistant turn with `{` so the model continues from there. Streaming used by `_stream_provider_chat` for the career-wide Atlas.
- **`OllamaProvider`** — OpenAI-compatible client against `OLLAMA_URL` (default `http://localhost:11434`, override via env). `_check_ollama` bypasses the local-pull check for `*-cloud`/`:cloud` models (Ollama Turbo). `score_job` uses `json_mode=True` so JSON parse failures stop falling back to the neutral 0.5/0.5 = 50.
- **`DemoProvider`** — pure-Python regex/section parsing. `_split_sections` + `_parse_education_block` + `_parse_experience_block` + `_parse_projects_block` — these are the same helpers `pipeline.profile_extractor` reuses for the heuristic baseline. Hard skills come from `config/skill_keywords.yaml`; soft skills come from text via `_scan_soft_skills` (no hardcoded list — the previous "Teamwork / Problem-solving / …" list was a bug, see Bug History).

### Resume extraction fallback chain (`pipeline/resume.py`)
`pypdfium2` → `pdfplumber` → `pypdf` → `pdfminer.six`. Always verify `pypdfium2` import is intact when touching this file. `_normalise_pdf_text` cleans ligatures (`ﬁ → fi`), curly quotes, and re-joins lines broken mid-word. `.docx` reads paragraphs AND table cells (sidebar layouts put content in tables).

### PDF generation
1. `pdflatex` if on `PATH` — preceded by `pipeline.latex._sanitize_latex_source` which strips `\input`, `\write18`, `\openin`, `\catcode`, `\directlua`, etc. so a malicious LaTeX upload can't read `/etc/passwd` or shell out.
2. `pipeline.resume._render_resume_pdf_reportlab` fallback. Honours `pdf_format` fingerprint (column count, body/header font sizes, accent color) computed by `pipeline.pdf_format.detect_format_profile` at upload time so generated tailored PDFs mirror the source aesthetic.

## 3a. Job ingestion subsystem

A whole sub-system runs continuously in the background to keep `data/jobs_ai_sessions.sqlite3:job_postings` fresh.

### Sources (`pipeline/sources/`)
Each provider implements the `JobSource` Protocol (`name`, `cadence_seconds`, `timeout_seconds`, `fetch(since) -> Iterator[RawJob]`). They self-register on import via `register(self)` in their module body. `pipeline.sources.__init__` eager-imports each module so importing `pipeline.sources.registry()` returns the populated list. Keyed sources skip registration silently when their env var is absent.

Live providers:
- **Keyless**: `github_readme` (Simplify-style READMEs), `api_themuse`, `api_remoteok`, `api_jobicy`, `api_himalayas`, `api_remotive`, `api_arbeitnow`, `api_weworkremotely`.
- **Keyed**: `api_usajobs` (USA), `api_adzuna` (UK + global), `api_reed` (UK), `api_jooble` (global), `api_findwork` (global).
- **ATS readers**: `ats_greenhouse`, `ats_lever`, `ats_ashby`, `ats_workable` — each iterates a curated slug list (~60 international slugs after the globalization pass + 19 Hong Kong slugs).

`QueryRotator` rotates through a long query list `batch_size` at a time across cycles so a keyed source covers dozens of job families without burning daily quota every tick. `infer_metadata` runs `infer_experience_level` / `infer_education_required` / `infer_citizenship_required` / `infer_job_category` against each raw row before upsert.

### Worker (`pipeline/ingest.py`)
`start_scheduler(connect)` is wired into FastAPI's startup event. Spawns an APScheduler `BackgroundScheduler` (UTC timezone) with one `IntervalTrigger` per source at its `cadence_seconds`. Per-source `threading.Lock` (`_locks`) prevents reentrancy. `run_one(source)` does fetch → `infer_metadata` → `job_repo.upsert_many` → `job_repo.mark_missing` (rows not refreshed bump `miss_count`; `>=3` flips `deleted=1`) → `record_source_run`. Failures are isolated per source and logged.

Boot-time backfill is parallel across all sources with a 60 s wall clock — but it's **skipped** when `source_runs.MAX(started_at)` is < 60 min ago, since restarting to pick up code changes shouldn't re-fire 8 concurrent upserters that block SQLite reads for half a minute. Set `JOBS_AI_DISABLE_INGESTION=1` in tests to skip the scheduler entirely.

### Repo (`pipeline/job_repo.py`)
- `canonical_url(url)` lowercases the host, strips utm/gh_src/fbclid/gclid/etc., drops fragment + trailing slash. The `id` column is `sha256(canonical_url)[:16]`.
- Upserts coalesce: location/requirements/posted_at use `COALESCE(excluded.x, jp.x)` so a thinner refresh doesn't overwrite richer prior data; salary uses a `CASE` to ignore `Unknown`.
- FTS5 virtual table (`job_postings_fts`) with `porter unicode61` tokenizer over `title`, `company`, `requirements`. Three triggers (`_ai`, `_ad`, `_au`) keep it in lockstep with the main table.
- `mark_missing` is per-source so a temporarily-offline provider doesn't soft-delete its peers' rows.

### Search (`pipeline/job_search.py`)
`search(conn, filters, profile, cursor, limit, rank_pool, dedupe, per_round)` is the single entry point. Pipeline:
1. SQL filter against `job_postings` (deleted=0, experience/education/remote/posted_at/location/citizenship/blacklist/job_categories).
2. Optional `JOIN job_postings_fts ... MATCH ?` using a query string built from the user's `q` plus profile-derived terms (target_titles + top_hard_skills, capped at 24 tokens).
3. SQL `ORDER BY bm25_score, posted_at DESC` with a deep `LIMIT` (`max(rank_pool, (cap+offset)*6)`, capped at 8000).
4. Python re-rank: `final = 0.45*bm25 + 0.30*skill_overlap + 0.15*freshness + 0.10*title_match`. Whitelist substring match boosts +0.25.
5. **`_dedupe_by_listing`** collapses cross-source / multi-city dupes by `(company_norm, title_norm, location_first_token)`. Title normalization strips year/season/work-model/`intern|internship` differentiators so mirrors of the same posting collapse.
6. **`_diversify_by_category`** (cold no-profile feed only) → cross-industry round-robin so a brand-new visitor sees `engineering / sales / marketing / healthcare / finance / …` on page 1 instead of an all-tech wall.
7. **`_diversify_by_company`** round-robin (default `max_per_round=1`) — at most one entry per company per round so a single dominant employer can't take over the page.
8. Cursor-paginated; cursor is base64'd `{score, last_id, page_offset}`. `newer_than(top_id)` powers the SPA's 25-second polling tick.

### Migrations (`pipeline/migrations.py`)
**The `try: ALTER TABLE…; except sqlite3.OperationalError: pass` pattern is forbidden.** It silently swallowed real failures on the Pi for months. `apply_all_migrations(conn)` is the single source of truth: `ensure_column(conn, table, col, type_decl, default=…)` uses `PRAGMA table_info` to detect missing columns explicitly, ALTERs only when missing, and re-raises anything that's not a duplicate-column race. Runs in **two places** belt-and-suspenders: inside `SQLiteSessionStore._init_db` (so a fresh constructor catches changes too) and in `app.py`'s `_ensure_schema_migrations` startup hook (so a deployed code update runs even if the store was constructed before the migration was added). Failures are loud — written to stderr so they show in `journalctl -u jobapp`. `scripts/migrate_db.py [--check]` is the standalone CLI for emergency / out-of-band runs.

---

## 4. Frontend (`frontend/app.jsx` — ~9000 lines)

- React 18 SPA. **Transpiled in-browser by Babel standalone** (`<script type="text/babel">`). There is no build step — edits to `app.jsx` are live after a HARD browser refresh. The `/app` handler stamps the JSX URL with the file mtime (`?v={mtime}`) to force browser cache invalidation across server restarts.
- Single root state object fetched from `/api/state`, refreshed via `refresh()` on an **adaptive poll**: 2 s while any resume is extracting, 8 s otherwise. **Polling is gated on `state.user`** — pre-login polling races would clobber the OAuth state set by a concurrent `/api/auth/google`.
- **Hash-based routing** with `history.pushState`. Pages: `home | jobs | resume | profile | agent | settings | feedback | dev | plans | auth | onboarding`. Empty hash = home. Auth gate routes anonymous visitors to `auth` (no SPA page is reachable without `state.user`); the onboarding gate routes authenticated users without a resume to `onboarding`.
- API helper `api` (top of file) wraps `fetch` with an `AbortController` (default 30 s timeout, 90 s for `/api/resume/tailor`), unwraps `{detail, error, message}`, and exposes `get` / `post` / `upload` / `delete`.
- `runPhaseSSE(n, …)` opens an `EventSource` against `/api/phase/n/{run|rerun}` and dispatches `start | log | done | error`. The SPA cancels on unmount or page navigation.
- `/api/atlas/chat/stream` uses **manual `ReadableStream` parsing** (NOT `EventSource`) so it can be cancelled mid-flight via the same `AbortController` the chat helpers use.

### Pages
| Page | Purpose |
|---|---|
| **home** | Hero, CockpitStrip HUD (status / greeting / live clock / streak / phase / high-fit count), mission-control quick actions, Resume Intelligence dossier card. |
| **jobs** | Jobs feed (locally-owned, paginates `/api/jobs/feed`), facet filters via `/api/jobs/facets` (industry / location / company), 65-entry curated location quick-pick, 250 ms-debounced search, per-job "Ask Atlas" drawer + "Tailor for this job" drawer (90 s timeout), 25 s polling tick via `since_id`. |
| **resume** | Multi-resume manager — upload (drag/drop/paste), original PDF iframe OR generated preview PDF, AI insights tabs (overview / metrics / insights / deep-dive), per-resume edit, set-primary, rename, delete, Re-scan. Per-upload extraction polling intervals tracked in a Set + cleared on unmount. |
| **profile** | Auto-save (700 ms debounce) form. AutoSaveBadge inline pill renders five states (idle / pending / saving / saved / error). `inFlightRef` + save-snapshot comparison handle the keep-typing-during-save race; unmount flushes pending edits. Includes `ProfileSelect` for the 17 canonical US work-auth options. |
| **agent** | 7-phase orchestrator with circular progress ring. Inline `agent-tune` slider for `max_scrape_jobs`. Career-wide AtlasChat sidebar streaming via `/api/atlas/chat/stream`. |
| **settings** | LLM backend, Ollama model picker (server-side `/api/ollama/status`), search prefs (titles / location / chips), filters, threshold, cover-letter mode. Job Discovery section (max_scrape_jobs, days_old). |
| **plans** | Free (local Ollama) / Pro (cloud Ollama) tier cards. Anthropic Claude is shown as "coming soon" in both tiers — it's not currently a Pro perk. **Currently uses the `requestUpgrade` feedback stub** (admin manually flips `plan_tier`); Stripe `startCheckout` / `openPortal` are wired in `app.py` but the SPA still calls the stub. To go live, swap the `requestUpgrade` call inside `PlansPage` for the checkout/portal helpers. |
| **feedback** | Textarea → `POST /api/feedback`. |
| **dev** | Sessions table, live metrics (CPU/memory/temp at 2 s tick), live server log SSE, plan-tier editor, restricted CLI. Hidden from non-dev users by `_is_dev_request` server-side AND `state.is_dev` client-side. |
| **auth** | Email/password login + signup, Google OAuth redirect (currently labelled "under development"). |
| **onboarding** | First-time guided flow — resume upload + demo. Requires `state.user` first. |

### Shared utilities
- **`api`** helper, **`runPhaseSSE`**, **`streamAtlasChat`** (manual SSE parser).
- **`Icon`** wraps `lucide` icons via `data-lucide` + `window.lucide.createIcons()`.
- **`Rail`** — sidebar nav, mobile drawer (Escape / click-outside / body-scroll lock).
- **`CompanyLogo`** — Clearbit → Google favicon → letter monogram fallback.
- **`Markdown`** — used by AskAtlas replies and the Phase 7 report.
- **`ChipToggle`** — multi-select filter chip component.
- **`ProfileSelect`** — single-select with custom chevron + light-theme override.

### Theming
- All colors live as CSS custom properties on `:root` in `index.html` (and again in `landing.html`).
- `:root[data-theme="light"]` overrides those tokens. The `light_mode` setting toggles `document.documentElement.dataset.theme` via a `useEffect` in `App()`. The `useEffect` skips no-op DOM writes (only writes when the value changes) so polling doesn't cause attribute churn.
- Brand: deep ink stage. **Accent `--accent: #7c5cff`** (electric violet). Secondary `--accent2: #22e5ff` (cyan), tertiary `--accent3: #ff3d9a` (magenta). Status: `--good: #3dff9a`, `--warn: #ffd23d`, `--bad: #ff4d6d`.
- `state.dev_tweaks` injects CSS custom properties on `<html>` on every poll (banner text, density, accent override, experiment flag).
- **No horizontal page scrolling, anywhere.** Set `overflow-x: hidden` AND `min-width: 0` on grid children — `overflow-y: auto` alone auto-promotes the other axis to `auto` per the CSS Overflow spec, which produced trackpad-hostile horizontal scroll in Dev Ops. See Bug History.

### Gating logic (App component)
1. **Booted gate** — wait for first `/api/state` response.
2. **Auth gate** — every page requires `state.user`. Anonymous visitors only ever see `<AuthPage/>`.
3. **Onboarding gate** — authenticated users without a resume see `<Onboarding/>`. **Must require `state.user` first**, otherwise unauthenticated uploads create ghost profiles in the Dev Ops list. The server enforces this independently via `_require_auth_user` on `/api/resume/{upload,demo}`.

---

## 5. Persistence (`session_store.py`)

- SQLite at **`data/jobs_ai_sessions.sqlite3`** (WAL mode where the OS allows). The file lives outside `OUTPUT_DIR` so the static-file route can never serve it. `pipeline/config.py:migrate_db_path()` runs at import time to rename the legacy `output/jobs_ai_sessions.sqlite3*` files (with WAL/SHM siblings) — bails out if it detects a live writer (locked WAL/SHM) so it can't corrupt an in-flight DB.
- **Corruption quarantine**: `_quarantine_if_corrupt` runs `PRAGMA quick_check` before opening; on failure renames the file with a `.corrupt-{YYYYMMDD-HHMMSS}` suffix (preserves WAL/SHM siblings) and rebuilds a fresh schema. Recovery cost: users + auth tokens + session state are gone (users re-sign-in; resume profiles re-extract; job index re-fills within minutes). Better than every `/api/state` 500ing.
- **Tables**:
  - `users(id PK, email UNIQUE, password_hash, google_id, is_developer, plan_tier, stripe_customer_id, stripe_subscription_id, created_at)`
  - `sessions(id PK, user_id FK→users, created_at, updated_at)`
  - `session_state(session_id PK→sessions, state_json TEXT, updated_at)`
  - `auth_tokens(token PK [SHA-256 digest], user_id FK→users, user_json, created_at)`
  - `job_postings(id PK, canonical_url UNIQUE, source, company, company_norm, title, title_norm, location, remote, requirements_json, salary_range, experience_level, education_required, citizenship_required, job_category, posted_at, fetched_at, last_seen_at, miss_count, deleted)` + indexes (deleted+last_seen_at, deleted+posted_at, deleted+experience+education+remote, company_norm, source, deleted+job_category) + FTS5 virtual `job_postings_fts(title, company, requirements)` with `_ai/_ad/_au` triggers.
  - `source_runs(source, started_at, finished_at, ok, fetched, inserted, updated, error, PRIMARY KEY (source, started_at))` + index on `source, started_at DESC`.
- **Plan-tier propagation** (`refresh_user_plan_in_tokens`): when a Stripe webhook flips `plan_tier`, every persisted `auth_tokens.user_json` row for that user is rewritten so the next `/api/state` poll reflects it without re-login. The `_AUTH_SESSIONS_FALLBACK` in-memory cache is refreshed in lockstep.
- **State proxy contract**: `default_state_factory` is injected at construction (it's `_default_state` from `app.py`), so adding a new state key requires updating that function only. `json_default` handles `set` and `Path`; `normalize_state` re-hydrates set-typed keys (`done`, `liked_ids`, `hidden_ids`, `extracting_ids`) and int-keyed dicts (`error`, `elapsed`). Adding a new set-typed state key requires updating both `normalize_state` and `_load_session_state` so the set survives the JSON round-trip.
- **Anonymous sessions never INSERT**: `peek_state(session_id)` is read-only; `_save_bound_state` early-returns to `_memory_sessions` when `state.user.id|email` is unset. `list_sessions` filters `WHERE user_id IS NOT NULL` to hide any legacy ghost rows.

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

1. **Secrets**: never commit `ANTHROPIC_API_KEY`, `GOOGLE_CLIENT_SECRET`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, or any `sk-…` / `whsec_…` literal. The Anthropic key lives in volatile session state or env var only. `.gitignore` covers `data/*.sqlite3*`, `*.corrupt-*`, `resources/profile_cache_*.json`, `test_cookies.txt`, `test_signup_resp.json`, `server.log`, `.coverage` — never commit these.
2. **Cross-platform paths**: always derive paths from `Path(__file__)` (this repo runs on Windows AND on the Linux production box / Pi). Don't hardcode separators.
3. **Frontend has no build step**: do NOT introduce webpack/vite/tsc unless you also commit the build artefact and update `index.html`. JSX is in-browser Babel; edits go live on hard refresh.
4. **PDF engine order**: `pypdfium2` is primary. If you reorder the fallback chain, update `pipeline/resume.py` AND this doc.
5. **Run the server before reporting frontend changes done**: `python app.py` (or `uvicorn app:app --reload --port 8000`), open `http://localhost:8000/app`, exercise the actual feature.
6. **Migration discipline (THE LOAD-BEARING ONE)**: never inline `try: ALTER TABLE…; except sqlite3.OperationalError: pass`. Always go through `pipeline/migrations.py:ensure_column` / `ensure_index`. Real failures must surface in `journalctl -u jobapp` — silent swallowing is what bricked the Pi for months.
7. **Provider parity**: changing the JSON schema for `extract_profile` / `score_job` / `tailor_resume` requires updating ALL THREE providers in `pipeline/providers.py`. The validator in `heuristic_tailor.validate_tailoring` is the safety net for low-end models — keep it in sync.
8. **No fabrication in tailoring**: `skills_reordered` only ever reorders existing user skills. Missing JD requirements MUST go in `ats_keywords_missing`, never silently appended to the user's skill list. Same for `experience_bullets` — reorder only, never invent.
9. **No hardcoded defaults that lie about the user**: search prefs (`job_titles`, `location`, `experience_levels`, `education_filter`) start EMPTY and are filled from the resume. Never reintroduce the legacy `Engineer` / `United States` / `bachelors` placeholders — they leaked into SQL filters even when the resume said otherwise.
10. **Phase 5 is curation, not auto-submission** in the web flow. The randomized "Applied" coin-flip stub was deliberately removed; never reintroduce it.
11. **Anonymous sessions never persist**: `_save_bound_state` early-returns to memory when `user.id|email` is unset; `peek_state` is read-only. Touching either of these to "fix" something else will reintroduce the ghost-profiles failure.
12. **Auth-token storage**: `auth_tokens.token` stores the SHA-256 digest, never the raw cookie value. The 64-hex purge on boot enforces this — don't re-introduce raw-token rows.
13. **Stripe webhook is the source of truth** for `plan_tier`, NOT the success-URL redirect. Until the webhook fires, `/api/state` reports `free`. The webhook is HMAC-verified, sits in the middleware `skip_save` list, and routes through `_apply_plan_change` (DB column + every cached `user_json`).
14. **Stale files** kept for git history but not wired in: `db.py`, `jobs.db`, `check_errors.py`, `pipeline/scrapers.py`, `frontend/index.original.html`, `frontend/preview.html`. Leave them alone unless the user asks for cleanup.
15. **Document your work — see §10**. Detailed commit message + CLAUDE.md updates per change type + a Bug History entry for fixes worth remembering. Errors and dead-ends count: leave one sentence so the next agent doesn't redo your wrong turns. Doc drift you spot while passing through? Fix it in the same commit.

---

## 8. Stripe billing — current state

**Status (as of 2026-05-07)**: backend fully wired and tested; frontend Plans page deliberately uses the `requestUpgrade` feedback stub at `frontend/app.jsx:7113`. The comment at line 7111 marks the swap site for `startCheckout` / `openPortal`.

### Backend setup (one-shot)
1. `pip install stripe` (in `requirements.txt`).
2. Set `STRIPE_SECRET_KEY` (`sk_test_…` for dev, `sk_live_…` for prod) in `.env`.
3. `python scripts/setup_stripe.py` — creates the Pro Product + recurring monthly Price; prints the price ID.
4. Paste it into `.env` as `STRIPE_PRICE_ID_PRO_MONTHLY`. Without this, `/api/billing/checkout` returns 503.
5. Set `STRIPE_WEBHOOK_SECRET` — `stripe listen --forward-to http://localhost:8000/api/webhooks/stripe` for dev (prints `whsec_…`); the dashboard endpoint config for prod. **The two are different secrets** — match the env to the active source.
6. Set `PUBLIC_BASE_URL` when running behind a proxy (Tailscale Funnel, Cloudflare). Without it, FastAPI sees the internal address and Stripe redirects break.

### Endpoints
- `POST /api/billing/checkout` — auth-gated. Subscription-mode Checkout Session. 409 for already-Pro callers (use the portal). 503 until price ID is set.
- `POST /api/billing/portal` — auth-gated. Stripe Customer Portal redirect. 404 for users without a `stripe_customer_id` (haven't gone through Checkout yet).
- `POST /api/webhooks/stripe` — HMAC-verified, NOT cookie-authenticated. In `skip_save` list (Stripe deliveries don't churn anonymous session_state rows). Handlers:
  - `checkout.session.completed` → links `stripe_customer_id` + `stripe_subscription_id` to user. Does NOT flip `plan_tier` (subscription may still be `incomplete`).
  - `customer.subscription.{created,updated}` → drives `plan_tier` from `subscription.status`. `active|trialing|past_due` → `pro`; everything else → `free`. `past_due` is intentional so a brief failed-payment retry doesn't bounce a paying user.
  - `customer.subscription.deleted` → `plan_tier='free'`, clears `stripe_subscription_id`, keeps `stripe_customer_id` so the user can resubscribe.
  - Unknown event types → no-op log; returned 200 so Stripe stops retrying.
  - Handler exceptions → traced to stderr, returned 200 (returning 500 makes Stripe retry forever — better to dead-letter in the dashboard for manual inspection).

### State exposure (in `/api/state`)
- `billing_configured` — `True` only when `STRIPE_PRICE_ID_PRO_MONTHLY` is set AND the Stripe SDK loads successfully. Self-host deployments without a price ID can hide the upgrade button entirely.
- `has_billing_customer` — `True` when the user has a persisted `stripe_customer_id`. Gates the "Manage subscription" button once the Plans page is wired back in.
- `plan_tier` / `is_pro` — drive every plan gate.

### Manual Pro flip (ops escape, comp Pro for testers)
`_user_store.set_user_plan_tier(uid, 'pro')` directly, OR Dev Ops Sessions → PLAN panel → GRANT PRO (which calls `POST /api/dev/users/{user_id}/plan` with `{tier}`). Both go through `_apply_plan_change` which writes the DB column AND refreshes every cached `auth_tokens.user_json` so the next `/api/state` poll reflects the change without re-login.

### Module layout
- `pipeline/stripe_billing.py` — lazy SDK wrapper (`is_configured()`, `ensure_customer`, `create_checkout_session`, `create_portal_session`, `verify_webhook`, `subscription_active`). Pinned to API version `2024-06-20` so dashboard upgrades can't silently change object shapes.
- `session_store.py` — `stripe_customer_id` + `stripe_subscription_id` columns on `users`; helpers `set_user_stripe_customer`, `set_user_subscription`, `get_user_by_stripe_customer`, `refresh_user_plan_in_tokens`.

---

## 9. Recent Bug History & Invariants

These are the bugs that shaped the current invariants. Format is **what broke → root cause → invariant**. Don't reintroduce the underlying patterns.

### Job index column missing on Pi (`4debfc9`)
Every `INSERT` into `job_postings` 500'd with `no such column: jp.job_category`, and the jobs feed was broken. The schema migration sat in a `try: ALTER…; except sqlite3.OperationalError: pass` block that swallowed REAL failures, not just "column already exists". Compounded by `CREATE INDEX IF NOT EXISTS ix_jobs_category` running BEFORE the `ALTER TABLE` that adds the referenced column — `CREATE INDEX IF NOT EXISTS` still validates referenced columns and crashed the rest of `init_schema`. **Invariant**: only `pipeline/migrations.ensure_column` / `ensure_index` add columns/indexes. Indexes referencing newly-added columns MUST come after the corresponding `ensure_column` call.

### Sidebar resume name extraction (`cf59a3e`)
A user's PDF was extracted as `name="Hong Kong"` because the sidebar layout placed the contact block (CONTACT label + phone + email + city) before the actual name at line 41 of the PDF text. **Invariant**: `_extract_name_from_text` is section-aware (only the "header" zone before the first recognized section header is eligible), uses a standalone-city blacklist, and returns `""` rather than committing to a wrong guess. The LLM merge fills empty fields.

### Hardcoded soft-skill list (`9a05dee`)
After `/api/reset` + re-upload of any random resume, `top_soft_skills` always came back `["Teamwork", "Problem-solving", "Communication", "Attention to detail", "Time management"]`. `DemoProvider.extract_profile` had a hardcoded fallback that fired whenever Ollama was unreachable. **Invariant**: providers extract from the resume text via `_scan_soft_skills` (lexicon-driven matcher) — no synthesized lists, ever. The same comment at `providers.py` line 1624 has been calling this anti-pattern out for hard skills since launch.

### Anonymous ghost profiles in Dev Ops (`51821dc`)
Anonymous visitors uploading resumes created `users` / `sessions` / `session_state` rows that surfaced in Dev Ops as "ghost users". **Invariant**: `_save_bound_state` only persists when `state.user.id|email` is set; anonymous state stays in `_memory_sessions`. `peek_state` is read-only. `list_sessions` filters `WHERE user_id IS NOT NULL` to hide legacy rows. `_require_auth_user` 401s `/api/resume/{upload,demo}` server-side regardless of frontend gating.

### Plaintext bearer tokens in DB (`51821dc`)
Auth tokens were stored verbatim in `auth_tokens.token`, so a DB read alone would let an attacker impersonate every user. **Invariant**: tokens are SHA-256 digested before storage (`session_store._hash_token`). Boot-time purge deletes any `auth_tokens.token` not exactly 64 hex chars (forces one re-login from every legacy session).

### Phase 2 blacklist applied after the cap (`43baf3d`)
A user with a 30-company blacklist + `cap=50` could end up with 20 results because the blacklist filtered the top-50 ranked rows after the cap. **Invariant**: every user-declared filter (blacklist, whitelist, citizenship, experience, education, days_old, location, job_categories) is pushed through to the SQL `WHERE` in `SearchFilters`. The cap reflects post-filter rows.

### LLM-invented skill scores (`51821dc`)
The Anthropic / Ollama scorer was asked to score skill coverage along with everything else, and would invent its own number. **Invariant**: `compute_skill_coverage(job, profile)` is deterministic and cached on the job dict — the LLM only judges `industry` and `location_seniority` (and a one-line `reasoning` that MUST cite a concrete JD requirement, not generic phrases). `_build_rubric_result` glues the deterministic + LLM dimensions together.

### Ollama JSON parse failures gave 50/100 (`51821dc`)
When OllamaProvider's `score_job` returned malformed JSON, every job ended up at 0.5 / 0.5 / 0.5 = 50. **Invariant**: `score_job` uses `json_mode=True` (and Ollama's `response_format`); failures escalate to errors instead of degrading silently. Same model-name escape: `_check_ollama` bypasses the local-pull check for `*-cloud` / `:cloud` models (Ollama Turbo).

### `mode='demo'` retired but stale state survived (`51821dc` era)
`_default_state["mode"] = "ollama"` now, but session-state rows from before the change still carried `"demo"` and would 400 on `/api/config` validation. **Invariant**: `_load_session_state` coerces stale `mode='demo'` forward to `'ollama'`. `/api/config` whitelist accepts `ollama | anthropic` only. The `DemoProvider` class still exists as the heuristic baseline / Ollama-fallback but isn't user-selectable.

### OAuth state clobbered by `/api/state` polling (`51821dc`)
`/api/auth/google` set `_S["google_oauth_state"]`, but the SPA's 2 s `/api/state` poll completed AFTER and saved its pre-OAuth snapshot, wiping the state. The callback then failed `secrets.compare_digest`. **Invariants**: GET requests AND `/api/state` AND `/api/webhooks/stripe` are in the middleware `skip_save` list; `/api/auth/google` calls `_save_bound_state` explicitly. The SPA's polling `useEffect` is gated on `state.user`.

### `mode='anthropic'` gate bypass via direct phase calls
A user could set `mode='anthropic'` before Claude moved to coming-soon, then re-run a phase. **Invariants**:
- `_run_phase_sse` re-checks `is_developer` against the bound user and aborts non-devs with `code: 'coming_soon'` (belt-and-suspenders against the `/api/config` 503).
- `_load_session_state` proactively migrates non-dev `mode='anthropic'` sessions back to `'ollama'` and snaps free users off `*-cloud` Ollama models — no stale config can leak past load.

### Hardcoded soft-skill list defaults (`9a05dee`) — see above; cross-listed because the underlying anti-pattern (hardcoded lists in extractors) is recurring.

### Dev Ops horizontal scrolling (`b387ec9`)
The Dev Ops Users page forced trackpad-hostile horizontal scroll across the whole console. Root cause: `.dop-body` set `overflow-y: auto` with no `overflow-x` — per the CSS Overflow spec, when one axis is non-visible, the browser auto-promotes the other from `visible` to `auto`. **Invariant**: any container with `overflow-y: auto` MUST set `overflow-x: hidden` AND `min-width: 0` on grid children so 1fr cells can compress past intrinsic content width. Wide `<pre>` blocks use `overflow-wrap: anywhere`.

### LaTeX file-read / shell-escape attacks
`pdflatex` will happily read `\input{/etc/passwd}` and shell out via `\write18`. **Invariant**: `pipeline/latex._sanitize_latex_source` strips `\input`, `\include`, `\openin`, `\openout`, `\read`, `\write`, `\immediate`, `\catcode`, `\directlua`, `\luaexec`, `\ShellEscape`, `\usepackage{shellesc}` before compile. The compile itself runs with `-no-shell-escape` and `-halt-on-error`.

### `psutil` daemon thread at module load
`pytest` collection and bare `python -c "import app"` were spawning a daemon thread that walked psutil at import. **Invariant**: per-core sampler primes inside `@app.on_event("startup")`, never at import; per-process sampling runs on demand inside `_top_processes` (cached 2.5 s).

### Sidebar layouts breaking experience-level inference (`cf59a3e`)
A student with leadership titles ("Chief of Operation" at a film club) was misclassified as `senior` because senior-keyword matching ran before the student-signal rule. **Invariant**: `_infer_experience_levels_from_profile` uses a 5-rule cascade — student-summary signals → intern-in-title → senior-keyword (gated on NOT being an active student) → in-progress-education with low work span → years-of-experience fallback. The 5-year work-span gate prevents misclassifying a senior engineer pursuing an MBA.

### `_apply_profile_search_prefs` not refreshing on primary switch (`cf59a3e`)
Setting a different resume as primary kept stale `location` / `experience_levels` / `education_filter` from the previous primary, producing job postings mis-aimed at the old persona. **Invariant**: the three trigger sites (extraction-complete on primary, set-primary, primary-deletion fallback) call `_apply_profile_search_prefs(force_refresh=True)`. Default is still `force_refresh=False` (fill-blanks-only) for the first upload to preserve user-typed values.

### `/api/jobs/facets` 401 on cold load
The SPA polled `/api/jobs/facets` before `/api/state` resolved the auth cookie, spamming `401 Unauthorized` in journalctl, and the landing page couldn't show real "we have N jobs in your city" stats. **Invariant**: `/api/jobs/facets` is PUBLIC. It returns pure aggregate catalog metadata — "how many jobs in London" / "how many at Stripe" — no PII, no per-user data. Auth-gating it was the bug.

### Server log mirror tee under pytest
The `_StreamTee` wrapped `sys.stdout` at import time and broke pytest's session-end teardown (it captures stdout itself). **Invariant**: the tee is skipped under pytest (`"pytest" in sys.modules`). Same guard applies to the UTF-8 stdout re-wrap in `pipeline/config.py`.

### Cursor-based pagination "stuck" on deduped results
After dedupe + diversification, the cursor's `last_id` could no longer be located in the ranked pool, and `start` reset to 0 — the user got the same first page on every "load more". **Invariant**: cursor lookup falls back to `min(page_offset * limit, len(ranked))` when the id isn't found, so pagination keeps moving. `next_cursor` is emitted whenever ranking *might* have more rows (page-fill OR pool-was-capped) — over-emit and let the next call return `[]`, never under-emit.

> **Future agents**: when you fix a bug worth remembering, add a new entry to this section with the **commit hash**, the **symptom**, the **root cause**, and the **invariant** future code must preserve. The point of this section is to keep the same bug from being reintroduced six months from now by someone who didn't see the original PR. If you can't think of an invariant, the entry probably doesn't belong here — it's just a normal fix.

---

## 10. Agent Session Hygiene — Document Your Work

Future agents (and humans) only inherit what you write down. When you finish a non-trivial session, the project relies on you to leave a paper trail so the next worker doesn't repeat your dead-ends.

### Required artifacts per session

For every commit / agent run, produce all three:

1. **A detailed commit message** in the project's established format. Look at `4debfc9`, `cf59a3e`, `51821dc`, `43baf3d` for templates — they all share the same shape:
   - **Headline**: imperative, ≤ 72 chars, conventional-commit-style prefix (`fix:`, `feat:`, `refactor:`, `docs:`, `chore:`, optionally scoped: `fix(api):`, `feat(jobs):`).
   - **Body sections** with visible separators (`═══` or `───`), each section explaining ONE thing:
     - **Root cause** — the actual mechanism that broke, not just the symptom.
     - **What changed** — file-by-file, with the *why* on each significant change.
     - **Verification** — how you tested (smoke test, unit test, manual repro, etc.).
     - **Things you tried that didn't work** — if you went down a dead-end before finding the fix, mention it in one sentence so the next agent doesn't redo your wrong turns.
     - **Co-Authored-By** trailer when an LLM helped.
   - The headline tells you *what*; the body tells you *why* and *how to undo if needed*.

2. **Documentation updates** in this file (CLAUDE.md). The doc must be accurate the moment your commit lands — don't defer.
   - Architecture change (new module, renamed file, moved DB) → update §1 Repo Layout.
   - New endpoint or route shape → §2 Route Inventory.
   - New `_default_state` key → §2 Config Whitelist + §6 "Add a new user-tunable setting" playbook check.
   - New phase or LLM schema → §3 phase table + provider parity reminder.
   - New JobSource / ingestion change → §3a.
   - New SQLite column / index → §6 "Add a new column" playbook + §3a Migrations note. Verify `pipeline/migrations.py:apply_all_migrations` lists it.
   - Frontend page added/removed → §4 Pages table.
   - New invariant from fixing a bug → §9 Recent Bug History (commit hash, symptom, root cause, invariant).
   - Stripe / billing / plan-tier touch → §8.
   - Cross-cutting policy change → §7 Operational Mandates.

3. **AGENTS.md updates** when the change reshapes the fast-on-ramp story:
   - "Files you'll touch most" / "common task → where to edit" / "where to look first when something breaks" tables.

### What "non-trivial" means

If you can answer YES to any of these, you owe documentation:

- It changes a public REST endpoint, SSE event shape, or `/api/state` field.
- It changes a SQLite schema (column, index, table, or trigger).
- It changes how a phase produces or consumes data.
- It introduces a new external dependency, env var, or runtime knob.
- It changes a security invariant (auth, cookie attrs, CSRF, sandbox, sanitizer).
- It changes a default value the user sees (`_default_state`, CSS token, page list).
- It fixes a bug the next agent could plausibly reintroduce.

Pure formatting / typo / lint passes don't need doc updates — but they should still get a meaningful commit message (`chore: …`).

### Errors and dead-ends matter

Future workers benefit from knowing what *didn't* work. When you:

- Spent time investigating a hypothesis that turned out to be wrong → leave one sentence in the commit body ("Initially suspected X; ruled out by Y").
- Tried a fix that introduced a worse bug → mention it ("First attempt with `try: …; except: pass` swallowed real failures — see `4debfc9` for the pattern that bricked the Pi").
- Found that a library / API behaves differently from its documentation → leave a code comment AND a note in the commit body so the next person doesn't trust the wrong source.
- Discovered a load-bearing invariant the hard way → add it to §9 Recent Bug History so it survives the next refactor.

### Agent-specific notes

- **Don't write narrative status reports as standalone files.** No `SESSION_NOTES.md`, no `WORK_LOG.md` — they rot quickly and nobody reads them. The signal goes in commit messages, CLAUDE.md, and code comments where future agents will actually find it via grep / `git log`.
- **Don't trust your last edit's success without verification.** Re-read what you wrote. For backend changes: run `python app.py` and exercise the path. For frontend changes: hard-refresh `http://localhost:8000/app` and click through. For SQLite changes: `python scripts/migrate_db.py --check` to confirm the schema.
- **When you call `advisor()`** (or its equivalent), surface the advice in the commit body so the next agent sees the reasoning, not just the outcome.
- **When you discover a doc drift while doing other work**, fix it as part of that commit. A "while I was here" doc edit is cheap and prevents the next agent from learning the wrong mental model.

The bar this section sets: a future agent reading `git log -p` + CLAUDE.md should be able to reconstruct WHY every non-obvious decision was made, WHICH dead-ends to avoid, and WHICH invariants the next change must preserve. That's how the codebase stays understandable as it grows.

---

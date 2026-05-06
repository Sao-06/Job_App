# Jobs AI — Architecture & Agent Guide

This file is the canonical map of the codebase. It is consumed by Claude Code (and other AI agents) on every session. Keep it accurate when you change architecture; do not let it drift.

For a fast "where do I start" guide, see `AGENTS.md`. For per-task playbooks, see the *Common Tasks* section below.

---

## 1. Repo Layout (current, verified)

```text
Job_App/
├── agent.py                    # CLI orchestrator (--demo / --ollama). Re-exports pipeline package.
├── app.py                      # FastAPI backend. Primary entry point for the web UI.
├── auth_utils.py               # bcrypt password hashing + Google OAuth helpers.
├── session_store.py            # SQLite-backed multi-user session/state persistence.
├── db.py                       # ⚠ STALE — legacy jobs table; superseded by session_store.
├── check_errors.py             # ⚠ STALE — orphaned Playwright smoke test.
├── requirements.txt
├── jobs.db                     # SQLite file used by db.py (legacy).
│
├── pipeline/                   # CORE LOGIC — 7 phases + providers + extraction.
│   ├── __init__.py             # Public re-exports.
│   ├── config.py               # Constants: OWNER_NAME, OUTPUT_DIR, RESOURCES_DIR, MAX_SCRAPE_JOBS, _CliSpinner.
│   ├── helpers.py              # Dedup, date/edu/citizenship inference, string cleaning.
│   ├── latex.py                # LaTeX detection / plaintext conversion / pdflatex+reportlab compile.
│   ├── phases.py               # phase1_ingest_resume … phase7_run_report; tracker writer; Playwright submit.
│   ├── profile_audit.py        # Post-extraction validation of resume profiles.
│   ├── providers.py            # BaseProvider, AnthropicProvider, OllamaProvider, DemoProvider.
│   ├── resume.py               # PDF/DOCX extraction chain; tailored-resume save; demo resume builder.
│   └── scrapers.py             # JobSpy, SimplifyJobs, Jobright, InternList, Himalayas, Remotive, Arbeitnow.
│
├── frontend/
│   ├── landing.html            # Marketing page served at GET /.
│   ├── index.html              # SPA shell served at GET /app. CSS tokens live in <style>.
│   ├── app.jsx                 # React 18 SPA. Babel-transpiled IN-BROWSER — no build step.
│   ├── hero-bg.png             # Asset.
│   ├── index.original.html     # ⚠ STALE backup.
│   └── preview.html            # ⚠ STALE preview tool.
│
├── dashboard/app.py            # Standalone Flask app for Excel tracker approval flow.
├── Workflow/                   # job-application-agent.md — canonical phase/scoring spec.
├── config/skill_keywords.yaml  # DemoProvider keyword groups (flattened at runtime).
├── resources/                  # JSON caches: profile_cache_*.json, sample_jobs_*.json.
├── output/                     # All generated artifacts.
│   ├── jobs_ai_sessions.sqlite3   # Session store DB (users, sessions, session_state).
│   └── sessions/{session_id}/     # Per-session generated files (resumes, trackers, reports).
└── .claude/agents/             # Sub-agent instruction files for Claude Code.
```

> The README.md mentions `streamlit_app.py`; that file no longer exists. The web UI is `app.py` (FastAPI), not Streamlit.

---

## 2. Backend (`app.py`)

### Session model
- `_SessionStateProxy` (`_S`) is a `MutableMapping` backed by `contextvars.ContextVar` so per-request state is isolated.
- `_bind_request_state(request)` runs early in the request lifecycle: it resolves `dev_impersonate_id` (localhost only) → `jobs_ai_session` cookie → fresh UUID, loads JSON state from SQLite via `session_store`, and binds via `_S.bind(state, session_id)`.
- `_default_state()` defines the schema for a new session. **`mode` defaults to `"ollama"`** (not "demo").
- State is serialized back to SQLite by `_save_bound_state()` (custom JSON handler for `set` and `Path`).

### Auth (`auth_utils.py` + `auth_tokens` table)
- Email/password: bcrypt hash stored in `users` table, session token persisted in the `auth_tokens` SQLite table (`session_store.create_auth_token` / `get_auth_user` / `delete_auth_token`). Tokens survive server restarts and work across uvicorn workers.
- Cookie `jobs_ai_auth` carries the auth token; cookie `jobs_ai_session` carries the state-session id.
- Google OAuth: `get_google_auth_url()` / `verify_google_token()`. If `GOOGLE_CLIENT_ID` is unset, a dummy dev flow is used. The callback verifies `state` with `secrets.compare_digest` against the value persisted in session state — a missing or mismatched state aborts the flow.
- **Server-side invariant:** `POST /api/resume/upload` and `POST /api/resume/demo` call `_require_auth_user(request)`, which 401s any caller without a valid `jobs_ai_auth` cookie. The frontend Auth gate is necessary but not sufficient — the server enforces it independently to prevent ghost profiles.

### Dev mode
- A request is "dev" iff the authenticated caller has `users.is_developer = 1`. Set the flag via `session_store.set_user_developer(user_id, True)`.
- For local-only debugging set the env var `LOCAL_DEV_BYPASS=1` to additionally allow loopback callers without the DB flag.
- `force_customer_mode` in session state forces the customer view regardless.
- Dev Ops endpoints live under `/api/dev/*` and are gated by `_is_dev_request`. There is no `/api/dev/toggle-role` endpoint; toggle the flag from the SQLite shell or by calling `set_user_developer` directly.

### Plan tier (billing)
- `users.plan_tier` is `'free'` | `'pro'` (default `'free'`). Mirrors the `is_developer` end-to-end pattern: DB column → `auth_user` dict → `/api/state` → `state.plan_tier` / `state.is_pro`.
- **Gate**: `POST /api/config` returns 402 if `body.mode === 'anthropic'` and the caller is non-Pro and non-dev. A belt-and-suspenders check inside `_run_phase_sse` aborts SSE phase runs with `code: 'plan_required'` if state was set before a downgrade.
- Free tier covers Demo + local Ollama (full pipeline). Pro unlocks Anthropic Claude. BYOK — Pro users still paste their own `ANTHROPIC_API_KEY`.
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
- **Static**: `GET /` (landing), `GET /app` (SPA), `GET /frontend/{f}`, `GET /output/{path}` (sandboxed download).
- **Auth**: `POST /api/auth/{login,signup,logout}`, `GET /api/auth/google`, `GET /api/auth/google/callback`.
- **Resume**: `POST /api/resume/upload`, `POST /api/resume/demo`, `GET /api/resume/content`, `POST /api/resume/text`, `DELETE /api/resume/{id}`, `POST /api/resume/primary/{id}`, `POST /api/resume/rename/{id}`, `POST /api/resume/tailor` (per-job on-demand tailoring; same shape as one phase-4 item).
- **Config / state**: `POST /api/config` (whitelisted keys only — see below), `GET /api/state`, `POST /api/reset`.
- **Profile**: `GET /api/profile`, `POST /api/profile`, `POST /api/profile/extract`.
- **Jobs / feedback**: `POST /api/jobs/action`, `POST /api/feedback`.
- **Phases (SSE)**: `GET /api/phase/{1..7}/run`, `…/rerun`. Phase 2 has `GET/DELETE /api/phase/2/cache`.
- **Ollama**: `GET /api/ollama/status`, `GET /api/ollama/pull`.
- **Dev Ops**: `/api/dev/overview`, `/api/dev/session/{id}` (GET, reset, delete, impersonate, feedback/read), `/api/dev/cli`, `/api/dev/tweaks`. (No role-toggle endpoint — flip `users.is_developer` directly.)

### `/api/config` whitelist
The config endpoint only accepts a fixed set of keys. When you add a new user-tunable setting, add it to **both** `_default_state()` and the whitelist tuple in `update_config`. Current keys include: `mode`, `api_key`, `ollama_model`, `threshold`, `job_titles`, `location`, `max_apps`, `max_scrape_jobs`, `days_old`, `cover_letter`, `blacklist`, `whitelist`, `experience_levels`, `education_filter`, `include_unknown_education`, `citizenship_filter`, `use_simplify`, `llm_score_limit`, `force_customer_mode`, `light_mode`.

### `/api/reset` semantics
Wipes session state to defaults but **preserves** `user`, `dev_tweaks`, `mode`, `api_key`, `ollama_model`, `light_mode`, `force_customer_mode`. Also deletes generated files in `output/sessions/{session_id}/`. The user stays logged in.

---

## 3. Pipeline (`pipeline/phases.py`)

Each phase is a function that mutates `_S` (via the proxy) and emits log lines to the SSE stream.

| Phase | Function | What it does |
|---|---|---|
| 1 | `phase1_ingest_resume` | `_read_resume` → `provider.extract_profile` → `audit_profile`. Stores `profile`. |
| 2 | `phase2_discover_jobs` | `scrapers.scrape_all` driven by `job_titles`/`location`. Caches under `resources/sample_jobs_*.json`. |
| 3 | `phase3_score_jobs` | Two-stage: fast keyword score → LLM `score_job` for top `llm_score_limit`. |
| 4 | `phase4_tailor_resume` | `provider.tailor_resume` → `_save_tailored_resume` (writes `.tex` + `.pdf`). |
| 5 | `phase5_simulate_submission` | Playwright submit when `--real-apply`; otherwise simulated. |
| 6 | `phase6_update_tracker` | `openpyxl` writes `Job_Applications_Tracker_{YYYY-MM}.xlsx`. |
| 7 | `phase7_run_report` | Markdown summary; optional SMTP email if all `SMTP_*` env vars are present. |

### Providers (`pipeline/providers.py`)
- `BaseProvider`: defines `extract_profile`, `score_job`, `tailor_resume`.
- `AnthropicProvider`: tool-calling for forced JSON; uses `claude-opus-4-x`.
- `OllamaProvider`: OpenAI-compatible client against `http://localhost:11434/v1`.
- `DemoProvider`: pure-Python keyword matching (driven by `config/skill_keywords.yaml`).

### Resume extraction fallback chain (`pipeline/resume.py`)
`pypdfium2` → `pdfplumber` → `pypdf` → `pdfminer.six`. Always verify `pypdfium2` import is intact when touching this file.

### PDF generation
1. `pdflatex` if on `PATH`.
2. `reportlab` `SimpleDocTemplate` fallback.

---

## 4. Frontend (`frontend/app.jsx`)

- React 18 SPA. **Transpiled in-browser by Babel standalone** (`<script type="text/babel">`). There is no build step — edits to `app.jsx` are live after a hard browser refresh.
- Single root state object fetched from `/api/state` and refreshed via `refresh()` on a 2 s/8 s adaptive poll.
- Page state is a string (`'home' | 'jobs' | 'resume' | 'profile' | 'agent' | 'dev' | 'feedback' | 'settings' | 'auth'`).
- API helper `api` (top of file) wraps `fetch` and unwraps `{detail, error, message}` errors.
- `runPhaseSSE(n, …)` opens an `EventSource` against `/api/phase/n/{run|rerun}` and dispatches `start | log | done | error` callbacks.

### Theming
- All colors are CSS custom properties on `:root` in `index.html`.
- `:root[data-theme="light"]` overrides those tokens. The `light_mode` setting toggles `document.documentElement.dataset.theme` via a `useEffect` in `App()`.
- Brand: dark indigo/purple. Accent `--accent: #5e6ad2`. Use the `<Icon name="…"/>` wrapper around `lucide` icons.

### Gating logic (App component)
1. **Booted gate** — wait for first `/api/state` response.
2. **Auth gate** — non-home, non-dev pages require `state.user`.
3. **Onboarding gate** — without a resume, show `<Onboarding/>`. **Must require `state.user` first**, otherwise unauthenticated users create ghost profiles in the Dev Ops user list.

---

## 5. Persistence (`session_store.py`)

- SQLite at `output/jobs_ai_sessions.sqlite3` (WAL mode).
- Tables:
  - `users(id PK, email UNIQUE, password_hash, google_id, is_developer, created_at)`
  - `sessions(id PK, user_id FK, created_at, updated_at)`
  - `session_state(session_id PK, state_json TEXT, updated_at)`
  - `auth_tokens(token PK, user_id FK, user_json, created_at)`
- `default_state_factory` is injected at construction (it's `_default_state` from `app.py`), so adding a new state key requires updating that function only.
- `json_default` handles `set` and `Path`; `normalize_state` re-hydrates them on load (`done`, `liked_ids`, `hidden_ids`, `extracting_ids`). Adding a new set-typed state key requires updating both `normalize_state` and `_load_session_state` so the set survives the JSON round-trip.

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
- Don't loosen it without checking the *Onboarding gate* — they cooperate. See `feedback_auth_before_upload.md` in the agent memory directory.

### Add a new column to a SQLite table (so existing prod DBs auto-migrate)
1. Add the column to the `CREATE TABLE` in either `session_store._init_db` (for `users` / `sessions` / `auth_tokens` / `session_state`) or `pipeline/job_repo._SCHEMA_SQL` (for `job_postings` / `source_runs`). This handles fresh DBs.
2. **Add a matching `ensure_column(...)` call to `pipeline/migrations.py:apply_all_migrations`.** This handles existing prod DBs (the Pi).
3. If the column needs an INDEX, add `ensure_index(...)` AFTER the `ensure_column` in the same function. NEVER put the CREATE INDEX in `_SCHEMA_SQL` if it references a not-yet-migrated column — it'll crash on stale DBs (`CREATE INDEX IF NOT EXISTS` still validates referenced columns).
4. Deploy: `git pull && sudo systemctl restart jobapp`. The startup hook `_ensure_schema_migrations` in `app.py` runs migrations on boot and logs each step to journalctl. NEVER manually `sqlite3 ... "ALTER TABLE…"` on the Pi after the first time — the migration framework owns this now.
5. To verify post-deploy: `python scripts/migrate_db.py --check` shows the live schema; without `--check` it runs migrations and reports steps.

---

## 7. Operational Mandates

1. **Secrets**: never commit `ANTHROPIC_API_KEY`, `GOOGLE_CLIENT_SECRET`, or any `sk-…` literal. The Anthropic key lives in volatile session state or env var only.
2. **Cross-platform paths**: always derive paths from `Path(__file__)` (this repo runs on Windows and Linux). Don't hardcode separators.
3. **Frontend has no build step**: do NOT introduce webpack/vite/tsc unless you also commit the build artefact and update `index.html`. JSX is in-browser Babel.
4. **PDF engine order**: `pypdfium2` is primary. If you reorder the fallback chain, update `pipeline/resume.py` AND this doc.
5. **Run the server before reporting frontend changes done**: `python app.py` (or `uvicorn app:app --reload --port 8000`), open `http://localhost:8000/app`, exercise the actual feature.
6. **Stale files** (`db.py`, `check_errors.py`, `streamlit_app.py` reference, `frontend/index.original.html`, `frontend/preview.html`) — leave them alone unless the user asks for cleanup.

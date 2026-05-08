# AGENTS.md — Orientation for AI agents

This file is the fast on-ramp. It assumes you've never seen the repo and need to be useful in the next ten minutes. For full architecture detail read `CLAUDE.md` after this.

---

## What this app is

Jobs AI is a multi-user FastAPI web app + 7-phase Python pipeline. A continuously-refreshed local job index (~20 keyless + keyed sources, APScheduler-driven) feeds a profile-ranked feed and 7 phases that ingest a resume, score jobs, tailor materials, curate top picks, write a tracker, and generate a run report.

It runs as:
- A **FastAPI web app** (`python app.py`, browser at `http://localhost:8000/app`) — this is the primary mode and where active development happens.
- A **CLI** (`python agent.py [--ollama|--demo|--real-apply|--dashboard]`).
- A **standalone Flask tracker viewer** (`python dashboard/app.py`, port 5000) for approving Manual Required rows.

LLM backend is selectable per session: **Free** runs local Ollama models on the Pi server; **Pro** unlocks the cloud Ollama models (`*-cloud` names, transparently proxied to Ollama Turbo). **Anthropic Claude** is under active development and reserved for developers — it will ship in Pro when it launches. The Demo / heuristic provider is no longer user-selectable but powers the regex baseline + Ollama fallback.

---

## The files you'll touch most

| File | When |
|---|---|
| `app.py` | Backend routes, state schema, auth, SSE phase runners, Dev Ops, Stripe webhook. Most backend changes. |
| `frontend/app.jsx` | React SPA. All UI changes. Babel-transpiled in-browser — **no build step**. |
| `frontend/index.html` | CSS tokens (`:root` + `:root[data-theme="light"]`), HTML shell. |
| `pipeline/phases.py` | The 7 phase functions. Pipeline behavior changes. |
| `pipeline/providers.py` | LLM provider classes + deterministic `compute_skill_coverage`. JSON schema changes for `extract_profile` / `score_job` / `tailor_resume`. |
| `pipeline/job_search.py` | Live feed ranking — BM25 + skill_overlap + freshness + title_match weights, dedupe, diversification. |
| `pipeline/sources/*.py` | Adding a new job source — implement `JobSource` Protocol; auto-registers on import. |
| `pipeline/migrations.py` | Adding a SQLite column or index — single source of truth. NEVER inline `try: ALTER…; except: pass`. |
| `pipeline/heuristic_tailor.py` | The deterministic tailoring safety net + `validate_tailoring` shape rejector. |
| `session_store.py` | `users` / `sessions` / `auth_tokens` schema + Stripe customer ids + token digest helpers. |

---

## Mental model in one paragraph

A request hits FastAPI → `session_state_middleware` binds a per-session state dict (loaded from `data/jobs_ai_sessions.sqlite3` for authenticated sessions, in-memory for anonymous) into a `contextvars`-backed proxy `_S` → the route reads/writes `_S[…]` like a normal dict → the middleware persists back to SQLite at end-of-request, but only for non-GET, non-`/api/state`, non-webhook requests AND only when `state.user.id|email` is set (anonymous sessions never INSERT). Meanwhile, `pipeline/ingest.py` runs an APScheduler `BackgroundScheduler` in the same process, ticking each registered `JobSource` on its own cadence to keep `job_postings` fresh; the SPA hits `/api/jobs/feed` for a profile-ranked view. The frontend polls `/api/state` every 2–8 s for the whole state and re-renders. Long-running phases stream over SSE at `/api/phase/{n}/run`. There is **one** big state dict per session; adding a feature usually means adding a key to `_default_state()`, the `update_config` whitelist, the `/api/state` payload, and the SettingsPage in `app.jsx`.

---

## Run it

```powershell
# Backend
python app.py            # http://localhost:8000

# Or with reload
uvicorn app:app --reload --port 8000

# Frontend: nothing to do — the FastAPI app serves it.
# After editing frontend/app.jsx, do a HARD browser refresh.
```

Localhost connections are auto-treated as developer sessions (Dev Ops console enabled).

---

## Conventions to respect

1. **Frontend has no build step.** Do not introduce webpack/vite/tsc. Do not generate a `dist/`. JSX runs through Babel standalone in the browser.
2. **All CSS is variables.** Don't hardcode hex colors in `app.jsx` — reference `var(--accent)` etc. (`#7c5cff`). Light mode is implemented purely by overriding `:root` tokens via `[data-theme="light"]`.
3. **State is one big dict.** Don't invent parallel storage. Add to `_default_state()` in `app.py` AND the `update_config` whitelist AND `/api/state`.
4. **Auth must precede upload.** The server enforces this independently via `_require_auth_user(request)` on `/api/resume/{upload,demo}` AND every other write endpoint — the frontend gate is necessary but not sufficient. Don't loosen it.
5. **Anonymous sessions never persist.** `_save_bound_state` early-returns to memory when `user.id|email` is unset; `peek_state` is read-only. Touching either of these to "fix" something else will reintroduce the ghost-profiles failure.
6. **Migrations go through `pipeline/migrations.py`.** ALWAYS use `ensure_column` / `ensure_index`. NEVER inline `try: ALTER…; except sqlite3.OperationalError: pass` — that pattern silently swallowed real failures and bricked the Pi for months.
7. **No fabrication in tailoring.** `skills_reordered` only ever reorders existing user skills. Missing JD requirements MUST go in `ats_keywords_missing`, never silently appended.
8. **No hardcoded "default" search prefs that lie about the user.** `job_titles` / `location` / `experience_levels` / `education_filter` start EMPTY and are filled from the resume after Phase 1. Never reintroduce `Engineer` / `United States` / `bachelors` placeholders.
9. **Phase 5 is curation, not auto-submit.** The randomized "Applied" coin-flip stub was deliberately removed. Don't bring it back.
10. **Cross-platform paths.** Use `Path(__file__).parent` style. The repo runs on Windows AND on the Linux production box / Pi.
11. **Provider parity.** When you change a JSON schema, change it in **all three** providers (Anthropic, Ollama, Demo) in `pipeline/providers.py`. The validator in `heuristic_tailor.validate_tailoring` is the safety net for low-end Ollama models — keep it in sync.
12. **Auth tokens are SHA-256 digested at rest** in `auth_tokens.token`. Don't reintroduce raw-token storage; the boot-time purge enforces this (deletes any row whose token isn't 64 hex chars).
13. **Stripe webhook is the source of truth** for `plan_tier`. The success-URL redirect alone is just a UI hint. The webhook is HMAC-verified, in `skip_save`, and routes through `_apply_plan_change` (writes the DB column AND every cached `auth_tokens.user_json`).
14. **No comment noise.** Add a comment only when *why* is non-obvious.
15. **Document your work before you stop.** Every non-trivial commit needs (a) a detailed commit body in the project's established format — see `4debfc9`, `cf59a3e`, `51821dc`, `43baf3d` for templates with section separators, root cause / what changed / verification / dead-ends; (b) updates to the relevant CLAUDE.md sections (architecture, route inventory, mandates, Bug History) so the doc never drifts past your commit; (c) a one-sentence note in the body about anything that *didn't* work, so the next agent doesn't redo your wrong turns. **Don't write standalone session-notes files** — they rot. The signal lives in commit messages, CLAUDE.md, and code comments where `git log` + grep will find it. See CLAUDE.md §10 for the full discipline.

---

## Common task → where to edit

| Goal | Touch |
|---|---|
| New settings toggle | `_default_state()` + `update_config` whitelist + `/api/state` payload + `SettingsPage` in `app.jsx` |
| New SPA page | hash route in `App()` switch + Rail item + (optional) new `/api/…` route. Don't forget `VALID_PAGES`. |
| Tweak a phase | the function in `pipeline/phases.py` (and provider JSON schema in ALL THREE providers if shape changes) |
| Theme change | CSS tokens in `frontend/index.html` (both `:root` and `:root[data-theme="light"]`) |
| Add a job source | new module under `pipeline/sources/` implementing `JobSource` Protocol; eager-import it from `pipeline/sources/__init__.py` so `register()` runs |
| Add a SQLite column | `ensure_column(...)` in `pipeline/migrations.apply_all_migrations` AND the corresponding `CREATE TABLE` in `session_store._init_db` or `pipeline/job_repo._SCHEMA_SQL`. If the column needs an index, add `ensure_index(...)` AFTER the `ensure_column` in the same function. NEVER put a `CREATE INDEX` referencing a not-yet-migrated column in `_SCHEMA_SQL` |
| Dev Ops endpoint | add `@app.{get,post,delete}("/api/dev/…")` guarded by `_is_dev_request` (or `_is_underlying_dev_request` for endpoints a customer-simulating dev needs to reach). Document in CLAUDE.md route inventory. |
| Tweak ranking | `pipeline/job_search.search` (BM25 + skill_overlap + freshness + title_match weights), `_dedupe_by_listing`, `_diversify_by_company`, `_diversify_by_category` |
| Stripe wiring | `pipeline/stripe_billing.py` for SDK calls; `app.py` `/api/billing/*` and `/api/webhooks/stripe`; the Plans page swap is at `frontend/app.jsx:7113` |

---

## Known stale / ignore list

- `db.py`, `jobs.db` — legacy jobs SQLite, replaced by `session_store.py`.
- `check_errors.py` — orphan Playwright smoke test.
- `pipeline/scrapers.py` — early live scrapers; superseded by `pipeline/sources/` (auto-registering JobSource providers + APScheduler ingestion). No importers.
- `streamlit_app.py` — referenced in old commit messages but does not exist; Streamlit was removed.
- `frontend/index.original.html`, `frontend/preview.html` — backups.

Don't delete these unless the user asks.

---

## Where to look first when something breaks

| Symptom | Look at |
|---|---|
| Upload returns 200 but profile is empty | `_run_extraction_bg` thread + `pipeline/resume.py` extractor chain. Run `GET /api/profile/diagnose?id=` for a heuristic-only view (no LLM call). |
| Sidebar resume returned a city as the name | Known fixed bug — `_extract_name_from_text` is now section-aware. Re-scan the resume. |
| Page won't load past spinner | `/api/state` 500 — check terminal traceback. The middleware converts pre-route exceptions to JSON 500s so the SPA stops choking on plaintext "Internal Server Error". |
| Phase hangs or never emits `done` | Thread exception in `_run_phase_sse`; logs land in the SSE stream. Per-session phase concurrency is capped at 1. |
| 503 / `code: coming_soon` on `mode='anthropic'` | Claude is under active development — only developers can select it. Switch to Ollama in Settings, or grant developer flag via Dev Ops if testing the integration. |
| 402 / `code: plan_required` on `*-cloud` Ollama model | Caller is non-Pro and tried a cloud model. Flip plan via Dev Ops PLAN panel or wait for Stripe webhook. |
| Anthropic provider 401 | `_S["api_key"]` empty — set it via the developer-only field in SettingsPage. |
| Ollama errors | `GET /api/ollama/status` — confirm daemon is running at `${OLLAMA_URL}`. `*-cloud` models bypass the local-pull check. `score_job` uses `json_mode=True`; malformed JSON now fails loud instead of falling back to neutral 0.5. |
| Jobs feed empty | Local index might be empty on first boot. Check `journalctl` for "[ingest]" lines. `POST /api/jobs/source-status` to force a re-tick. |
| Theme didn't switch | `document.documentElement.dataset.theme` — hard-refresh the browser. The `useEffect` skips no-op DOM writes. |
| Login bypassed; ghost user appears | Server `_require_auth_user` enforcement broke. Check `app.py` for `_require_auth_user(request)` on `/api/resume/{upload,demo}` and every other write endpoint. |
| Auth cookie present but `/api/state` returns no user | Token rotation purge fired (legacy plaintext rows deleted). One re-login fixes it. Look for `[api/state] auth cookie present but lookup returned None` in stderr. |
| `no such column: jp.X` on the Pi after deploy | Migration didn't run. Check `journalctl -u jobapp -n 50` for `[migrations]` lines. `python scripts/migrate_db.py` to force. |
| `/api/jobs/facets` 401 spam in logs | You're on an older build — the endpoint is now PUBLIC. Pull and restart. |
| Stripe webhook never fires | Public reachability — Stripe can't hit `localhost`. Use `stripe listen --forward-to`. The `whsec_…` from `stripe listen` is DIFFERENT from the dashboard secret. |
| Pro user still seeing `plan_tier=free` | `_apply_plan_change` didn't refresh `auth_tokens.user_json`. Check `refresh_user_plan_in_tokens` was called. Worst case, force re-login. |

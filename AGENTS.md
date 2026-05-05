# AGENTS.md — Orientation for AI agents

This file is the fast on-ramp. It assumes you've never seen the repo and need to be useful in the next ten minutes. For full architecture detail read `CLAUDE.md` after this.

---

## What this app is

Jobs AI is a 7-phase Python pipeline that ingests a resume, scrapes/scores jobs, tailors application materials, simulates submission, writes an Excel tracker, and generates a run report. It runs as either:
- A **CLI** (`python agent.py [--demo|--ollama]`)
- A **FastAPI web app** (`python app.py`, browser at `http://localhost:8000/app`) — this is the main mode.

LLM backend is selectable: Anthropic Claude, local Ollama, or a no-API Demo mode.

---

## The five files you'll touch most

| File | When |
|---|---|
| `app.py` | Backend routes, state schema, auth, SSE phase runners. Almost every backend change. |
| `frontend/app.jsx` | React SPA. All UI changes. Babel-transpiled in-browser — **no build step**. |
| `frontend/index.html` | CSS tokens (`:root` + `:root[data-theme="light"]`). HTML shell. |
| `pipeline/phases.py` | The 7 phase functions. Pipeline behavior changes. |
| `pipeline/providers.py` | LLM provider classes. JSON schema changes for `extract_profile` / `score_job` / `tailor_resume`. |

---

## Mental model in one paragraph

A request hits FastAPI → middleware binds a per-session state dict (loaded from `output/jobs_ai_sessions.sqlite3`) into a `contextvars`-backed proxy `_S` → the route reads/writes `_S[…]` like a normal dict → the response handler serializes `_S` back to SQLite. The frontend polls `/api/state` every 2–8 s for the whole state and re-renders. Long-running phases stream over SSE at `/api/phase/{n}/run`. There is **one** big state dict per session; adding a feature usually means adding a key to `_default_state()`, exposing it in `/api/state`, accepting it in `/api/config`, and rendering it in `app.jsx`.

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
2. **All CSS is variables.** Don't hardcode hex colors in `app.jsx` — reference `var(--accent)` etc. Light mode is implemented purely by overriding `:root` tokens via `[data-theme="light"]`.
3. **State is one big dict.** Don't invent parallel storage. Add to `_default_state()` in `app.py`.
4. **Auth must precede upload.** The Onboarding (resume upload) gate must require `state.user`. Otherwise an unauthenticated upload creates a ghost profile visible in Dev Ops. See `CLAUDE.md` §4.
5. **Cross-platform paths.** Use `Path(__file__).parent` style. The repo runs on Windows.
6. **Provider parity.** When you change a JSON schema, change it in **all three** providers (Anthropic, Ollama, Demo) in `pipeline/providers.py`.
7. **No comment noise.** Add a comment only when *why* is non-obvious.

---

## Common task → where to edit

| Goal | Touch |
|---|---|
| New settings toggle | `_default_state()` + `update_config` whitelist + `/api/state` payload + `SettingsPage` in `app.jsx` |
| New SPA page | switch in `App()` + Rail item + (optional) new `/api/…` route |
| Tweak a phase | the function in `pipeline/phases.py` (and provider JSON schema if shape changes) |
| Theme change | CSS tokens in `frontend/index.html` (both `:root` and `[data-theme="light"]`) |
| New scraper | new class in `pipeline/scrapers.py`, register in `scrape_all` |
| Dev Ops endpoint | add `@app.{get,post,delete}("/api/dev/…")` guarded by `_ensure_dev` |

---

## Known stale / ignore list

- `db.py`, `jobs.db` — legacy jobs SQLite, replaced by `session_store.py`.
- `check_errors.py` — orphan Playwright test.
- `streamlit_app.py` — referenced in README but does not exist.
- `frontend/index.original.html`, `frontend/preview.html` — backups.

Don't delete these unless the user asks.

---

## Where to look first when something breaks

| Symptom | Look at |
|---|---|
| Upload returns 200 but profile is empty | `_run_extraction_bg` thread + `pipeline/resume.py` extractor chain |
| Page won't load past spinner | `/api/state` 500 — check terminal traceback |
| Phase hangs or never emits `done` | thread exception in `_run_phase_sse`; logs land in the SSE stream |
| Anthropic provider 401 | `_S["api_key"]` empty — set it via SettingsPage |
| Ollama errors | `GET /api/ollama/status` — confirm `ollama serve` is running on `:11434` |
| Theme didn't switch | `document.documentElement.dataset.theme` — hard-refresh the browser |
| Login bypassed; ghost user appears | the Onboarding gate is missing the auth check (see `CLAUDE.md` §4) |

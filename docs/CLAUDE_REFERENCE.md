# Jobs AI — Deep Reference

This file holds the long-form reference content extracted from `CLAUDE.md` to keep that file under the 40k auto-load threshold. Load this on demand when working on Stripe / billing, when triaging a bug that smells like a reintroduced regression, or when preparing a non-trivial commit.

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

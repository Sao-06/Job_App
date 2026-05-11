# Claude CLI Provider — Design Spec

**Status**: approved (brainstorming complete, ready for implementation plan)
**Date**: 2026-05-11
**Owner**: Jonathan

## 1. Motivation

Replace the `anthropic` Python SDK transport in `pipeline/providers.py:AnthropicProvider` with subprocess calls to the locally-installed `claude` CLI (Claude Code). The CLI authenticates against the OS keychain using a Claude **subscription** (OAuth), so the entire app's Claude usage is funded by one flat subscription instead of pay-per-token API charges.

This unlocks **Claude as the Pro tier**: Free users continue to get local Ollama; Pro users now get Claude (Sonnet 4.6) via the CLI subprocess. The existing `*-cloud` Ollama option remains as a parallel Pro entitlement.

## 2. Scope

### In scope
- Rewrite `pipeline/providers.py:AnthropicProvider` to shell out to `claude -p` for every method (`chat`, `extract_profile`, `score_job`, `tailor_resume`, `generate_cover_letter`, `generate_report`, `generate_demo_jobs`).
- Preserve streaming for `_stream_provider_chat` in `app.py` (Atlas chat) using `--output-format stream-json --include-partial-messages`.
- Pin model to Sonnet 4.6 (`--model sonnet`) by default to protect subscription rate limits. Overridable via `CLAUDE_CLI_MODEL` env var.
- Parallelize `phase3_score_jobs` LLM scoring of the top-N jobs via `ThreadPoolExecutor(max_workers=5)`.
- Flip the four `mode='anthropic'` plan-gate sites in `app.py` from `is_developer`-only to `is_developer or plan_tier == 'pro'`.
- Drop `anthropic` from `requirements.txt` (two-commit sequence — keep installed during the first soak window for clean revert).
- Drop the `api_key` field from session state, `/api/config` whitelist, `/api/state`, and SettingsPage UI.
- Update CLAUDE.md, docs/CLAUDE_REFERENCE.md, and the Plans / Settings / TailorDrawer frontend copy.
- Delete `scripts/check_anthropic_key.py` (replaced by a startup health-check).

### Out of scope (deferred)
- Long-running pooled `claude` processes per user session (`--input-format stream-json`). Cold start per call is acceptable on Pi 4 with the `Semaphore(5)` cap.
- Parallelization of other phases (only Phase 3 has the multi-call hot path that benefits).
- A RAM-aware watchdog before subprocess spawn. The fixed semaphore is sufficient at current scale.
- Multi-model strategy (Opus for tailoring, Sonnet elsewhere). Single model keeps the design tight; revisit if Sonnet's tailoring quality regresses noticeably.

## 3. Approach Chosen

**Approach A — in-place rewrite** of `AnthropicProvider`. The class name and `mode='anthropic'` wire value are preserved; only the internal transport changes. Minimizes touch points across gate sites, state migrations, SPA copy, and tests.

Rejected alternatives:
- **Approach B**: new `ClaudeCLIProvider` class + rename to `mode='claude'` everywhere. Cleaner naming, ~4× the touch points, higher chance of leaving stale `mode='anthropic'` sessions stuck.
- **Approach C**: extract a `pipeline/claude_cli.py` runner module. Cleaner test boundary, but new test infrastructure for a layer that's barely covered today anyway.

## 4. File Map

```
pipeline/providers.py          rewrite AnthropicProvider (lines 355-~1010)
                                — drop `import anthropic`
                                — _output_config / SDK kwargs removed
                                — every method calls new _run_cli() helper
                                — new module-level constants:
                                    CLAUDE_BIN, CLAUDE_CLI_MODEL,
                                    CLAUDE_DEFAULT_TIMEOUT,
                                    _CLI_SEMAPHORE, _CLI_HEALTHY
                                — new schema constants:
                                    EXTRACT_PROFILE_SCHEMA,
                                    SCORE_JOB_SCHEMA,
                                    TAILOR_RESUME_SCHEMA
                                — class MODEL constant flips to
                                  "claude-sonnet-4-6"

pipeline/phases.py             rewrite phase3_score_jobs loop into
                                ThreadPoolExecutor(max_workers=5) +
                                as_completed pattern with per-future
                                timeout=180s and _fast_score fallback
                                on per-job failure.

requirements.txt               remove `anthropic>=0.88.0` line
                                (in a SECOND commit after soak)

.env.example                   remove ANTHROPIC_API_KEY block + comment

app.py
  _default_state               drop `api_key` field
  update_config                drop `api_key` from whitelist;
                                replace inline is_developer check with
                                _can_use_claude(auth_user);
                                error code becomes "plan_required" (402)
  _load_session_state          remove api_key migration; replace
                                non-dev anthropic coerce with
                                not _can_use_claude(user) coerce
  _run_phase_sse               replace is_developer check with
                                _can_use_claude(auth_user)
  resume_tailor                same — replace is_developer with
                                _can_use_claude(auth_user)
  _stream_provider_chat        AnthropicProvider branch rewritten to
                                spawn subprocess with --output-format
                                stream-json, parse JSONL deltas off
                                stdout, yield text. Cleanup in finally.
  GET /api/state               drop `api_key` from response
  _can_use_claude              NEW helper — single source of truth

  @app.on_event("startup")     boot health check via _run_cli ping;
                                sets _CLI_HEALTHY = bool(success).
                                Apscheduler tick every 5 min refreshes.

scripts/check_anthropic_key.py DELETE

frontend/app.jsx
  SettingsPage                 remove "Anthropic API key" input row.
                                Anthropic radio: enabled for Pro,
                                disabled-with-tooltip for Free.
  TailorDrawer                 remove "Soon" pill on Claude
  PlansPage                    Pro card: add Claude bullet.
                                Remove "Anthropic — coming soon" notes.

docs/CLAUDE_REFERENCE.md       §3 Providers — rewrite AnthropicProvider
                                block to describe CLI transport.
                                §7 Anthropic launch state — rewrite to
                                "Pro tier — Sonnet 4.6 via CLI subprocess.
                                OAuth keychain auth. Flip at
                                _can_use_claude in app.py."
                                §9 Bug History — new entry: CWD must NOT
                                contain a CLAUDE.md or it leaks into
                                every prompt.

CLAUDE.md                       §3 Providers — update AnthropicProvider
                                bullet (Sonnet 4.6 / CLI / OAuth).
                                §2 plan-tier — note new _can_use_claude
                                helper and Pro tier expansion.

tests/conftest.py              add `claude_cli_bin` fixture (fake
                                shell script on $PATH).

tests/unit/pipeline/
  test_claude_cli.py           NEW — _run_cli unit coverage
  test_anthropic_provider.py   REWRITE — patches _run_cli, verifies
                                argv building + JSON parsing
  test_anthropic_provider_sdk.py  DELETE if exists (legacy SDK shape)

tests/integration/
  test_plan_gates.py           NEW — free=402, pro=200, dev=200 on
                                mode='anthropic'
  test_phase3_parallel.py      NEW — 10-job parallel scoring wall
                                time < 1s w/ 200ms mock; failure
                                isolation falls back to _fast_score

tests/test_app_config.py       drop api_key whitelist cases
```

## 5. Subprocess Contract — `_run_cli`

Single private helper, the chokepoint for every Anthropic-equivalent call.

```python
def _run_cli(
    prompt: str,
    *,
    system: str | None = None,
    json_schema: dict | None = None,
    effort: str = "high",       # low | medium | high | xhigh | max
    timeout_s: float = 120.0,
    budget_usd: float = 2.00,
) -> str:
    """Blocking. Returns assistant text. Raises ClaudeCLIError on failure.
    Acquires _CLI_SEMAPHORE for the duration of the subprocess."""
```

Note: the CLI does not expose a per-call `max_tokens` flag (only
`--max-budget-usd` as a cost ceiling). The vestigial `max_tokens` kwarg
from the SDK signature is dropped from `_run_cli`. Each call-site that
still wants to advertise a token budget for upstream display purposes
can carry its own constant separately from the CLI invocation.

### argv assembly

```
{CLAUDE_BIN}
  -p {prompt}                          # or via stdin if len > 64KB
  --append-system-prompt {system}      # only if system provided
  --json-schema {json}                 # only if schema provided
  --effort {effort}
  --model {CLAUDE_CLI_MODEL}           # default "sonnet"
  --output-format text                 # streaming branch uses stream-json
  --disable-slash-commands
  --max-budget-usd 2.00
  --exclude-dynamic-system-prompt-sections
```

**Explicitly NOT used**:
- `--bare` (would require ANTHROPIC_API_KEY; defeats subscription auth)
- `--continue` / `--resume` (each call is stateless)
- `--mcp-config`, `--add-dir`, `--allowedTools` (no tool use needed)

### env / cwd

- Env: inherit, plus `CLAUDE_CODE_NONINTERACTIVE=1` as a belt-and-suspenders hint. `HOME` must pass through (keychain location).
- `cwd=/tmp/jobapp-claude/` — a dedicated scratch dir that intentionally contains **no** `CLAUDE.md`. Critical: the CLI auto-discovers CLAUDE.md in CWD and prepends it to system. `--exclude-dynamic-system-prompt-sections` is the belt; clean CWD is the suspenders.
- stdin: closed unless prompt > 64KB.

### Error mapping

| Exit code | Cause | Raised exception |
|---|---|---|
| 0 | Success | none — returns stdout |
| 1 | Auth missing, model overloaded, schema mismatch | `ClaudeCLIError(msg, stderr=...)` |
| 124 | Our timeout fired | `ClaudeCLITimeoutError` |
| 130 | SIGINT (supervisor kill) | `ClaudeCLIError("interrupted")` |
| other | Unexpected | `ClaudeCLIError(...)` |

Stderr is captured and surfaced inside the exception message so failures land in journalctl with full context.

### Per-call timeouts

| Caller | Timeout |
|---|---|
| `chat` (one-shot) | 120s |
| `extract_profile` | 120s |
| `score_job` | 120s (per future); 180s Phase 3 future timeout |
| `tailor_resume` | 240s (xhigh effort is slower) |
| Streaming branch | unbounded (closes with connection) |

## 6. JSON-Schema Strategy

The current SDK code uses forced tool-calling to extract strict JSON from `extract_profile / score_job / tailor_resume`. The CLI replaces this with `--json-schema=<json>`, which validates output against a JSON Schema before returning.

The three schemas are extracted from their current Python-dict tool-input locations into module-level constants:

```python
EXTRACT_PROFILE_SCHEMA = { ... }   # target_titles, work_experience,
                                    # education, skills, etc.
SCORE_JOB_SCHEMA       = { ... }   # required_skills, industry,
                                    # location_seniority, matching_skills,
                                    # missing_skills, reasoning
TAILOR_RESUME_SCHEMA   = { ... }   # summary_rewrite, skills_reordered,
                                    # experience_bullets,
                                    # ats_keywords_missing, cover_letter
```

`chat(json_mode=True)` passes `{"type": "object", "additionalProperties": true}` — caller hasn't specified shape, just wants valid JSON.

`chat(json_mode=False)` omits `--json-schema` entirely.

**Validation safety net intact**: `heuristic_tailor.validate_tailoring` still runs on `tailor_resume` output, one-retry path still works, heuristic-fallback still kicks in on persistent failure. No behavior change downstream of the JSON parse.

**Effort levels preserved**: `tailor_resume` passes `effort="xhigh"`. Effort and model are independent dials; xhigh on Sonnet remains meaningful.

**What's lost**: explicit `cache_control: ephemeral` prompt caching across calls. Each `claude -p` is a fresh subprocess that cannot reach back across invocations. Anthropic's server-side cache still hits for identical prefixes within a short window, but we lose direct control. Net impact on subscription tier is minor (flat billing) — the only risk is rate-limit headroom for power users tailoring back-to-back, bounded by the semaphore (5) + per-call budget cap ($2).

## 7. Streaming Branch

`_stream_provider_chat` in `app.py:4368-4385` is the only streaming caller (Atlas chat). The new `AnthropicProvider` branch:

```python
proc = subprocess.Popen(
    _build_argv(
        prompt=_collapse_history(messages, system),
        system=system,
        stream=True,
    ),
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
    env=_cli_env(), cwd=_CLI_SCRATCH,
)
try:
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "stream_event":
            inner = evt.get("event", {})
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text") or ""
                    if text:
                        yield text
        elif evt.get("type") == "result" and evt.get("subtype") != "success":
            raise ClaudeCLIError(evt.get("error") or "Claude CLI failed")
finally:
    if proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=2)
        except subprocess.TimeoutExpired: proc.kill()
```

**History collapse** — the SDK took `messages: list[{role, content}]`; the CLI takes a single prompt string. `_collapse_history(messages, system)` serializes prior turns as a transcript:

```
[Previous conversation]
User: <prior user msg>
Assistant: <prior reply>
...

[Current message]
<latest user msg>
```

System prompt goes via `--append-system-prompt`. For 2-3 turn Atlas advisor conversations this is functionally equivalent to native multi-turn.

**Cancellation**: frontend already uses `AbortController` on the manual `ReadableStream` parser. SSE close → FastAPI tears down the generator → `finally` block kills subprocess. No orphan processes.

**Stderr**: drained on a background thread into a small ring buffer; surfaced only on non-zero exit. Don't interleave with stdout — would break the JSON parser.

## 8. Concurrency Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  module-global (providers.py)                                    │
│  _CLI_SEMAPHORE = threading.BoundedSemaphore(N)                  │
│  N = int(env "CLAUDE_CLI_MAX_CONCURRENCY", default=5)            │
│  Every _run_cli() and stream branch acquires this.               │
│  Hard ceiling on total concurrent `claude` subprocesses.         │
└──────────────────────────────────────────────────────────────────┘
                       ▲                ▲
                       │                │
        ┌──────────────┴────────┐   ┌───┴──────────────────────────┐
        │ phase3_score_jobs     │   │ extract_profile, tailor,     │
        │ ThreadPoolExecutor    │   │ chat, atlas stream           │
        │ (max_workers=5)       │   │                              │
        │ One future per job in │   │ Single-call, blocking        │
        │ top-N; workers call   │   │                              │
        │ provider.score_job(). │   │                              │
        │ Each future acquires  │   │                              │
        │ _CLI_SEMAPHORE.       │   │                              │
        └───────────────────────┘   └──────────────────────────────┘
```

### Phase 3 rewrite

```python
# pipeline/phases.py:phase3_score_jobs (replacing serial loop)
from concurrent.futures import ThreadPoolExecutor, as_completed

with ThreadPoolExecutor(
    max_workers=5,
    thread_name_prefix="phase3-score",
) as pool:
    futures = {pool.submit(provider.score_job, job, profile): job
               for job in top_n}
    for fut in as_completed(futures):
        job = futures[fut]
        try:
            job["score_result"] = fut.result(timeout=180)
        except Exception as e:
            log(f"Score failed for {job['id']}: {e}")
            job["score_result"] = _fast_score_fallback(job, profile)
```

### Rationale for N=5

- Pi 4 with 8 GB RAM: 5 × ~250 MB = ~1.25 GB peak, plus FastAPI (~400 MB) + Ollama daemon (~2-4 GB) + OS. Stays under swap.
- Sonnet 4.6 Pro rate limits (~30K TPM / 50 RPM ballpark, tier-dependent). At 5 concurrent × ~4K tokens each, we touch ~20K tokens/sec — well under TPM with natural staggering.
- Bumpable via `CLAUDE_CLI_MAX_CONCURRENCY` env var.

### Thread-safety contract

`provider.score_job` is thread-safe because every call lands in `_run_cli()`, which is purely transactional (no shared state outside the semaphore). DemoProvider and OllamaProvider are also stateless — Phase 3 parallelization works for all three providers without provider-specific changes.

### Failure isolation

If 1 of 10 score_job calls fails, the other 9 keep going. The failed job falls back to `_fast_score` (deterministic, no LLM). Phase 3 never crashes from a CLI error. Matches today's serial-loop robustness.

## 9. Plan Gate Flip

### New helper (single source of truth)

```python
# app.py
def _can_use_claude(auth_user: dict) -> bool:
    if auth_user.get("is_developer"):
        return True
    return (auth_user.get("plan_tier") or "free").lower() == "pro"
```

### Four gate sites — same edit at each

```python
# Before
if mode == "anthropic" and not auth_user.get("is_developer"):
    # reject / coerce

# After
if mode == "anthropic" and not _can_use_claude(auth_user):
    # reject / coerce
```

Sites:
| Function | Location | Behavior |
|---|---|---|
| `_load_session_state` | `app.py:~1149` | Coerce `mode='anthropic'` → `'ollama'` for non-Pro non-dev |
| `update_config` | `app.py:~2663` | 402 `plan_required` (not 503 `coming_soon`) for non-Pro non-dev |
| `_run_phase_sse` | `app.py:~4216` | Reject with `code: "plan_required"` |
| `resume_tailor` | `app.py:~3929` | Reject with `code: "plan_required"` |

### Error code change

Free non-dev users setting `mode='anthropic'` get **402 `plan_required`** (matches the existing `*-cloud` Ollama gate). The legacy `503 coming_soon` disappears. Frontends already handle 402 → upgrade-prompt.

### Boot health check

On `@app.on_event("startup")`:

```python
async def _claude_cli_health_check():
    global _CLI_HEALTHY
    try:
        # _run_cli is blocking — run it off-thread so we don't stall the
        # event loop during startup.
        await asyncio.to_thread(_run_cli, "ping", timeout_s=20, budget_usd=0.01)
        _CLI_HEALTHY = True
        logger.info("[claude-cli] verified")
    except Exception as e:
        _CLI_HEALTHY = False
        logger.warning(f"[claude-cli] FAILED — Pro users fallback to Ollama: {e}")
```

The 5-minute APScheduler tick wraps its `_run_cli` call the same way.

A 5-min APScheduler tick re-runs the check so a keychain expiry surfaces within minutes.

When `_CLI_HEALTHY` is False, `_can_use_claude` short-circuits to False regardless of plan tier — Pro users transparently coerce to Ollama, with the warning visible on the Dev Ops live-log SSE.

## 10. Frontend Changes

- **SettingsPage**: remove the "Anthropic API key" input row entirely. The backend selector's "Claude (Anthropic)" radio option is enabled for Pro and Dev; disabled-with-tooltip ("Upgrade to Pro to use Claude") for Free. Mirrors the existing `*-cloud` Ollama model picker pattern.
- **TailorDrawer** (`app.jsx:7113` area): remove the "Soon" pill on the Claude option. Pro users can use it now.
- **PlansPage**: Pro card adds a bullet "Claude Sonnet 4.6 via Anthropic CLI — premium AI tailoring & advice". Remove the "Anthropic — coming soon" / "under development" disclaimers wherever they appear.

## 11. Tests

| File | Status | Coverage |
|---|---|---|
| `tests/conftest.py` | edit | New `claude_cli_bin` fixture: writes a fake `claude` shell script to `tmp_path` and prepends it to `PATH`. Reused by all CLI tests so no real subscription tokens are spent in CI. |
| `tests/unit/pipeline/test_claude_cli.py` | new | `_run_cli` success-text / success-JSON / schema-mismatch / timeout-124 / auth-failure / oversized-prompt-via-stdin / semaphore-bounds-concurrency / stream-json-deltas / generator-close-kills-subprocess. |
| `tests/unit/pipeline/test_anthropic_provider.py` | rewrite | Patches `pipeline.providers._run_cli`; verifies each method (`chat`, `extract_profile`, `score_job`, `tailor_resume`) builds the right prompt + schema and parses returned JSON. Adds a threading test: 5 concurrent `score_job` calls return without deadlock. |
| `tests/integration/test_plan_gates.py` | new | Free user → 402 on `POST /api/config {mode:'anthropic'}`; Pro user → 200; Dev → 200. Phase tailor SSE rejects free with `code:'plan_required'`. |
| `tests/integration/test_phase3_parallel.py` | new | Phase 3 with `_run_cli` monkey-patched to sleep 200ms. 10 jobs in top-N → wall time < 1s (parallelism proof). Inject one exception → other 9 succeed, failed one falls back to `_fast_score`. |
| `tests/unit/pipeline/test_anthropic_provider_sdk.py` | delete-if-present | Legacy SDK-shape assertions. |
| `tests/test_app_config.py` | edit | Drop `api_key` whitelist cases. |

## 12. Rollback / Kill Switch

Three tiers, ordered by speed of activation:

1. **Env-var kill switch** — `CLAUDE_CLI_DISABLED=1` forces `_CLI_HEALTHY = False`. Pro users transparently coerce to Ollama. `systemctl restart jobapp` activates in <10s. No code change.
2. **Per-call circuit breaker** — sliding window inside `_run_cli`: >5 consecutive errors in 60s → set `_CLI_HEALTHY = False` until the next successful health-tick (5 min). Auto-recovery, no human intervention.
3. **Git revert** — the change is split across two commits so revert is clean:
   - Commit A: provider rewrite + gate flip + tests. Keeps `anthropic` in `requirements.txt`.
   - Commit B: remove `anthropic` from `requirements.txt` after a soak window.
   Reverting A alone restores the SDK transport with all dependencies still importable.

## 13. Observability

Every CLI subprocess emits structured log lines visible via `/api/dev/logs/stream`:

```
[claude-cli] start pid=12345 method=score_job tokens_in=~2400
[claude-cli] done  pid=12345 elapsed=2.3s cost_usd=0.004
[claude-cli] error pid=12345 exit=1 stderr=Authentication failed
```

`cost_usd` comes from the CLI's `result` event. Useful for tracking subscription burn rate even though billing is flat — early warning if a hot path balloons.

## 13a. Assumptions to Verify in Implementation

These claims are taken from CLI `--help` output and Anthropic's public CLI
docs but should be empirically confirmed in the very first step of the
implementation plan (a 5-minute smoke test against the locally-installed
binary):

1. **`--json-schema` exit code on validation failure** — the spec assumes
   the CLI exits non-zero with a parseable stderr message when the model
   produces JSON that doesn't match the schema, so `_run_cli` can raise
   `ClaudeCLIError` cleanly. If the CLI instead returns exit 0 with
   malformed output, we need to add a post-call validator (use the same
   schema via `jsonschema` lib) inside `_run_cli`.
2. **stream-json event shape** — §7 parses
   `{"type":"stream_event","event":{"type":"content_block_delta","delta":{"text":"..."}}}`.
   Confirm against a live `--output-format stream-json --include-partial-messages`
   run before wiring the Atlas branch. The CLI's event envelope may use
   different key names than the SDK's native event types.
3. **`--effort` accepts arbitrary string values** — assumed to pass
   through to the model. Confirm `xhigh` and `max` are accepted on
   Sonnet 4.6 (these are documented as Opus-tier in some places; if
   Sonnet rejects them, fall back to `high`).
4. **OAuth keychain on Linux without an active desktop session** — the
   server is headless. Confirm `claude /login` persists into a backend
   that can be read by a non-interactive subprocess. If the keychain
   needs an active D-Bus session, the deploy step must include
   `gnome-keyring-daemon --unlock` or similar.

## 14. Non-Obvious Gotchas

Documented in `CLAUDE.md` §3 and `docs/CLAUDE_REFERENCE.md` §9 Bug History:

1. **`cwd` MUST NOT contain a `CLAUDE.md`** or the CLI auto-discovers it and prepends to system prompt. We use `/tmp/jobapp-claude/` + `--exclude-dynamic-system-prompt-sections` (belt + suspenders).
2. **NOT `--bare`** — bare mode strictly reads `ANTHROPIC_API_KEY` and bypasses the keychain, defeating the subscription auth entirely.
3. **Prompt length > 64 KB** routes via stdin instead of argv (argv length limit on Linux is ~128 KB but varies).
4. **stderr must not be interleaved with stdout** in streaming mode — would break the JSONL parser. Drain stderr on a separate thread.
5. **OAuth keychain must already be populated on the server** — run `claude /login` once during deploy. The boot health check catches it if not.

## 15. CLAUDE.md Updates

- §3 Providers — rewrite `AnthropicProvider` bullet: "Subprocess wrapper around the locally-installed `claude` CLI. Pinned to `claude-sonnet-4-6` via `--model sonnet`. OAuth keychain auth on server. No API key. Module-level `_CLI_SEMAPHORE` bounds total concurrent processes (default 5)."
- §2 plan-tier — add: "Helper `_can_use_claude(auth_user)` is the single source of truth for the four `mode='anthropic'` gate sites. Returns True for dev OR plan_tier='pro' AND `_CLI_HEALTHY`."
- §3 Pipeline — note `phase3_score_jobs` now uses a `ThreadPoolExecutor(max_workers=5)` for LLM scoring of top-N jobs.
- §7 Operational Mandates — add: "CLI provider runs in `/tmp/jobapp-claude/` (no CLAUDE.md). Do not change cwd without `--exclude-dynamic-system-prompt-sections`."

## 16. Acceptance Criteria

- Free user setting `mode='anthropic'`: 402 `plan_required`.
- Pro user setting `mode='anthropic'`: 200, Atlas chat streams, profile extraction returns valid JSON, Phase 3 scoring runs and completes faster than the serial baseline.
- Dev user: same as Pro (always permitted).
- Boot health check failure: Pro users transparently coerce to Ollama with a `journalctl` warning. No 500s anywhere.
- `requirements.txt` initially keeps `anthropic`; removed in a follow-up commit after one soak week.
- `pytest tests/` green across new + existing suites.
- `journalctl -u jobapp` shows `[claude-cli]` start/done/error log lines.
- Manual: Phase 3 with 10 jobs completes in ~3–5s wall time (vs. ~15s serial baseline).

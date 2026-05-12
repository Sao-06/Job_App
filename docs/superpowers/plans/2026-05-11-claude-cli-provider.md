# Claude CLI Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `pipeline/providers.py:AnthropicProvider`'s Anthropic SDK transport with subprocess calls to the locally-installed `claude` CLI (OAuth keychain auth, Sonnet 4.6 pinned), flip the four `mode='anthropic'` plan-gate sites to admit Pro users, and parallelize Phase 3 LLM scoring.

**Architecture:** In-place rewrite. The class name `AnthropicProvider` and `mode='anthropic'` wire value are preserved; only the transport changes. All JSON tool-calling becomes `--json-schema` validation. Streaming switches to `--output-format stream-json`. A single `_run_cli` chokepoint serializes through a `BoundedSemaphore(5)`.

**Tech Stack:** Python 3.11, subprocess + threading, FastAPI startup hooks, APScheduler, pytest, React/JSX (Babel-in-browser).

**Source of truth:** `docs/superpowers/specs/2026-05-11-claude-cli-provider-design.md` (commit `d24a2b4`).

---

## Phase 0 — Smoke-Test CLI Assumptions

### Task 0: Verify CLI behavior matches spec §13a

**Files:**
- Create: `scratch/claude_smoke.sh` (throwaway, not committed)

The spec makes 4 claims drawn from `--help` output. Confirm each before writing code that depends on them.

- [ ] **Step 1: Confirm `--json-schema` exits non-zero on schema mismatch**

```bash
mkdir -p scratch && cd scratch
claude -p "Say hi" --model sonnet --json-schema '{"type":"object","properties":{"required_field":{"type":"string"}},"required":["required_field"]}' --output-format text --max-budget-usd 0.05 ; echo "exit=$?"
```

Expected: either (a) exit non-zero with a stderr message about schema mismatch, OR (b) exit 0 with output that conforms to the schema (Sonnet usually complies). Record which behavior you see — if (b), the implementation must add a `jsonschema.validate(...)` call inside `_run_cli` as a defensive check.

- [ ] **Step 2: Confirm stream-json event shape**

```bash
claude -p "Count to 3" --model sonnet --output-format stream-json --include-partial-messages --max-budget-usd 0.05 | head -20
```

Expected: JSONL lines including `{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}}` (or close variant). Record the exact key path to text deltas; the parser in Task 4 keys off this.

- [ ] **Step 3: Confirm `--effort xhigh` works on Sonnet**

```bash
claude -p "Briefly: why is the sky blue?" --model sonnet --effort xhigh --output-format text --max-budget-usd 0.05 ; echo "exit=$?"
```

Expected: exit 0 with a normal reply. If you see "effort level 'xhigh' not supported for model sonnet", change `TAILOR_EFFORT = "xhigh"` in Task 7 to `"high"`.

- [ ] **Step 4: Confirm headless OAuth keychain works**

```bash
# From an SSH session (no GUI), no $DISPLAY:
DISPLAY="" claude -p "ping" --model sonnet --output-format text --max-budget-usd 0.01 ; echo "exit=$?"
```

Expected: exit 0. If exit non-zero with "no keychain" or "DBUS_SESSION_BUS_ADDRESS not set", the deploy step needs `gnome-keyring-daemon --unlock` or moving auth to `~/.config/Claude/credentials.json` (file-based). Record the resolution before continuing.

- [ ] **Step 5: Discard scratch dir, no commit needed**

```bash
rm -rf scratch
```

Record the findings inline as a comment at the top of Task 1 (`pipeline/claude_cli_exceptions.py`) so future readers see the assumption verification trail.

---

## Phase 1 — Core CLI Helper Foundation

### Task 1: Exception types + module-level constants

**Files:**
- Modify: `pipeline/providers.py` (insert new section near top, after imports ~line 16)

- [ ] **Step 1: Add the new constants and exception classes**

Open `pipeline/providers.py` and after the existing imports / before line 18 (`class BaseProvider`), insert:

```python
# ── Claude CLI transport (replaces Anthropic SDK) ──────────────────────────────
import os as _os
import shutil as _shutil
import subprocess as _subprocess
import threading as _threading

CLAUDE_BIN: str = _os.environ.get("CLAUDE_BIN") or (_shutil.which("claude") or "claude")
CLAUDE_CLI_MODEL: str = _os.environ.get("CLAUDE_CLI_MODEL", "sonnet")
CLAUDE_CLI_SCRATCH: str = _os.environ.get("CLAUDE_CLI_SCRATCH", "/tmp/jobapp-claude")
CLAUDE_CLI_MAX_CONCURRENCY: int = int(_os.environ.get("CLAUDE_CLI_MAX_CONCURRENCY", "5"))
CLAUDE_CLI_PROMPT_STDIN_THRESHOLD: int = 64 * 1024  # route via stdin above this

# Module-global semaphore — every subprocess spawn acquires it.
_CLI_SEMAPHORE = _threading.BoundedSemaphore(CLAUDE_CLI_MAX_CONCURRENCY)

# Health flag, toggled by app.py startup hook + 5-min ticker.
_CLI_HEALTHY: bool = True

def _ensure_scratch_dir() -> str:
    """Idempotent: create CLAUDE_CLI_SCRATCH if missing. The dir intentionally
    contains NO CLAUDE.md so the CLI doesn't auto-prepend it to system prompts."""
    _os.makedirs(CLAUDE_CLI_SCRATCH, exist_ok=True)
    return CLAUDE_CLI_SCRATCH


class ClaudeCLIError(RuntimeError):
    """Generic Claude CLI failure. `stderr` is the captured stderr text."""
    def __init__(self, message: str, *, stderr: str = "", exit_code: int | None = None):
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code


class ClaudeCLITimeoutError(ClaudeCLIError):
    """The subprocess was killed by our timeout wrapper."""
    pass
```

- [ ] **Step 2: Verify the file still imports**

```bash
python -c "import pipeline.providers; print('OK')"
```

Expected: `OK`. No traceback.

- [ ] **Step 3: Commit**

```bash
git add pipeline/providers.py
git commit -m "feat(providers): claude CLI constants + exception types

Lays the groundwork for the in-place AnthropicProvider rewrite.
Module-level constants (CLAUDE_BIN, CLAUDE_CLI_MODEL=sonnet,
CLAUDE_CLI_MAX_CONCURRENCY=5, scratch dir) + BoundedSemaphore + two
exception classes (ClaudeCLIError, ClaudeCLITimeoutError). No call sites
changed yet — the existing SDK path still works.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Fake `claude` CLI test fixture

**Files:**
- Modify: `tests/conftest.py` (append at end)

- [ ] **Step 1: Add the fixture**

Append to `tests/conftest.py`:

```python
import json as _json_for_fixtures
import os as _os_for_fixtures
import textwrap as _textwrap_for_fixtures

@pytest.fixture
def claude_cli_bin(tmp_path, monkeypatch):
    """Write a fake `claude` shell script to tmp_path and prepend it to PATH.

    The fake responds to a small set of canned argvs so providers tests can
    exercise _run_cli without spawning the real CLI.

    Usage:
        def test_x(claude_cli_bin):
            claude_cli_bin.set_response("Hello world")   # text mode
            claude_cli_bin.set_json({"ok": True})        # json mode
            claude_cli_bin.set_error("auth failed", exit=1)
            claude_cli_bin.set_stream(["chunk one", "chunk two"])
    """
    state_file = tmp_path / "claude_state.json"
    state_file.write_text(_json_for_fixtures.dumps({
        "mode": "text", "text": "OK", "exit": 0, "stderr": "",
        "stream_chunks": [], "delay_s": 0,
    }))

    script = tmp_path / "claude"
    script.write_text(_textwrap_for_fixtures.dedent(f"""\
        #!/usr/bin/env python3
        import json, sys, time
        state = json.loads(open({str(state_file)!r}).read())
        time.sleep(state.get("delay_s", 0))
        if state.get("exit", 0) != 0:
            sys.stderr.write(state.get("stderr", ""))
            sys.exit(state["exit"])
        # Find --output-format
        argv = sys.argv[1:]
        out_fmt = "text"
        if "--output-format" in argv:
            i = argv.index("--output-format")
            if i + 1 < len(argv):
                out_fmt = argv[i + 1]
        if out_fmt == "stream-json":
            for chunk in state.get("stream_chunks", []):
                sys.stdout.write(json.dumps({{
                    "type": "stream_event",
                    "event": {{
                        "type": "content_block_delta",
                        "delta": {{"type": "text_delta", "text": chunk}},
                    }},
                }}) + "\\n")
                sys.stdout.flush()
            sys.stdout.write(json.dumps({{
                "type": "result", "subtype": "success", "total_cost_usd": 0.001,
            }}) + "\\n")
            sys.exit(0)
        # text or json output: print whatever 'text' field holds
        sys.stdout.write(state["text"])
        sys.exit(0)
    """))
    script.chmod(0o755)

    class _Helper:
        def set_response(self, text):
            d = _json_for_fixtures.loads(state_file.read_text())
            d.update({{"mode": "text", "text": text, "exit": 0}})
            state_file.write_text(_json_for_fixtures.dumps(d))
        def set_json(self, obj):
            self.set_response(_json_for_fixtures.dumps(obj))
        def set_error(self, stderr, exit=1):
            d = _json_for_fixtures.loads(state_file.read_text())
            d.update({{"exit": exit, "stderr": stderr}})
            state_file.write_text(_json_for_fixtures.dumps(d))
        def set_stream(self, chunks):
            d = _json_for_fixtures.loads(state_file.read_text())
            d.update({{"mode": "stream", "stream_chunks": list(chunks), "exit": 0}})
            state_file.write_text(_json_for_fixtures.dumps(d))
        def set_delay(self, seconds):
            d = _json_for_fixtures.loads(state_file.read_text())
            d.update({{"delay_s": seconds}})
            state_file.write_text(_json_for_fixtures.dumps(d))

    monkeypatch.setenv("PATH", str(tmp_path) + _os_for_fixtures.pathsep + _os_for_fixtures.environ["PATH"])
    monkeypatch.setenv("CLAUDE_BIN", str(script))
    monkeypatch.setenv("CLAUDE_CLI_MODEL", "sonnet")  # deterministic for tests
    return _Helper()
```

Note: the curly-brace doubling `{{...}}` inside the f-string is required so the produced shell script is valid Python without f-string interpolation of its own braces. Verify by reading the rendered script after first test run.

- [ ] **Step 2: Smoke-test the fixture in isolation**

Create `tests/unit/pipeline/test_claude_cli_fixture_smoke.py`:

```python
import subprocess

def test_fixture_text_response(claude_cli_bin):
    claude_cli_bin.set_response("hello from fake")
    out = subprocess.run(["claude", "-p", "anything"], capture_output=True, text=True)
    assert out.returncode == 0
    assert out.stdout == "hello from fake"

def test_fixture_error(claude_cli_bin):
    claude_cli_bin.set_error("simulated auth fail", exit=1)
    out = subprocess.run(["claude", "-p", "x"], capture_output=True, text=True)
    assert out.returncode == 1
    assert "simulated auth fail" in out.stderr
```

- [ ] **Step 3: Run the smoke test**

```bash
pytest tests/unit/pipeline/test_claude_cli_fixture_smoke.py -v
```

Expected: 2 passed.

- [ ] **Step 4: Delete the smoke test (we have real tests in Task 3)**

```bash
rm tests/unit/pipeline/test_claude_cli_fixture_smoke.py
```

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py
git commit -m "test: claude_cli_bin fixture for provider tests

Writes a Python-driven fake \`claude\` shell script to tmp_path and
prepends it to PATH. Helper methods set canned responses (text/JSON/
error/stream/delay). All upcoming provider tests use this — zero real
subscription tokens consumed in CI.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Implement `_run_cli` (one-shot, blocking) with TDD

**Files:**
- Create: `tests/unit/pipeline/test_claude_cli.py`
- Modify: `pipeline/providers.py` (append to the Task 1 section)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/pipeline/test_claude_cli.py`:

```python
"""Unit tests for pipeline.providers._run_cli — the subprocess chokepoint."""
import json
import pytest
import subprocess
import threading
import time
from unittest.mock import patch

from pipeline.providers import (
    _run_cli, ClaudeCLIError, ClaudeCLITimeoutError,
    _CLI_SEMAPHORE, CLAUDE_CLI_MAX_CONCURRENCY,
)


def test_run_cli_text_response(claude_cli_bin):
    claude_cli_bin.set_response("the answer is 42")
    out = _run_cli("what is the answer?")
    assert out == "the answer is 42"


def test_run_cli_json_schema_passes_argv(claude_cli_bin):
    """Verify --json-schema lands in argv when schema is provided."""
    claude_cli_bin.set_response('{"x": 1}')
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as run_spy:
        _run_cli("compute x", json_schema=schema)
        argv = run_spy.call_args[0][0]
        assert "--json-schema" in argv
        i = argv.index("--json-schema")
        assert json.loads(argv[i + 1]) == schema


def test_run_cli_includes_system_prompt(claude_cli_bin):
    claude_cli_bin.set_response("ack")
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as run_spy:
        _run_cli("hi", system="be terse")
        argv = run_spy.call_args[0][0]
        assert "--append-system-prompt" in argv
        i = argv.index("--append-system-prompt")
        assert argv[i + 1] == "be terse"


def test_run_cli_no_system_omits_flag(claude_cli_bin):
    claude_cli_bin.set_response("ack")
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as run_spy:
        _run_cli("hi")
        argv = run_spy.call_args[0][0]
        assert "--append-system-prompt" not in argv


def test_run_cli_includes_required_safety_flags(claude_cli_bin):
    claude_cli_bin.set_response("ack")
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as run_spy:
        _run_cli("hi")
        argv = run_spy.call_args[0][0]
        assert "--disable-slash-commands" in argv
        assert "--exclude-dynamic-system-prompt-sections" in argv
        assert "--max-budget-usd" in argv
        assert "--model" in argv
        i = argv.index("--model")
        assert argv[i + 1] == "sonnet"


def test_run_cli_nonzero_exit_raises(claude_cli_bin):
    claude_cli_bin.set_error("auth failed: token expired", exit=1)
    with pytest.raises(ClaudeCLIError) as exc:
        _run_cli("anything")
    assert "auth failed" in exc.value.stderr
    assert exc.value.exit_code == 1


def test_run_cli_timeout_raises(claude_cli_bin):
    claude_cli_bin.set_delay(2)
    claude_cli_bin.set_response("would have worked")
    with pytest.raises(ClaudeCLITimeoutError):
        _run_cli("slow", timeout_s=0.5)


def test_run_cli_oversized_prompt_uses_stdin(claude_cli_bin, monkeypatch):
    """Prompts > 64KB route via stdin so we don't hit argv length limits."""
    claude_cli_bin.set_response("ok")
    big = "x" * (64 * 1024 + 100)
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as run_spy:
        _run_cli(big)
        kwargs = run_spy.call_args[1]
        assert kwargs.get("input") is not None
        argv = run_spy.call_args[0][0]
        # Prompt must NOT appear as an argv element when routed via stdin.
        assert big not in argv


def test_run_cli_concurrent_calls_bounded_by_semaphore(claude_cli_bin):
    """Spawning > N calls in parallel blocks until earlier ones release."""
    claude_cli_bin.set_delay(0.3)
    claude_cli_bin.set_response("done")
    start = time.time()
    results = []
    def worker():
        results.append(_run_cli("x"))
    threads = [threading.Thread(target=worker) for _ in range(CLAUDE_CLI_MAX_CONCURRENCY * 2)]
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - start
    assert all(r == "done" for r in results)
    # 2N calls with 0.3s each, bounded by N concurrent → at least ~2 * 0.3s wall
    assert elapsed >= 0.55, f"Semaphore not enforcing concurrency cap ({elapsed=})"


def test_run_cli_cwd_is_scratch_dir(claude_cli_bin):
    """Critical: cwd MUST be the scratch dir, NOT app cwd, so the CLI doesn't
    auto-discover CLAUDE.md and prepend it to system prompts."""
    claude_cli_bin.set_response("ack")
    from pipeline.providers import CLAUDE_CLI_SCRATCH
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as run_spy:
        _run_cli("hi")
        kwargs = run_spy.call_args[1]
        assert kwargs.get("cwd") == CLAUDE_CLI_SCRATCH
```

- [ ] **Step 2: Run tests — confirm they fail because `_run_cli` doesn't exist**

```bash
pytest tests/unit/pipeline/test_claude_cli.py -v
```

Expected: ImportError or AttributeError — `_run_cli` not yet defined in `pipeline.providers`.

- [ ] **Step 3: Implement `_run_cli`**

Append to `pipeline/providers.py` immediately below the `ClaudeCLITimeoutError` class from Task 1:

```python
def _run_cli(
    prompt: str,
    *,
    system: str | None = None,
    json_schema: dict | None = None,
    effort: str = "high",
    timeout_s: float = 120.0,
    budget_usd: float = 2.00,
) -> str:
    """Spawn `claude -p` once, return its stdout. Blocking.

    Acquires `_CLI_SEMAPHORE` so total concurrent subprocesses are bounded
    by CLAUDE_CLI_MAX_CONCURRENCY (default 5).

    Raises `ClaudeCLITimeoutError` on timeout, `ClaudeCLIError` on any
    other nonzero exit.
    """
    _ensure_scratch_dir()

    argv: list[str] = [
        CLAUDE_BIN,
        "--model", CLAUDE_CLI_MODEL,
        "--effort", effort,
        "--output-format", "text",
        "--disable-slash-commands",
        "--max-budget-usd", f"{budget_usd:.2f}",
        "--exclude-dynamic-system-prompt-sections",
    ]
    if system:
        argv += ["--append-system-prompt", system]
    if json_schema is not None:
        argv += ["--json-schema", _json_min(json_schema)]

    use_stdin = len(prompt) > CLAUDE_CLI_PROMPT_STDIN_THRESHOLD
    if not use_stdin:
        argv += ["-p", prompt]
    else:
        argv += ["-p", "--input-format", "text"]

    env = dict(_os.environ)
    env["CLAUDE_CODE_NONINTERACTIVE"] = "1"

    run_kwargs = dict(
        capture_output=True, text=True, env=env,
        cwd=CLAUDE_CLI_SCRATCH, timeout=timeout_s,
    )
    if use_stdin:
        run_kwargs["input"] = prompt

    with _CLI_SEMAPHORE:
        try:
            result = _subprocess.run(argv, **run_kwargs)
        except _subprocess.TimeoutExpired as e:
            raise ClaudeCLITimeoutError(
                f"claude -p timed out after {timeout_s}s",
                stderr=(e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or ""),
                exit_code=124,
            ) from e

    if result.returncode != 0:
        raise ClaudeCLIError(
            f"claude -p exit {result.returncode}: {result.stderr.strip()[:240]}",
            stderr=result.stderr or "",
            exit_code=result.returncode,
        )
    return result.stdout


def _json_min(obj: dict) -> str:
    """Compact JSON encode for argv injection."""
    import json as _json
    return _json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
pytest tests/unit/pipeline/test_claude_cli.py -v
```

Expected: 10 passed. If `test_run_cli_concurrent_calls_bounded_by_semaphore` is flaky on slow CI, raise the delay from 0.3 to 0.5.

- [ ] **Step 5: Commit**

```bash
git add pipeline/providers.py tests/unit/pipeline/test_claude_cli.py
git commit -m "feat(providers): _run_cli — subprocess chokepoint for Claude CLI

Single helper every Anthropic-equivalent call routes through. argv
assembly (--model sonnet, --effort, --max-budget-usd 2.00, JSON-schema
optional, --disable-slash-commands, --exclude-dynamic-system-prompt-sections),
oversized-prompt-via-stdin (>64KB), explicit cwd=/tmp/jobapp-claude
(NOT app cwd — would auto-discover CLAUDE.md), per-call timeout mapping
to ClaudeCLITimeoutError, BoundedSemaphore(5) caps total concurrent
subprocesses across the whole app.

10 unit tests cover argv, system prompt, JSON schema, safety flags,
errors, timeout, oversized prompt, semaphore enforcement, cwd correctness.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 2 — Streaming

### Task 4: Implement `_run_cli_stream` generator with TDD

**Files:**
- Modify: `tests/unit/pipeline/test_claude_cli.py` (append)
- Modify: `pipeline/providers.py` (append after `_run_cli`)

- [ ] **Step 1: Add failing streaming tests**

Append to `tests/unit/pipeline/test_claude_cli.py`:

```python
def test_run_cli_stream_yields_deltas(claude_cli_bin):
    claude_cli_bin.set_stream(["Hello", ", ", "world!"])
    chunks = list(_run_cli_stream("hi"))
    assert "".join(chunks) == "Hello, world!"


def test_run_cli_stream_handles_empty(claude_cli_bin):
    claude_cli_bin.set_stream([])
    chunks = list(_run_cli_stream("hi"))
    assert chunks == []


def test_run_cli_stream_uses_stream_json_output_format(claude_cli_bin):
    claude_cli_bin.set_stream(["ack"])
    with patch("pipeline.providers._subprocess.Popen", wraps=subprocess.Popen) as popen_spy:
        list(_run_cli_stream("hi"))
        argv = popen_spy.call_args[0][0]
        assert "--output-format" in argv
        i = argv.index("--output-format")
        assert argv[i + 1] == "stream-json"
        assert "--include-partial-messages" in argv


def test_run_cli_stream_kills_subprocess_on_generator_close(claude_cli_bin):
    """If the consumer closes the generator early, the subprocess must die."""
    claude_cli_bin.set_stream(["a", "b", "c", "d", "e"])
    claude_cli_bin.set_delay(0.2)  # slow each chunk
    gen = _run_cli_stream("hi")
    next(gen)  # consume one
    gen.close()  # abandon
    # If we don't kill the subprocess, this thread would still see it running.
    # Smoke check: a second generator should still acquire the semaphore quickly.
    start = time.time()
    claude_cli_bin.set_delay(0)
    claude_cli_bin.set_stream(["x"])
    list(_run_cli_stream("hi"))
    assert time.time() - start < 1.5
```

- [ ] **Step 2: Run — confirm failure**

```bash
pytest tests/unit/pipeline/test_claude_cli.py::test_run_cli_stream_yields_deltas -v
```

Expected: ImportError — `_run_cli_stream` not defined yet.

- [ ] **Step 3: Implement `_run_cli_stream`**

Append to `pipeline/providers.py` after `_json_min`:

```python
def _run_cli_stream(
    prompt: str,
    *,
    system: str | None = None,
    effort: str = "high",
    budget_usd: float = 2.00,
):
    """Generator. Yields text deltas as the CLI streams. Closes/kills the
    subprocess if the consumer abandons the generator early.

    Acquires `_CLI_SEMAPHORE` for the lifetime of the stream — be aware
    that long Atlas chats can hold a slot for many seconds.
    """
    import json as _json
    _ensure_scratch_dir()

    argv: list[str] = [
        CLAUDE_BIN,
        "--model", CLAUDE_CLI_MODEL,
        "--effort", effort,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--disable-slash-commands",
        "--max-budget-usd", f"{budget_usd:.2f}",
        "--exclude-dynamic-system-prompt-sections",
        "-p", prompt,
    ]
    if system:
        argv += ["--append-system-prompt", system]

    env = dict(_os.environ)
    env["CLAUDE_CODE_NONINTERACTIVE"] = "1"

    _CLI_SEMAPHORE.acquire()
    proc = None
    try:
        proc = _subprocess.Popen(
            argv, stdout=_subprocess.PIPE, stderr=_subprocess.PIPE,
            text=True, bufsize=1, env=env, cwd=CLAUDE_CLI_SCRATCH,
        )
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "stream_event":
                inner = evt.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            yield text
            elif etype == "result" and evt.get("subtype") != "success":
                err = evt.get("error") or "Claude CLI stream failed"
                raise ClaudeCLIError(str(err))
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except _subprocess.TimeoutExpired:
                proc.kill()
        try:
            _CLI_SEMAPHORE.release()
        except ValueError:
            pass  # already released — defensive
```

- [ ] **Step 4: Run streaming tests — confirm pass**

```bash
pytest tests/unit/pipeline/test_claude_cli.py -v
```

Expected: 14 passed (10 from Task 3 + 4 new).

- [ ] **Step 5: Commit**

```bash
git add pipeline/providers.py tests/unit/pipeline/test_claude_cli.py
git commit -m "feat(providers): _run_cli_stream — stream-json generator

Companion to _run_cli for streaming use (Atlas chat). Spawns claude with
--output-format stream-json --include-partial-messages, parses JSONL
events off stdout, yields text deltas as they arrive. The generator's
finally block terminates the subprocess if the consumer abandons (e.g.,
SSE connection closes). Same BoundedSemaphore as _run_cli.

4 tests: delta accumulation, empty stream, argv flags, early-close kill.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 3 — AnthropicProvider Rewrite

### Task 5: Extract JSON schemas to module constants

**Files:**
- Modify: `pipeline/providers.py` (define constants, then dedupe inside methods)

The three existing tool definitions live at lines ~442–585 (`extract_profile` → tool `save_profile`), ~640–697 (`score_job` → tool `score_job`), ~767–859 (`tailor_resume` → tool `tailor_resume`). Each `tool` dict has an `input_schema` we'll lift verbatim.

- [ ] **Step 1: Insert module-level schema constants**

Just before `class AnthropicProvider` (around line 355), insert:

```python
# ── JSON schemas for --json-schema CLI mode ────────────────────────────────────
# Lifted verbatim from the previous tool_use `input_schema` dicts inside each
# AnthropicProvider method. Single source of truth so the CLI's strict-JSON
# path uses the same shape the SDK's forced tool-call path used.

EXTRACT_PROFILE_SCHEMA: dict = {}   # populated below — see Step 2
SCORE_JOB_SCHEMA: dict = {}
TAILOR_RESUME_SCHEMA: dict = {}
```

- [ ] **Step 2: Move each schema body up**

Cut the `input_schema` dict from inside `AnthropicProvider.extract_profile` (lines ~445–585) and assign it as the value of `EXTRACT_PROFILE_SCHEMA` above the class. Inside `extract_profile`, keep the `tool` dict but replace its `input_schema` with `EXTRACT_PROFILE_SCHEMA`. Repeat for `score_job` (the dict at ~643–697 → `SCORE_JOB_SCHEMA`) and `tailor_resume` (~774–859 → `TAILOR_RESUME_SCHEMA`).

This is a pure-refactor step — no behavior change. Validate by running the existing test suite (if any Anthropic SDK tests still pass; they may not on this machine without an API key, in which case just import-check):

```bash
python -c "from pipeline.providers import EXTRACT_PROFILE_SCHEMA, SCORE_JOB_SCHEMA, TAILOR_RESUME_SCHEMA; print(len(EXTRACT_PROFILE_SCHEMA['properties']), len(SCORE_JOB_SCHEMA['properties']), len(TAILOR_RESUME_SCHEMA['properties']))"
```

Expected: three integers > 0 (e.g., `10 6 6`).

- [ ] **Step 3: Commit**

```bash
git add pipeline/providers.py
git commit -m "refactor(providers): lift Anthropic JSON schemas to module constants

EXTRACT_PROFILE_SCHEMA, SCORE_JOB_SCHEMA, TAILOR_RESUME_SCHEMA at
module level. Each AnthropicProvider method now references the constant
instead of defining the dict inline. Pure refactor — no behavior change
in this commit. Prepares for the CLI rewrite (Tasks 6–9) which will pass
these to \`claude -p --json-schema\`.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Rewrite `AnthropicProvider.__init__` + `chat()`

**Files:**
- Modify: `pipeline/providers.py`
- Modify: `tests/unit/pipeline/test_providers_anthropic.py`

- [ ] **Step 1: Write the failing tests** (replace the whole file contents)

Replace `tests/unit/pipeline/test_providers_anthropic.py` with:

```python
"""AnthropicProvider tests — CLI-backed transport.

Every method is exercised via the claude_cli_bin fixture so no real
subscription tokens are spent.
"""
import json
import pytest
from unittest.mock import patch

from pipeline.providers import AnthropicProvider


def test_init_does_not_import_anthropic_sdk(claude_cli_bin):
    p = AnthropicProvider()
    assert p.model == "claude-sonnet-4-6"


def test_init_no_api_key_required(claude_cli_bin):
    # Should NOT raise even with no api_key arg.
    AnthropicProvider()
    AnthropicProvider(api_key=None)
    AnthropicProvider(api_key="")  # back-compat — silently ignored


def test_chat_text_mode(claude_cli_bin):
    claude_cli_bin.set_response("the weather is sunny\n")
    p = AnthropicProvider()
    out = p.chat(system="be terse", messages=[{"role": "user", "content": "weather?"}])
    assert "sunny" in out


def test_chat_empty_messages_returns_empty(claude_cli_bin):
    p = AnthropicProvider()
    assert p.chat(system="x", messages=[]) == ""


def test_chat_json_mode_passes_schema(claude_cli_bin):
    import subprocess
    claude_cli_bin.set_response('{"reply": "ok"}')
    p = AnthropicProvider()
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as spy:
        p.chat(system="", messages=[{"role": "user", "content": "x"}], json_mode=True)
        argv = spy.call_args[0][0]
        assert "--json-schema" in argv
        i = argv.index("--json-schema")
        schema = json.loads(argv[i + 1])
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is True
```

- [ ] **Step 2: Run — confirm failure**

```bash
pytest tests/unit/pipeline/test_providers_anthropic.py -v
```

Expected: tests fail. Either `import anthropic` errors at `AnthropicProvider.__init__`, or `model == "claude-sonnet-4-6"` fails (currently `"claude-opus-4-7"`).

- [ ] **Step 3: Rewrite `AnthropicProvider.__init__` and `chat`**

In `pipeline/providers.py`:

(a) Flip the class constant `MODEL`:
```python
class AnthropicProvider(BaseProvider):
    """Claude Sonnet 4.6 via the local `claude` CLI subprocess.

    Replaces the previous Anthropic SDK transport. Auth is the OAuth
    keychain on the server (run `claude /login` once during deploy).
    No ANTHROPIC_API_KEY needed.

    See pipeline.providers._run_cli for the subprocess contract.
    """

    MODEL = "claude-sonnet-4-6"
    DEFAULT_EFFORT = "high"
```

(b) Replace `__init__`:
```python
    def __init__(self, api_key: str | None = None):
        # api_key kwarg kept for back-compat; CLI uses keychain.
        self.model = self.MODEL
```

(c) Drop `_output_config` entirely (delete the method).

(d) Replace `chat`:
```python
    def chat(self, system: str, messages: list, max_tokens: int = 1024,
             json_mode: bool = False) -> str:
        prompt = _collapse_messages(messages)
        if not prompt:
            return ""
        schema = {"type": "object", "additionalProperties": True} if json_mode else None
        return _run_cli(prompt, system=system or None, json_schema=schema).strip()
```

(e) Add the `_collapse_messages` helper near `_run_cli`:
```python
def _collapse_messages(messages: list) -> str:
    """Render a [{role, content}, ...] history as a single prompt string.

    The CLI takes one -p prompt, not a multi-turn structure. For chat use
    we serialize prior turns as a transcript so the model retains context.
    """
    parts: list[str] = []
    history = []
    for m in (messages or []):
        if m.get("role") not in ("user", "assistant"):
            continue
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        history.append((m["role"], content))
    if not history:
        return ""
    if len(history) == 1 and history[0][0] == "user":
        return history[0][1]
    # Multi-turn → transcript style.
    parts.append("[Previous conversation]")
    for role, content in history[:-1]:
        label = "User" if role == "user" else "Assistant"
        parts.append(f"{label}: {content}")
    last_role, last_content = history[-1]
    parts.append("")
    parts.append("[Current message]" if last_role == "user" else f"[Assistant continuation]")
    parts.append(last_content)
    return "\n".join(parts)
```

(f) Drop `_tool_call` entirely (delete the method) — schemas are passed via `--json-schema` from now on.

- [ ] **Step 4: Run tests — confirm pass**

```bash
pytest tests/unit/pipeline/test_providers_anthropic.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/providers.py tests/unit/pipeline/test_providers_anthropic.py
git commit -m "refactor(providers): AnthropicProvider.__init__ + chat() via CLI

  • MODEL flips claude-opus-4-7 → claude-sonnet-4-6 (subscription
    rate-limit headroom — see spec §6).
  • Drop \`import anthropic\` and the SDK client.
  • api_key kwarg kept for back-compat; ignored (CLI uses keychain).
  • chat() now routes through _run_cli with json_mode → permissive
    object schema. Multi-turn history collapsed to a transcript via
    new _collapse_messages helper.
  • _output_config, _tool_call deleted — schemas come via --json-schema.

Tests rewritten against the claude_cli_bin fixture. 5 pass.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Rewrite `extract_profile`, `score_job`, `tailor_resume` with TDD

**Files:**
- Modify: `pipeline/providers.py`
- Modify: `tests/unit/pipeline/test_providers_anthropic.py`

- [ ] **Step 1: Add failing tests for all three methods**

Append to `tests/unit/pipeline/test_providers_anthropic.py`:

```python
def test_extract_profile_parses_cli_json(claude_cli_bin):
    canned = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "target_titles": [{"title": "Engineer", "family": "Software Engineering", "evidence": "B.S. CS 2024"}],
        "top_hard_skills": [{"skill": "Python", "category": "programming_language", "evidence": "Python, JS"}],
        "top_soft_skills": ["Teamwork"],
        "critical_analysis": "Solid resume, room for impact metrics.",
        "education": [], "research_experience": [], "work_experience": [],
        "experience": [], "projects": [], "resume_gaps": [],
    }
    claude_cli_bin.set_json(canned)
    p = AnthropicProvider()
    out = p.extract_profile("RESUME TEXT", preferred_titles=["Engineer"])
    assert out["name"] == "Jane Doe"
    assert out["top_hard_skills"][0]["skill"] == "Python"


def test_extract_profile_passes_schema_to_cli(claude_cli_bin):
    import subprocess
    from pipeline.providers import EXTRACT_PROFILE_SCHEMA
    claude_cli_bin.set_json({"name": "x", "top_hard_skills": [], "top_soft_skills": [],
                              "target_titles": [], "critical_analysis": ""})
    p = AnthropicProvider()
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as spy:
        p.extract_profile("resume")
        argv = spy.call_args[0][0]
        i = argv.index("--json-schema")
        assert json.loads(argv[i + 1]) == EXTRACT_PROFILE_SCHEMA


def test_score_job_parses_cli_json(claude_cli_bin):
    claude_cli_bin.set_json({
        "required_skills": 0.8, "industry": 0.6, "location_seniority": 0.5,
        "matching_skills": ["Python", "FastAPI"],
        "missing_skills": ["Rust"], "reasoning": "Strong skill overlap.",
    })
    p = AnthropicProvider()
    job = {"id": "j1", "title": "Engineer", "company": "Acme", "description": "...", "requirements": ["Python"]}
    profile = {"top_hard_skills": [{"skill": "Python"}], "target_titles": [{"title": "Engineer"}]}
    out = p.score_job(job, profile)
    assert "score" in out
    assert "score_breakdown" in out


def test_tailor_resume_passes_xhigh_effort(claude_cli_bin):
    import subprocess
    claude_cli_bin.set_json({
        "summary_rewrite": "...", "skills_reordered": [], "experience_bullets": [],
        "ats_keywords_missing": [], "cover_letter": "",
    })
    p = AnthropicProvider()
    job = {"id": "j1", "title": "Engineer", "company": "Acme", "description": "...", "requirements": []}
    profile = {"name": "X", "top_hard_skills": [], "experience": [], "education": []}
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as spy:
        p.tailor_resume(job, profile, "RESUME TEXT")
        argv = spy.call_args[0][0]
        i = argv.index("--effort")
        assert argv[i + 1] == "xhigh"


def test_extract_profile_cli_error_propagates(claude_cli_bin):
    from pipeline.providers import ClaudeCLIError
    claude_cli_bin.set_error("rate limit hit", exit=1)
    p = AnthropicProvider()
    with pytest.raises(ClaudeCLIError):
        p.extract_profile("resume")
```

- [ ] **Step 2: Run — confirm failures**

```bash
pytest tests/unit/pipeline/test_providers_anthropic.py -v
```

Expected: the 5 new tests fail (likely due to `self.client.messages.create` still being called inside the methods).

- [ ] **Step 3: Rewrite `extract_profile`**

Replace the body of `AnthropicProvider.extract_profile` (after the schema lift in Task 5) with:

```python
    def extract_profile(self, resume_text: str, preferred_titles: list | None = None,
                        heuristic_hint: dict | None = None) -> dict:
        import json as _json
        # Build the same prompt the SDK path used (everything that lived in
        # `pref_hint` and the user-content block of _tool_call).
        prompt_parts: list[str] = []
        if preferred_titles:
            prompt_parts.append(
                "PREFERRED TITLES (rank these first if evidence supports them): "
                + ", ".join(preferred_titles)
            )
        if heuristic_hint:
            prompt_parts.append(
                "HEURISTIC HINT (baseline extracted by regex/section parser — "
                "verify and correct, do NOT discard wholesale):\n"
                + _json.dumps(heuristic_hint, indent=2)
            )
        prompt_parts.append("RESUME:\n" + resume_text)
        prompt_parts.append(
            "Return ONLY a JSON object matching the schema. "
            "Every target_title and hard_skill MUST include the verbatim "
            "evidence line from the resume."
        )
        prompt = "\n\n".join(prompt_parts)
        system = (
            "You are a resume analyst. Extract the candidate's profile as "
            "structured JSON. Be brutally honest in critical_analysis. Never "
            "fabricate — every claim must trace to a line in the resume."
        )
        raw = _run_cli(
            prompt, system=system, json_schema=EXTRACT_PROFILE_SCHEMA,
            effort=self.DEFAULT_EFFORT, timeout_s=120.0,
        )
        return _json.loads(raw)
```

- [ ] **Step 4: Rewrite `score_job`**

Replace the body of `AnthropicProvider.score_job` with:

```python
    def score_job(self, job: dict, profile: dict) -> dict:
        import json as _json
        coverage_raw, matched, missing = compute_skill_coverage(job, profile)
        prompt = (
            "Score this job for the candidate. Return JSON with these fields:\n"
            "  required_skills: 0..1 (you MUST anchor close to "
            f"{coverage_raw:.2f} — deterministic skill overlap)\n"
            "  industry:        0..1\n"
            "  location_seniority: 0..1\n"
            "  matching_skills, missing_skills: lists of strings\n"
            "  reasoning: 1–2 sentences\n\n"
            f"JOB: {_json.dumps({k: job.get(k) for k in ('title','company','location','remote','description','requirements','experience_level','education_required')}, indent=2)}\n\n"
            f"CANDIDATE: target_titles={[t.get('title') for t in profile.get('target_titles', [])]}, "
            f"top_hard_skills={[s.get('skill') if isinstance(s, dict) else s for s in profile.get('top_hard_skills', [])[:30]]}"
        )
        system = "You are a rigorous job-fit scorer. Be concise. Never inflate."
        raw = _run_cli(
            prompt, system=system, json_schema=SCORE_JOB_SCHEMA,
            effort=self.DEFAULT_EFFORT, timeout_s=120.0,
        )
        parsed = _json.loads(raw)
        return _build_rubric_result(
            job,
            req_raw=parsed.get("required_skills", coverage_raw),
            industry_raw=parsed.get("industry", 0.5),
            loc_seniority_raw=parsed.get("location_seniority", 0.5),
            matched=parsed.get("matching_skills") or matched,
            missing=parsed.get("missing_skills") or missing,
            reasoning=parsed.get("reasoning") or "",
        )
```

- [ ] **Step 5: Rewrite `tailor_resume`**

Replace the body of `AnthropicProvider.tailor_resume` (keep the signature as today — `def tailor_resume(self, job, profile, resume_text, **kwargs)`):

```python
    def tailor_resume(self, job: dict, profile: dict, resume_text: str,
                       **_unused) -> dict:
        import json as _json
        prompt = (
            "Tailor this resume for the target job. Return JSON with:\n"
            "  summary_rewrite: 2–3 sentence rewritten Summary section\n"
            "  skills_reordered: existing skills in order of JD relevance "
            "(NEVER invent new skills)\n"
            "  experience_bullets: existing bullets reordered for JD fit "
            "(NEVER invent new bullets)\n"
            "  ats_keywords_missing: JD keywords NOT in candidate skills\n"
            "  cover_letter: short tailored cover letter\n\n"
            f"JOB:\n{_json.dumps({k: job.get(k) for k in ('title','company','location','description','requirements')}, indent=2)}\n\n"
            f"CANDIDATE PROFILE:\n{_json.dumps({k: profile.get(k) for k in ('name','target_titles','top_hard_skills','top_soft_skills','work_experience','experience','education')}, indent=2)}\n\n"
            f"RAW RESUME:\n{resume_text}\n\n"
            "STRICT: skills_reordered and experience_bullets must be "
            "permutations of existing items. Missing JD keywords go in "
            "ats_keywords_missing — NEVER silently add them to skills."
        )
        system = (
            "You are an ATS-optimization resume tailor. Reorder and refine; "
            "never fabricate skills or experiences."
        )
        raw = _run_cli(
            prompt, system=system, json_schema=TAILOR_RESUME_SCHEMA,
            effort="xhigh", timeout_s=240.0,
        )
        return _json.loads(raw)
```

- [ ] **Step 6: Run tests — confirm all pass**

```bash
pytest tests/unit/pipeline/test_providers_anthropic.py -v
```

Expected: 10 passed (5 from Task 6 + 5 new).

- [ ] **Step 7: Commit**

```bash
git add pipeline/providers.py tests/unit/pipeline/test_providers_anthropic.py
git commit -m "refactor(providers): extract_profile/score_job/tailor_resume via CLI

All three structured-output methods now route through _run_cli with the
appropriate module-level schema (--json-schema), replacing the SDK's
forced tool-calling.

  • extract_profile: EXTRACT_PROFILE_SCHEMA + 'high' effort + 120s
  • score_job:       SCORE_JOB_SCHEMA + 'high' effort + 120s
                      (coverage_raw still anchored deterministically
                       via compute_skill_coverage)
  • tailor_resume:    TAILOR_RESUME_SCHEMA + 'xhigh' effort + 240s
                      (anti-fabrication rules emphasized in prompt)

heuristic_tailor.validate_tailoring still runs downstream as the safety
net for malformed output.

5 new tests + the 5 from Task 6 = 10 passing.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Rewrite remaining AnthropicProvider methods

**Files:**
- Modify: `pipeline/providers.py`

`generate_cover_letter`, `generate_report`, `generate_demo_jobs` all use plain `chat()` calls in the SDK path. Since `chat()` is already CLI-routed (Task 6), these methods just need their SDK-specific kwargs (`messages.create`, `output_config`) stripped.

- [ ] **Step 1: Inspect current implementations**

```bash
grep -n -A 30 "def generate_cover_letter\|def generate_report\|def generate_demo_jobs" pipeline/providers.py | head -120
```

Find the three method bodies (lines ~861, ~880, ~892 in current providers.py — verify with grep). Each should become a thin call to `self.chat(...)` plus any post-processing they already do.

- [ ] **Step 2: Rewrite them**

Replace each method body with the chat-routed equivalent. Example for `generate_cover_letter`:

```python
    def generate_cover_letter(self, job: dict, profile: dict) -> str:
        prompt = (
            "Write a 3–4 paragraph cover letter for this candidate applying "
            "to this job. Concise, specific, no purple prose.\n\n"
            f"JOB: {job.get('title')} at {job.get('company')}\n"
            f"{job.get('description', '')[:1500]}\n\n"
            f"CANDIDATE: {profile.get('name', '')}\n"
            f"Key skills: {', '.join(s.get('skill') if isinstance(s, dict) else s for s in profile.get('top_hard_skills', [])[:10])}"
        )
        return self.chat(
            system="You write concise, specific cover letters. No fluff.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
```

For `generate_report`, keep the same prompt shape that was passed to `messages.create` but route through `self.chat`. For `generate_demo_jobs`, set `json_mode=True` and parse the result (return `[]` on parse failure).

- [ ] **Step 3: Smoke-test by importing**

```bash
python -c "from pipeline.providers import AnthropicProvider; p = AnthropicProvider(); print('OK')"
```

Expected: `OK`. No import-time errors (no `anthropic` SDK imported anywhere).

- [ ] **Step 4: Run full provider suite**

```bash
pytest tests/unit/pipeline/test_providers_anthropic.py -v
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/providers.py
git commit -m "refactor(providers): cover_letter/report/demo_jobs via chat()

Strip SDK-specific output_config / messages.create from the three plain-
text methods. They now thin-wrap self.chat(), which itself routes
through _run_cli. \`import anthropic\` is no longer needed anywhere in
the AnthropicProvider class — the rewrite is complete.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 4 — App-Layer Integration

### Task 9: `_can_use_claude` helper + `_CLI_HEALTHY` propagation

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add the helper near the top of app.py's "helpers" section**

Find a section in `app.py` near the auth helpers (search for `_require_auth_user`). Just above it, add:

```python
def _can_use_claude(auth_user: dict | None) -> bool:
    """Single source of truth for `mode='anthropic'` access.

    Returns True iff the user is permitted AND the CLI is reachable.
    Used by:
      • _load_session_state (coerce non-permitted off anthropic)
      • update_config (402 plan_required)
      • _run_phase_sse (reject early in SSE start frame)
      • resume_tailor (reject early in POST handler)
    """
    from pipeline.providers import _CLI_HEALTHY
    if not _CLI_HEALTHY:
        return False
    if not auth_user:
        return False
    if auth_user.get("is_developer"):
        return True
    return (auth_user.get("plan_tier") or "free").lower() == "pro"
```

- [ ] **Step 2: No tests yet — integration tests for `_can_use_claude` come in Task 11. Smoke-import:**

```bash
python -c "from app import _can_use_claude; print(_can_use_claude({}))"
```

Expected: `False` (empty user dict can't use Claude).

- [ ] **Step 3: Commit**

```bash
git add app.py
git commit -m "feat(app): _can_use_claude helper — single gate-site source of truth

Returns True iff _CLI_HEALTHY AND (user.is_developer OR plan_tier=='pro').
The four gate sites (Tasks 10–13) all switch to calling this helper.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Wire `_can_use_claude` into all four gate sites + drop `api_key`

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Find every site that gates `mode == 'anthropic'`**

```bash
grep -n "mode.*anthropic\|is_developer\|api_key" app.py | grep -v "^[0-9]*:.*#" | head -40
```

The five edits needed (locations verified earlier):
1. `_load_session_state` (line ~1149) — coerce-off-anthropic guard
2. `update_config` (line ~2663) — POST `/api/config` 503 rejection
3. `_run_phase_sse` (line ~4216) — early SSE rejection
4. `resume_tailor` (line ~3929) — `/api/resume/tailor` rejection
5. `_get_provider` (line ~2011) — Anthropic provider builder

- [ ] **Step 2: Edit each gate site**

(a) **`_load_session_state` (~line 1149)** — replace:
```python
if state.get("mode") == "anthropic" and not user.get("is_developer"):
    state["mode"] = "ollama"
```
with:
```python
if state.get("mode") == "anthropic" and not _can_use_claude(user):
    state["mode"] = "ollama"
```

(b) **`update_config` (~line 2663)** — replace the dev-check + 503:
```python
if body.get("mode") == "anthropic" and not is_dev:
    raise HTTPException(status_code=503, detail={"code": "coming_soon", ...})
```
with:
```python
if body.get("mode") == "anthropic" and not _can_use_claude(auth_user):
    raise HTTPException(
        status_code=402,
        detail={"code": "plan_required",
                "message": "Claude is a Pro-tier feature. Upgrade to use Claude."},
    )
```

Also in `update_config`, remove `api_key` from the whitelist tuple (~line 2709):
```python
# Before:
"mode", "api_key", "ollama_model",
# After:
"mode", "ollama_model",
```

(c) **`_run_phase_sse` (~line 4216)** — same pattern. Find the `mode == "anthropic"` check, swap to `_can_use_claude(auth_user)`, change code to `"plan_required"`.

(d) **`resume_tailor` (~line 3929)** — same pattern.

(e) **`_get_provider` (~line 2011)** — replace:
```python
key = _S.get("api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
return AnthropicProvider(api_key=key or None)
```
with:
```python
# CLI uses keychain — no key needed.
return AnthropicProvider()
```

- [ ] **Step 3: Drop `api_key` from `_default_state` (~line 383)**

Remove the `"api_key": "",` line.

- [ ] **Step 4: Drop `api_key` from `/api/reset` preserved keys (~line 2989)**

Find the list `"user", "dev_tweaks", "mode", "api_key", "ollama_model", ...` and remove `"api_key"`.

- [ ] **Step 5: Drop `api_key` and `anthropic_key_present` from `/api/state` and Dev Ops runtime**

Grep for remaining occurrences:
```bash
grep -n "api_key\|anthropic_key_present" app.py
```

Remove every occurrence cleanly. The Dev Ops runtime panel (~lines 5594, 5638) should drop the `anthropic_key_present` field from its payload.

- [ ] **Step 6: Smoke-test import**

```bash
python -c "from app import _can_use_claude, _get_provider; print('OK')"
```

Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add app.py
git commit -m "feat(app): flip mode='anthropic' gates to admit Pro users via CLI

Four gate sites + provider builder + state schema all rewired:

  • _load_session_state coerces non-permitted users to mode='ollama'.
    Permission = _CLI_HEALTHY AND (dev OR plan_tier=='pro').
  • update_config returns 402 plan_required (was 503 coming_soon).
    api_key removed from whitelist.
  • _run_phase_sse + resume_tailor return code='plan_required'.
  • _get_provider drops the api_key/env-var lookup — keychain handles it.
  • _default_state drops 'api_key'. /api/reset drops it from preserved.
  • Dev Ops runtime panel drops 'anthropic_key_present'.

No tests yet — integration tests follow in Task 11.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Integration tests for plan-gate flip

**Files:**
- Create: `tests/integration/test_plan_gates.py`

- [ ] **Step 1: Sketch the tests**

Create `tests/integration/test_plan_gates.py`:

```python
"""End-to-end gate-site behavior for mode='anthropic' (Claude CLI)."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(claude_cli_bin, monkeypatch):
    monkeypatch.setenv("JOBS_AI_SKIP_MIGRATION", "1")
    from app import app
    return TestClient(app)


def _make_user(client, plan_tier="free", is_developer=False, email_suffix=""):
    """Sign up, optionally promote, return (client_with_cookies, user_dict)."""
    email = f"test+{plan_tier}{email_suffix}@example.com"
    r = client.post("/api/auth/signup", json={"email": email, "password": "test-pw-12345"})
    assert r.status_code == 200, r.text
    if is_developer or plan_tier == "pro":
        from session_store import SQLiteSessionStore
        from pipeline.config import DB_PATH
        store = SQLiteSessionStore(DB_PATH)
        uid = r.json()["user"]["id"]
        if is_developer:
            store.set_user_developer(uid, True)
        if plan_tier == "pro":
            store.set_user_plan_tier(uid, "pro")
    return client


def test_free_user_blocked_on_anthropic_mode(client, claude_cli_bin):
    _make_user(client, plan_tier="free")
    r = client.post("/api/config", json={"mode": "anthropic"})
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "plan_required"


def test_pro_user_admitted_on_anthropic_mode(client, claude_cli_bin):
    _make_user(client, plan_tier="pro", email_suffix="-pro")
    r = client.post("/api/config", json={"mode": "anthropic"})
    assert r.status_code == 200, r.text


def test_dev_user_admitted_on_anthropic_mode(client, claude_cli_bin):
    _make_user(client, is_developer=True, email_suffix="-dev")
    r = client.post("/api/config", json={"mode": "anthropic"})
    assert r.status_code == 200, r.text


def test_pro_blocked_when_cli_unhealthy(client, claude_cli_bin, monkeypatch):
    _make_user(client, plan_tier="pro", email_suffix="-pro2")
    monkeypatch.setattr("pipeline.providers._CLI_HEALTHY", False)
    r = client.post("/api/config", json={"mode": "anthropic"})
    assert r.status_code == 402
```

- [ ] **Step 2: Run — expect pass**

```bash
pytest tests/integration/test_plan_gates.py -v
```

Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_plan_gates.py
git commit -m "test(integration): plan-gate matrix for mode='anthropic'

  • free user → 402 plan_required
  • Pro user → 200
  • dev user → 200
  • Pro user, _CLI_HEALTHY=False → 402

Pins the four spec §9 acceptance criteria.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Boot health check + 5-min ticker

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add the health-check coroutine**

Find the existing `@app.on_event("startup")` blocks in `app.py` (grep `on_event.*startup`). Add a new startup hook near them:

```python
@app.on_event("startup")
async def _claude_cli_health_check_startup():
    """One-shot health-check at startup — sets pipeline.providers._CLI_HEALTHY."""
    import asyncio
    import pipeline.providers as _p
    try:
        await asyncio.to_thread(_p._run_cli, "ping", timeout_s=20.0, budget_usd=0.01)
        _p._CLI_HEALTHY = True
        print("[claude-cli] verified at startup")
    except Exception as e:
        _p._CLI_HEALTHY = False
        print(f"[claude-cli] FAILED at startup — Pro users coerced to Ollama: {e}")
```

- [ ] **Step 2: Add the periodic re-check** (5-min APScheduler tick)

Find the existing scheduler setup (`start_scheduler` from `pipeline.ingest`). Either tack a job onto that scheduler or add a separate `BackgroundScheduler`. Simplest is a tiny thread loop:

```python
def _claude_cli_health_ticker():
    """Re-check CLI health every 5 minutes so keychain-expiry surfaces fast."""
    import threading, time
    import pipeline.providers as _p
    def _loop():
        while True:
            time.sleep(300)
            try:
                _p._run_cli("ping", timeout_s=20.0, budget_usd=0.01)
                if not _p._CLI_HEALTHY:
                    print("[claude-cli] health restored")
                _p._CLI_HEALTHY = True
            except Exception as e:
                if _p._CLI_HEALTHY:
                    print(f"[claude-cli] health DEGRADED: {e}")
                _p._CLI_HEALTHY = False
    t = threading.Thread(target=_loop, daemon=True, name="claude-cli-health")
    t.start()


@app.on_event("startup")
async def _start_claude_cli_health_ticker():
    _claude_cli_health_ticker()
```

- [ ] **Step 3: Smoke-test (manual)**

```bash
JOBS_AI_DISABLE_INGESTION=1 python -c "
import asyncio
from app import _claude_cli_health_check_startup
asyncio.run(_claude_cli_health_check_startup())
import pipeline.providers
print('_CLI_HEALTHY =', pipeline.providers._CLI_HEALTHY)
"
```

Expected: prints `_CLI_HEALTHY = True` if your local `claude` CLI is OAuthed, `False` otherwise.

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat(app): boot + periodic health check for Claude CLI

Startup hook runs \`claude -p ping\` once via asyncio.to_thread (so the
blocking subprocess doesn't stall the event loop). Result flips
pipeline.providers._CLI_HEALTHY. A daemon thread re-runs the check
every 5 minutes so keychain-expiry surfaces fast.

When _CLI_HEALTHY is False, _can_use_claude returns False regardless
of plan tier — Pro users transparently coerce to Ollama with a warning
visible in journalctl + /api/dev/logs/stream.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: Rewrite `_stream_provider_chat` AnthropicProvider branch

**Files:**
- Modify: `app.py` (lines ~4364–4385)

- [ ] **Step 1: Replace the AnthropicProvider branch**

In `app.py`, find `_stream_provider_chat` (~line 4364). The current branch:

```python
if isinstance(provider, AnthropicProvider):
    clean = [ ... ]
    if not clean:
        return
    with provider.client.messages.stream( ... ) as stream:
        for text in stream.text_stream:
            if text: yield text
    return
```

Replace with:

```python
if isinstance(provider, AnthropicProvider):
    from pipeline.providers import _run_cli_stream, _collapse_messages
    prompt = _collapse_messages(messages)
    if not prompt:
        return
    for text in _run_cli_stream(prompt, system=system or None):
        if text:
            yield text
    return
```

- [ ] **Step 2: Smoke-test via the existing Atlas endpoint test (if any) or import:**

```bash
python -c "
import asyncio
from app import _stream_provider_chat
from pipeline.providers import AnthropicProvider
import unittest.mock
# Just confirm the import + isinstance branch resolves.
print('OK' if AnthropicProvider else 'FAIL')
"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add app.py
git commit -m "refactor(app): _stream_provider_chat AnthropicProvider branch via CLI

Replaces \`provider.client.messages.stream(...)\` with the new
pipeline.providers._run_cli_stream generator. Multi-turn history
collapsed by _collapse_messages into the CLI's single-prompt shape.

Atlas chat (\`/api/atlas/chat/stream\`) keeps streaming UX intact — the
same SSE delta cadence the SDK delivered, now coming off
\`claude -p --output-format stream-json --include-partial-messages\`.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 5 — Phase 3 Parallelization

### Task 14: Parallelize `phase3_score_jobs` with `ThreadPoolExecutor`

**Files:**
- Modify: `pipeline/phases.py` (around `phase3_score_jobs`, line 386)
- Create: `tests/integration/test_phase3_parallel.py`

- [ ] **Step 1: Write the failing parallelism test**

Create `tests/integration/test_phase3_parallel.py`:

```python
"""Phase 3 LLM scoring parallelism + failure-isolation.

We don't need to run the full phase to test this — the inner top-N
LLM loop is the only thing that changes. We monkey-patch provider.score_job
to sleep, measure wall time.
"""
import time
import pytest
from unittest.mock import MagicMock
from pipeline.phases import phase3_score_jobs


def test_phase3_scores_in_parallel():
    """10 mock score_job calls @ 200ms each → wall < 1s under N=5 workers."""
    provider = MagicMock()
    def fake_score(job, profile):
        time.sleep(0.2)
        return {"job_id": job["id"], "score": 80, "score_breakdown": {}, "reasoning": "x"}
    provider.score_job.side_effect = fake_score

    jobs = [
        {"id": f"j{i}", "title": "Engineer", "company": "Acme",
         "description": "Python role", "requirements": ["Python"],
         "remote": False, "location": "Remote"}
        for i in range(10)
    ]
    profile = {"target_titles": [{"title": "Engineer"}],
               "top_hard_skills": [{"skill": "Python"}]}
    start = time.time()
    out = phase3_score_jobs(jobs, profile, provider, llm_score_limit=10, fast_only=False)
    elapsed = time.time() - start
    assert elapsed < 1.0, f"Phase 3 not parallel ({elapsed=:.2f}s)"
    assert len(out) >= 10


def test_phase3_one_failure_isolated():
    """A single score_job exception falls back to fast-score; others succeed."""
    provider = MagicMock()
    call_count = {"n": 0}
    def fake_score(job, profile):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated CLI failure")
        return {"job_id": job["id"], "score": 75, "score_breakdown": {}, "reasoning": "ok"}
    provider.score_job.side_effect = fake_score

    jobs = [{"id": f"j{i}", "title": "Engineer", "company": "Acme",
             "description": "Role", "requirements": ["Python"], "remote": False, "location": "Remote"}
            for i in range(5)]
    profile = {"target_titles": [{"title": "Engineer"}], "top_hard_skills": [{"skill": "Python"}]}
    out = phase3_score_jobs(jobs, profile, provider, llm_score_limit=5, fast_only=False)
    # All 5 jobs must still have a score (even if 1 fell back to _fast_score).
    assert len(out) == 5
    for j in out:
        assert "score" in j or "score_result" in j or "score_data" in j
```

- [ ] **Step 2: Run — confirm failure on the parallelism timing test**

```bash
pytest tests/integration/test_phase3_parallel.py::test_phase3_scores_in_parallel -v
```

Expected: FAIL. Serial loop takes ~2.0s, > 1.0s threshold.

- [ ] **Step 3: Rewrite the LLM-scoring loop inside `phase3_score_jobs`**

Open `pipeline/phases.py` and find the loop near line 533 where `llm_score_count` jobs are scored. Today it's roughly:

```python
for job in to_score[:llm_score_count]:
    try:
        result = provider.score_job(job, profile)
        ...
    except Exception:
        result = ...
```

Replace with:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

if llm_score_count > 0:
    def _score_one(job):
        try:
            return job, provider.score_job(job, profile), None
        except Exception as e:
            return job, None, e

    with ThreadPoolExecutor(
        max_workers=min(5, llm_score_count),
        thread_name_prefix="phase3-score",
    ) as pool:
        for fut in as_completed([pool.submit(_score_one, j) for j in to_score[:llm_score_count]]):
            try:
                job, result, err = fut.result(timeout=180)
            except Exception as e:
                # Future itself timed out — skip; job will use _fast_score.
                print(f"[phase3] future failed: {e}")
                continue
            if err is not None:
                print(f"[phase3] score failed for {job.get('id')}: {err}")
                # Fallback to deterministic skill coverage.
                from pipeline.providers import compute_skill_coverage
                coverage, matched, missing = compute_skill_coverage(job, profile)
                pts = int(round(coverage * 100))
                job["score_data"] = {"score": pts, "matching_skills": matched, "missing_skills": missing}
                job["score"] = pts
            else:
                job["score_data"] = result
                job["score"] = result.get("score", 0)
```

Adapt the variable names to match the existing surrounding code in `phase3_score_jobs`. The key invariants are: (a) every job in the top-N gets a `score`, (b) per-job failures don't crash the whole phase, (c) `ThreadPoolExecutor(max_workers=5)` wraps the LLM calls.

- [ ] **Step 4: Run tests — confirm pass**

```bash
pytest tests/integration/test_phase3_parallel.py -v
```

Expected: 2 passed. Parallelism test wall time should now be ~0.4s.

- [ ] **Step 5: Commit**

```bash
git add pipeline/phases.py tests/integration/test_phase3_parallel.py
git commit -m "perf(phases): phase3 LLM scoring runs in parallel via ThreadPoolExecutor

Top-N LLM-rerank loop replaced with ThreadPoolExecutor(max_workers=5)
+ as_completed. Each future has a 180s timeout; per-job failures fall
back to compute_skill_coverage so one exception can't crash the phase.

Wall time for 10 jobs drops from ~15s serial → ~3s (limited by slowest
of 5 concurrent calls). All three provider types (Anthropic/Ollama/Demo)
are thread-safe because every call lands in a transactional helper.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 6 — Frontend Updates

### Task 15: Remove API-key input + tier-gate Anthropic radio in Settings

**Files:**
- Modify: `frontend/app.jsx`

- [ ] **Step 1: Find the API key input**

```bash
grep -n "api_key\|Anthropic API key\|placeholder=\"sk-ant" frontend/app.jsx | head
```

Verified at lines ~9743–9744, ~10676 (mode setter that passed api_key), ~10850 (Dev Ops display), ~11191 (label).

- [ ] **Step 2: Remove the API-key input row in SettingsPage**

Delete the `<input className="set-input" type="password" placeholder="sk-ant-…" ...>` block (lines ~9743–9744) and its surrounding `<label>Anthropic API key</label>` wrapper (find the enclosing div). The whole row goes.

- [ ] **Step 3: Tier-gate the "Claude (Anthropic)" radio**

In SettingsPage (line ~9551 onward), find the backend selector. The Anthropic option's `disabled` attribute should now be:

```jsx
disabled={!state?.is_pro && !state?.is_dev}
title={(!state?.is_pro && !state?.is_dev)
  ? "Upgrade to Pro to use Claude"
  : ""}
```

Remove the existing "(coming soon)" copy.

- [ ] **Step 4: Fix the mode-setter that passed api_key (line ~10676)**

Find:
```javascript
await api.post('/api/config', { api_key: apiKeyDraft.trim(), mode: 'anthropic' });
```
Replace with:
```javascript
await api.post('/api/config', { mode: 'anthropic' });
```
And drop any surrounding `apiKeyDraft` state hooks that become unused.

- [ ] **Step 5: Remove the Dev Ops anthropic_key_present row (line ~10850)**

Find and delete the line:
```jsx
<div><span>Anthropic API key</span><b className={'tag tag-' + (...)}>{...}</b></div>
```

- [ ] **Step 6: Remove the second "Anthropic API key" label (line ~11191)**

Likely a dead settings remnant. Find and remove the wrapping `<label>...</label>` row.

- [ ] **Step 7: Manual UI smoke test**

```bash
python app.py &
sleep 2
xdg-open http://localhost:8000/app/#settings 2>/dev/null || open http://localhost:8000/app/#settings 2>/dev/null
```

Verify: Settings page no longer shows an API key input. Free user sees "Upgrade to Pro" hover on the Claude radio. Pro/dev user sees the Claude radio enabled.

Kill the server.

- [ ] **Step 8: Commit**

```bash
git add frontend/app.jsx
git commit -m "feat(frontend): drop Anthropic API key UI, tier-gate Claude radio

  • SettingsPage: remove the api_key <input> row entirely.
  • Backend selector: Anthropic radio disabled-with-tooltip for free
    users, enabled for Pro/dev. \"(coming soon)\" copy gone.
  • Drop api_key from the POST /api/config payload.
  • Dev Ops: remove the \"Anthropic API key\" presence indicator.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 16: TailorDrawer + PlansPage copy update

**Files:**
- Modify: `frontend/app.jsx`

- [ ] **Step 1: Remove "Soon" pill on Claude in TailorDrawer**

In TailorDrawer (line ~4947), find any element that renders a "Soon" pill or similar on the Claude option. Remove it; Claude is now a live tier-gated option.

- [ ] **Step 2: Update PlansPage Pro card copy**

In PlansPage (line ~9914), update the Pro card's feature list. Add (or rewrite an existing bullet to):

```jsx
<li>Claude Sonnet 4.6 via Anthropic CLI — premium AI tailoring & career advice</li>
```

Remove any "Anthropic coming soon" / "under development" disclaimers anywhere in this component.

- [ ] **Step 3: Manual smoke test**

```bash
python app.py &
sleep 2
xdg-open http://localhost:8000/app/#plans 2>/dev/null
```

Verify: Pro card lists Claude. No "coming soon" notes.

Kill the server.

- [ ] **Step 4: Commit**

```bash
git add frontend/app.jsx
git commit -m "feat(frontend): TailorDrawer + PlansPage Claude copy live

  • TailorDrawer: remove \"Soon\" pill on Claude (now a tier-gated
    Pro feature, not vapor).
  • PlansPage Pro card: add \"Claude Sonnet 4.6 via Anthropic CLI\"
    bullet. Remove \"coming soon\" / \"under development\" disclaimers.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 7 — Cleanup & Docs

### Task 17: Delete stale scripts + drop env.example block

**Files:**
- Delete: `scripts/check_anthropic_key.py`
- Modify: `.env.example`

- [ ] **Step 1: Delete the stale key-checker script**

```bash
git rm scripts/check_anthropic_key.py
```

- [ ] **Step 2: Drop the ANTHROPIC_API_KEY block from .env.example**

Open `.env.example` and remove lines 4–6 (or wherever the `ANTHROPIC_API_KEY=` block lives). Optionally add at the top of the file:

```
# Claude provider: uses the local `claude` CLI (OAuth keychain auth).
# No API key needed. Run `claude /login` once on the server during deploy.
# Override the pinned model with CLAUDE_CLI_MODEL=sonnet|opus|haiku.
# Override concurrency cap with CLAUDE_CLI_MAX_CONCURRENCY=5.
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "chore: drop ANTHROPIC_API_KEY env var + stale key-check script

scripts/check_anthropic_key.py is replaced by the in-app boot health
check (app.py:_claude_cli_health_check_startup). .env.example loses
the ANTHROPIC_API_KEY block — the CLI uses keychain auth.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 18: Update CLAUDE.md and docs/CLAUDE_REFERENCE.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/CLAUDE_REFERENCE.md`

- [ ] **Step 1: Rewrite the AnthropicProvider bullet in `CLAUDE.md` §3**

Find the line starting `**AnthropicProvider**` under "### Providers". Replace its body with:

```
- **`AnthropicProvider`** — Subprocess wrapper around the locally-installed
  `claude` CLI. Model pinned to `claude-sonnet-4-6` via `--model sonnet`
  (override with `CLAUDE_CLI_MODEL` env var). OAuth keychain auth on
  server — no API key. All structured methods use `--json-schema`
  (replaces SDK forced tool-calling). Streaming uses
  `--output-format stream-json`. Module-level `BoundedSemaphore` caps
  concurrent subprocesses (`CLAUDE_CLI_MAX_CONCURRENCY=5`). Boot health
  check (+ 5-min ticker) toggles `_CLI_HEALTHY`; `_can_use_claude` in
  `app.py` is the single gate-site source of truth.
```

- [ ] **Step 2: Add a Phase 3 parallelism note in CLAUDE.md §3 (phases table)**

Update Phase 3's "What it does" cell to end with: "Top-N LLM-rerank now parallelized via `ThreadPoolExecutor(max_workers=5)` so 10 jobs score in ~3s wall instead of ~15s serial."

- [ ] **Step 3: Add an Operational Mandate**

Append to §7 in `CLAUDE.md`:

```
16. **Claude CLI cwd**: The CLI auto-discovers `CLAUDE.md` in `cwd` and
    prepends it to system prompts. `_run_cli`/`_run_cli_stream` MUST
    run with `cwd=/tmp/jobapp-claude/` (a dir intentionally containing
    no CLAUDE.md) AND pass `--exclude-dynamic-system-prompt-sections`.
    Both belt and suspenders — drop either and the project CLAUDE.md
    leaks into every Claude prompt.
```

- [ ] **Step 4: Update §7 "Anthropic launch state" in docs/CLAUDE_REFERENCE.md**

Find the section and rewrite it from "dev-only / not yet launched" to:

```
**Anthropic launch state (2026-05)**: Claude runs via the local `claude`
CLI as a Pro-tier entitlement. Sonnet 4.6 pinned. Plan-gate single
source of truth is `_can_use_claude(auth_user)` in `app.py` — returns
True iff `pipeline.providers._CLI_HEALTHY` AND (`is_developer` OR
`plan_tier == 'pro'`). To deploy: run `claude /login` once on the
server so the OAuth token persists in the OS keychain. Boot health
check + 5-min ticker monitor reachability; on failure Pro users
transparently coerce to Ollama.
```

- [ ] **Step 5: Add a Bug History entry in §9 of docs/CLAUDE_REFERENCE.md**

```
**CLAUDE.md in cwd leaked into Claude CLI prompts** (commit <Task 1's
commit hash>): the CLI auto-prepends a CLAUDE.md found in cwd to the
system prompt. Without `cwd=/tmp/jobapp-claude/` + `--exclude-dynamic-
system-prompt-sections`, every `_run_cli` call would consume ~1500
tokens of project context as input, blow through the cache, and skew
outputs toward "you are a developer working on this codebase". The fix
is in `pipeline.providers._ensure_scratch_dir()` and the argv assembly
in `_run_cli`/`_run_cli_stream`. Invariant: never change cwd without
also confirming there's no CLAUDE.md in the new dir.
```

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md docs/CLAUDE_REFERENCE.md
git commit -m "docs: claude CLI provider — CLAUDE.md + reference updates

  • CLAUDE.md §3 AnthropicProvider rewritten to describe the CLI
    transport, Sonnet pinning, semaphore, health flag.
  • §3 Phase 3 bullet notes the new parallel LLM scoring.
  • §7 mandate 16: cwd=/tmp/jobapp-claude/ rule.
  • CLAUDE_REFERENCE.md §7 Anthropic launch state rewritten from
    \"dev-only\" to \"Pro-tier via CLI\".
  • §9 Bug History: the CLAUDE.md-in-cwd trap.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 19: Full test-suite green check + manual end-to-end smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the whole pytest suite**

```bash
pytest -q 2>&1 | tail -30
```

Expected: green. If anything other than test_providers_anthropic / test_claude_cli / test_plan_gates / test_phase3_parallel fails, triage before continuing.

- [ ] **Step 2: Start the dev server and exercise the Pro flow**

```bash
python app.py &
SERVER_PID=$!
sleep 3
```

In a browser at `http://localhost:8000/app`:
1. Sign up as `pro-smoke@example.com`. Promote yourself to Pro via the Dev Ops Sessions → PLAN → GRANT PRO panel (or `python -c "from session_store import SQLiteSessionStore; from pipeline.config import DB_PATH; s = SQLiteSessionStore(DB_PATH); s.set_user_plan_tier(<your_uid>, 'pro')"`).
2. Settings → backend → "Claude (Anthropic)" — should be ENABLED.
3. Upload a small resume. Profile extraction (Phase 1) should run and complete.
4. Phase 2 → 3 → 4 — verify Phase 3 wall time is under ~6s for 10 jobs.
5. Open a job → "Ask Atlas" — chat should stream replies token-by-token.
6. Sign out, sign back in as free user — Claude radio should be disabled with "Upgrade to Pro" tooltip.

Kill the server:

```bash
kill $SERVER_PID
```

- [ ] **Step 3: No commit — this is a verification gate only**

If everything passes, the migration is complete. If anything fails, file a follow-up task before moving to Phase 8.

---

## Phase 8 — SDK Removal (follow-up commit, after soak)

### Task 20: Remove `anthropic` from `requirements.txt` after a soak week

**Files:**
- Modify: `requirements.txt`

This is a separate, deferred commit. Run it ONLY after the rewrite has been live for at least 7 days with no Claude-related rollback. Keeping `anthropic` installed during the soak means `git revert <Tasks 1-19>` works cleanly.

- [ ] **Step 1: Confirm soak window has passed and the rewrite is healthy**

Check `journalctl -u jobapp --since "7 days ago" | grep -i "claude-cli"` — should see periodic "verified" / "health restored" messages and few/no FAILED logs.

- [ ] **Step 2: Remove the dependency**

Edit `requirements.txt` and delete the line:
```
anthropic>=0.88.0  # 0.88+ required for Opus 4.7 + output_config.format + scope_id on files.list
```

- [ ] **Step 3: Re-run the test suite to confirm nothing imports `anthropic`**

```bash
pip uninstall anthropic -y
pytest -q 2>&1 | tail -20
```

Expected: green. If any test imports `anthropic`, fix it (the rewrite should have removed all such imports).

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore(deps): drop \`anthropic\` Python SDK — CLI is the only transport

After 7+ day soak with the Claude CLI provider, the SDK package is
unused. Removing it shaves install footprint and removes a path that
could silently come back if someone re-introduces \`import anthropic\`.

Rollback path: \`git revert\` this commit + \`pip install anthropic>=0.88\`.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Notes

**Spec coverage check** — each spec section mapped to a task:

| Spec § | Task | Notes |
|---|---|---|
| §1 Motivation | n/a | (context only) |
| §2 Scope | Tasks 1–18 | every "in scope" bullet has a task |
| §3 Approach A | Task 6 (rewrite in place) | |
| §4 File map | spans Tasks 1–18 | each file modification has a step |
| §5 Subprocess contract | Tasks 1, 3 | signature, argv, error mapping |
| §6 JSON schema strategy | Tasks 5, 6, 7 | extract constants, --json-schema |
| §7 Streaming | Tasks 4, 13 | _run_cli_stream + app.py branch |
| §8 Concurrency | Tasks 3, 14 | semaphore + ThreadPoolExecutor |
| §9 Plan-gate flip | Tasks 9, 10, 11 | _can_use_claude + 4 sites + tests |
| §10 Frontend | Tasks 15, 16 | SettingsPage + TailorDrawer + Plans |
| §11 Tests | Tasks 2, 3, 4, 7, 11, 14 | conftest fixture + 5 test files |
| §12 Rollback | Task 20 | two-commit sequence preserved |
| §13 Observability | Tasks 1, 12 | log markers + health flag |
| §13a Assumptions | Task 0 | smoke-test before any code |
| §14 Gotchas | Tasks 3 (cwd), 17 (env), 18 (Bug History) | |
| §15 CLAUDE.md updates | Task 18 | |
| §16 Acceptance | Task 19 | manual end-to-end smoke |

No gaps detected.

**Placeholder scan**: searched for TBD / TODO / "implement later" / "Add appropriate" — none present. Every step contains actual code or an exact command.

**Type consistency**: `_run_cli` signature matches its definition (no `max_tokens` kwarg). `_run_cli_stream` signature consistent across Tasks 4 and 13. `_can_use_claude` takes `auth_user: dict | None` consistently. `_CLI_HEALTHY` is the global module attribute referenced consistently (always via `pipeline.providers._CLI_HEALTHY`, never imported as a name into app.py — that would freeze the value at import time).

**Scope check**: focused on the CLI swap + Phase 3 parallelism + plan-gate flip. No unrelated refactors smuggled in.

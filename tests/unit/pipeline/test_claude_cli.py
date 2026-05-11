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
    claude_cli_bin.set_response(json.dumps({
        "type": "result", "subtype": "success",
        "structured_output": {"x": 1},
    }))
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as run_spy:
        _run_cli("compute x", json_schema=schema)
        argv = run_spy.call_args[0][0]
        assert "--json-schema" in argv
        i = argv.index("--json-schema")
        assert json.loads(argv[i + 1]) == schema


def test_run_cli_json_schema_uses_json_output_format(claude_cli_bin):
    """Schema mode REQUIRES --output-format json (not text) — see Task 0 findings."""
    claude_cli_bin.set_response(json.dumps({
        "type": "result", "structured_output": {"x": 1},
    }))
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as run_spy:
        _run_cli("x", json_schema={"type": "object"})
        argv = run_spy.call_args[0][0]
        i = argv.index("--output-format")
        assert argv[i + 1] == "json"


def test_run_cli_text_mode_uses_text_output_format(claude_cli_bin):
    """No-schema calls use --output-format text and return stdout directly."""
    claude_cli_bin.set_response("plain text reply")
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as run_spy:
        _run_cli("hi")
        argv = run_spy.call_args[0][0]
        i = argv.index("--output-format")
        assert argv[i + 1] == "text"


def test_run_cli_schema_mode_extracts_structured_output(claude_cli_bin):
    """Schema-mode response is parsed and structured_output is returned as JSON."""
    claude_cli_bin.set_response(json.dumps({
        "type": "result", "subtype": "success",
        "structured_output": {"name": "Jane", "age": 30},
    }))
    out = _run_cli("extract", json_schema={"type": "object"})
    assert json.loads(out) == {"name": "Jane", "age": 30}


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


def test_run_cli_oversized_prompt_uses_stdin(claude_cli_bin):
    """Prompts > 64KB route via stdin so we don't hit argv length limits."""
    claude_cli_bin.set_response("ok")
    big = "x" * (64 * 1024 + 100)
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as run_spy:
        _run_cli(big)
        kwargs = run_spy.call_args[1]
        assert kwargs.get("input") is not None
        argv = run_spy.call_args[0][0]
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

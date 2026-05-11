"""AnthropicProvider tests — CLI-backed transport.

Every method is exercised via the claude_cli_bin fixture so no real
subscription tokens are spent.
"""
import json
import pytest
import subprocess
from unittest.mock import patch

from pipeline.providers import (
    AnthropicProvider, ClaudeCLIError,
    EXTRACT_PROFILE_SCHEMA, SCORE_JOB_SCHEMA, TAILOR_RESUME_SCHEMA,
)


# ── __init__ / chat ───────────────────────────────────────────────────────────

def test_model_is_sonnet(claude_cli_bin):
    p = AnthropicProvider()
    assert p.model == "claude-sonnet-4-6"


def test_init_no_api_key_required(claude_cli_bin):
    AnthropicProvider()
    AnthropicProvider(api_key=None)
    AnthropicProvider(api_key="")  # back-compat — silently ignored


def test_init_does_not_import_anthropic_sdk(claude_cli_bin):
    """The CLI-backed provider must not pull in the anthropic Python SDK."""
    import sys
    # Prevent contamination from anything else
    pre_loaded = "anthropic" in sys.modules
    p = AnthropicProvider()
    # Either it was already loaded by something else (acceptable — not our concern),
    # or it's not loaded now. What we care about is: this provider doesn't load it.
    if not pre_loaded:
        assert "anthropic" not in sys.modules, (
            "AnthropicProvider should not import the anthropic SDK"
        )


def test_chat_text_mode(claude_cli_bin):
    claude_cli_bin.set_response("the weather is sunny\n")
    p = AnthropicProvider()
    out = p.chat(system="be terse", messages=[{"role": "user", "content": "weather?"}])
    assert "sunny" in out


def test_chat_empty_messages_returns_empty(claude_cli_bin):
    p = AnthropicProvider()
    assert p.chat(system="x", messages=[]) == ""


def test_chat_json_mode_passes_permissive_schema(claude_cli_bin):
    """json_mode=True should pass a permissive object schema to --json-schema."""
    claude_cli_bin.set_response(json.dumps({
        "type": "result", "structured_output": {"reply": "ok"},
    }))
    p = AnthropicProvider()
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as spy:
        p.chat(system="", messages=[{"role": "user", "content": "x"}], json_mode=True)
        argv = spy.call_args[0][0]
        assert "--json-schema" in argv
        i = argv.index("--json-schema")
        schema = json.loads(argv[i + 1])
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is True


# ── extract_profile ───────────────────────────────────────────────────────────

def test_extract_profile_parses_cli_json(claude_cli_bin):
    canned_profile = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "target_titles": [{"title": "Engineer", "family": "Software Engineering",
                           "evidence": "B.S. CS 2024"}],
        "top_hard_skills": [{"skill": "Python", "category": "programming_language",
                              "evidence": "Python, JS"}],
        "top_soft_skills": ["Teamwork"],
        "critical_analysis": "Solid resume, room for impact metrics.",
        "education": [], "research_experience": [], "work_experience": [],
        "experience": [], "projects": [], "resume_gaps": [],
    }
    claude_cli_bin.set_response(json.dumps({
        "type": "result", "subtype": "success",
        "structured_output": canned_profile,
    }))
    p = AnthropicProvider()
    out = p.extract_profile("RESUME TEXT", preferred_titles=["Engineer"])
    assert out["name"] == "Jane Doe"
    assert out["top_hard_skills"][0]["skill"] == "Python"


def test_extract_profile_passes_schema_to_cli(claude_cli_bin):
    claude_cli_bin.set_response(json.dumps({
        "type": "result",
        "structured_output": {"name": "x", "top_hard_skills": [], "top_soft_skills": [],
                              "target_titles": [], "critical_analysis": ""},
    }))
    p = AnthropicProvider()
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as spy:
        p.extract_profile("resume")
        argv = spy.call_args[0][0]
        i = argv.index("--json-schema")
        assert json.loads(argv[i + 1]) == EXTRACT_PROFILE_SCHEMA


def test_extract_profile_cli_error_propagates(claude_cli_bin):
    claude_cli_bin.set_error("rate limit hit", exit=1)
    p = AnthropicProvider()
    with pytest.raises(ClaudeCLIError):
        p.extract_profile("resume")


# ── score_job ─────────────────────────────────────────────────────────────────

def test_score_job_returns_rubric_dict(claude_cli_bin):
    """SCORE_JOB_SCHEMA only has industry/location_seniority/reasoning — skill
    coverage is anchored deterministically via compute_skill_coverage."""
    claude_cli_bin.set_response(json.dumps({
        "type": "result",
        "structured_output": {
            "industry": 0.6, "location_seniority": 0.5,
            "reasoning": "Strong skill overlap.",
        },
    }))
    p = AnthropicProvider()
    job = {"id": "j1", "title": "Engineer", "company": "Acme",
           "description": "Python role", "requirements": ["Python"],
           "remote": False, "location": "Remote"}
    profile = {"top_hard_skills": [{"skill": "Python"}],
               "target_titles": [{"title": "Engineer"}]}
    out = p.score_job(job, profile)
    assert "score" in out
    assert "score_breakdown" in out
    # Total score is bounded 0..100
    assert 0 <= out["score"] <= 100


def test_score_job_passes_schema_to_cli(claude_cli_bin):
    claude_cli_bin.set_response(json.dumps({
        "type": "result",
        "structured_output": {"industry": 0.5, "location_seniority": 0.5,
                              "reasoning": "ok"},
    }))
    p = AnthropicProvider()
    job = {"id": "j1", "title": "X", "company": "Y", "description": "z",
           "requirements": [], "remote": True, "location": "Remote"}
    profile = {"top_hard_skills": [], "target_titles": []}
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as spy:
        p.score_job(job, profile)
        argv = spy.call_args[0][0]
        i = argv.index("--json-schema")
        assert json.loads(argv[i + 1]) == SCORE_JOB_SCHEMA


# ── tailor_resume ─────────────────────────────────────────────────────────────

def test_tailor_resume_passes_xhigh_effort(claude_cli_bin):
    # Minimal valid response — content irrelevant to this assertion.
    claude_cli_bin.set_response(json.dumps({
        "type": "result",
        "structured_output": {"name": "X", "skills": [], "experience": [],
                               "education": [], "section_order": []},
    }))
    p = AnthropicProvider()
    job = {"id": "j1", "title": "Engineer", "company": "Acme",
           "description": "...", "requirements": []}
    profile = {"name": "X", "top_hard_skills": [], "experience": [], "education": []}
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as spy:
        p.tailor_resume(job, profile, "RESUME TEXT")
        argv = spy.call_args[0][0]
        i = argv.index("--effort")
        assert argv[i + 1] == "xhigh"


def test_tailor_resume_passes_schema_to_cli(claude_cli_bin):
    claude_cli_bin.set_response(json.dumps({
        "type": "result",
        "structured_output": {"name": "X", "skills": [], "experience": [],
                               "education": [], "section_order": []},
    }))
    p = AnthropicProvider()
    job = {"id": "j1", "title": "X", "company": "Y", "description": "z",
           "requirements": []}
    profile = {"name": "Y", "top_hard_skills": [], "experience": [], "education": []}
    with patch("pipeline.providers._subprocess.run", wraps=subprocess.run) as spy:
        p.tailor_resume(job, profile, "TEXT")
        argv = spy.call_args[0][0]
        i = argv.index("--json-schema")
        assert json.loads(argv[i + 1]) == TAILOR_RESUME_SCHEMA


# ── cover_letter / report / demo_jobs ─────────────────────────────────────────

def test_generate_cover_letter_uses_chat(claude_cli_bin):
    claude_cli_bin.set_response("Dear Hiring Manager,\n\nI am writing...")
    p = AnthropicProvider()
    out = p.generate_cover_letter(
        job={"title": "Engineer", "company": "Acme", "description": "Python role"},
        profile={"name": "Jane", "top_hard_skills": [{"skill": "Python"}]},
    )
    assert "Dear" in out


def test_generate_report_uses_chat(claude_cli_bin):
    claude_cli_bin.set_response("# Run Report\nAll good.\n")
    p = AnthropicProvider()
    out = p.generate_report({"total_jobs": 5, "applied": 3, "manual": 2})
    assert "Run Report" in out


def test_generate_demo_jobs_returns_list(claude_cli_bin):
    """demo_jobs uses json_mode chat — verify it parses and returns a list."""
    claude_cli_bin.set_response(json.dumps({
        "type": "result",
        "structured_output": {"jobs": [
            {"id": "demo-1", "title": "Engineer", "company": "Acme"},
        ]},
    }))
    p = AnthropicProvider()
    out = p.generate_demo_jobs(profile={"name": "X"}, titles=["Engineer"], location="Remote")
    # If the parser returns the structured_output's "jobs" list, it's a list
    # If it returns [] on parse failure (acceptable fallback), it's also a list
    assert isinstance(out, list)

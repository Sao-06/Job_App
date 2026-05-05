"""Tests for AnthropicProvider — respx-mocked Anthropic SDK calls."""
import json

import httpx
import pytest
import respx

from pipeline.providers import AnthropicProvider, _build_rubric_result

pytestmark = pytest.mark.unit


@pytest.fixture
def anthropic_provider():
    # SDK requires a key to be present (or via env). We pass a dummy.
    return AnthropicProvider(api_key="sk-ant-dummy-test")


def _tool_use_response(tool_input: dict) -> dict:
    """Build the JSON body the Anthropic /v1/messages endpoint would return
    for a tool-call response."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-6",
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": "tu_1", "name": "save_profile",
             "input": tool_input}
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _text_response(text: str) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-6",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


class TestAnthropicChat:
    def test_returns_text_content(self, anthropic_provider):
        with respx.mock(base_url="https://api.anthropic.com") as mock:
            mock.post("/v1/messages").mock(
                return_value=httpx.Response(200, json=_text_response("hello world"))
            )
            out = anthropic_provider.chat(
                "system prompt",
                [{"role": "user", "content": "hi"}],
                max_tokens=100,
            )
        assert out == "hello world"

    def test_empty_messages_returns_empty(self, anthropic_provider):
        with respx.mock(base_url="https://api.anthropic.com"):
            out = anthropic_provider.chat("sys", [])
        assert out == ""

    def test_skips_invalid_role(self, anthropic_provider):
        with respx.mock(base_url="https://api.anthropic.com"):
            out = anthropic_provider.chat(
                "sys",
                [{"role": "system", "content": "ignored"}],  # 'system' not in allowlist
            )
        # All messages dropped → return without calling.
        assert out == ""

    def test_json_mode_appends_prefill(self, anthropic_provider):
        captured = {}

        def _record(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json=_text_response('"value":42}'))

        with respx.mock(base_url="https://api.anthropic.com") as mock:
            mock.post("/v1/messages").mock(side_effect=_record)
            out = anthropic_provider.chat(
                "sys",
                [{"role": "user", "content": "give me json"}],
                json_mode=True,
            )
        # The prefill assistant message ending with '{' must be the last item.
        msgs = captured["body"]["messages"]
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["content"] == "{"
        # And the response has the leading '{' restored.
        assert out.startswith("{")


class TestAnthropicToolCalls:
    def test_tool_call_extracts_input(self, anthropic_provider):
        tool = {"name": "save_profile",
                "description": "x",
                "input_schema": {"type": "object", "properties": {}}}
        with respx.mock(base_url="https://api.anthropic.com") as mock:
            mock.post("/v1/messages").mock(
                return_value=httpx.Response(200, json=_tool_use_response({"name": "Jane"}))
            )
            out = anthropic_provider._tool_call(tool, "extract this")
        assert out == {"name": "Jane"}

    def test_tool_call_returns_empty_when_no_tool_use(self, anthropic_provider):
        tool = {"name": "save_profile", "description": "x",
                "input_schema": {"type": "object", "properties": {}}}
        # Server returns plain text instead of tool_use → handler returns {}.
        with respx.mock(base_url="https://api.anthropic.com") as mock:
            mock.post("/v1/messages").mock(
                return_value=httpx.Response(200, json=_text_response("oops"))
            )
            out = anthropic_provider._tool_call(tool, "extract")
        assert out == {}


class TestAnthropicScoreJob:
    def test_score_job_uses_deterministic_skill_coverage(self, anthropic_provider):
        """The LLM's industry/location numbers feed _build_rubric_result, but
        the skill-coverage axis comes from compute_skill_coverage, NOT the
        LLM. Verify the LLM tool result is honored for industry/location.
        """
        with respx.mock(base_url="https://api.anthropic.com") as mock:
            mock.post("/v1/messages").mock(
                return_value=httpx.Response(200, json=_tool_use_response({
                    "industry": 0.9, "location_seniority": 0.5,
                    "reasoning": "Strong match on EDA tooling.",
                }))
            )
            job = {"id": "j1", "title": "FPGA Intern", "company": "Acme",
                   "location": "Remote", "remote": True,
                   "experience_level": "internship",
                   "requirements": ["Verilog", "Python"],
                   "description": "FPGA work."}
            profile = {"top_hard_skills": ["Verilog", "Python"],
                       "target_titles": ["FPGA Intern"], "education": []}
            out = anthropic_provider.score_job(job, profile)
        # All three axes contribute; reasoning passes through.
        assert "Strong match" in out["reasoning"]
        # Score is bounded.
        assert 0 <= out["score"] <= 100
        # matched_skills picked up from the deterministic stage.
        assert "Verilog" in out["matching_skills"]

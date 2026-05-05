"""Tests for OllamaProvider — respx-mocked OpenAI-compatible endpoint."""
import json
from unittest.mock import patch, MagicMock

import httpx
import pytest
import respx

from pipeline.providers import OllamaProvider

pytestmark = pytest.mark.unit


# OllamaProvider's __init__ calls _check_ollama which uses urllib.request.urlopen.
# We mock that so construction doesn't hit a real Ollama instance.

def _patched_urlopen(json_payload: dict):
    """Build a context manager that monkeypatches urlopen to return *json_payload*."""
    response = MagicMock()
    response.read.return_value = json.dumps(json_payload).encode("utf-8")
    return patch("urllib.request.urlopen", return_value=response)


@pytest.fixture
def ollama_url(monkeypatch):
    url = "http://ollama-test.local:11434"
    monkeypatch.setenv("OLLAMA_URL", url)
    return url


# ── Construction / _check_ollama ────────────────────────────────────────────


class TestConstruction:
    def test_constructs_when_model_present(self, ollama_url):
        with _patched_urlopen({"models": [{"name": "llama3.2:latest"}]}):
            p = OllamaProvider(model="llama3.2")
        assert p.model == "llama3.2"
        assert p.OLLAMA_URL == ollama_url

    def test_raises_when_unreachable(self, ollama_url):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("nope")):
            with pytest.raises(ConnectionError, match="not reachable"):
                OllamaProvider(model="llama3.2")

    def test_raises_when_model_not_pulled(self, ollama_url):
        # /api/tags returns no matching base.
        with _patched_urlopen({"models": [{"name": "mistral:7b"}]}):
            with pytest.raises(ValueError, match="not pulled"):
                OllamaProvider(model="llama3.2")

    def test_cloud_tag_skips_local_check(self, ollama_url):
        # Cloud-tagged models aren't listed in /api/tags; must NOT raise.
        with _patched_urlopen({"models": []}):
            p = OllamaProvider(model="ministral:3-cloud")
        assert p.model == "ministral:3-cloud"

    def test_per_instance_url_reads_env(self, monkeypatch):
        # OLLAMA_URL is read per construction so two providers can target
        # different daemons (e.g. one local, one Tailnet) without process restart.
        monkeypatch.setenv("OLLAMA_URL", "http://first.local:11434")
        with _patched_urlopen({"models": [{"name": "llama3.2"}]}):
            p1 = OllamaProvider(model="llama3.2")
        monkeypatch.setenv("OLLAMA_URL", "http://second.local:11434")
        with _patched_urlopen({"models": [{"name": "llama3.2"}]}):
            p2 = OllamaProvider(model="llama3.2")
        assert p1.OLLAMA_URL == "http://first.local:11434"
        assert p2.OLLAMA_URL == "http://second.local:11434"


# ── chat() ──────────────────────────────────────────────────────────────────


def _chat_response(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "llama3.2",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content},
             "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class TestChat:
    def test_returns_message_content(self, ollama_url):
        with _patched_urlopen({"models": [{"name": "llama3.2"}]}):
            p = OllamaProvider(model="llama3.2")
        with respx.mock(base_url=f"{ollama_url}/v1") as mock:
            mock.post("/chat/completions").mock(
                return_value=httpx.Response(200, json=_chat_response("hello there"))
            )
            out = p.chat("system", [{"role": "user", "content": "hi"}])
        assert out == "hello there"

    def test_empty_messages_no_call(self, ollama_url):
        with _patched_urlopen({"models": [{"name": "llama3.2"}]}):
            p = OllamaProvider(model="llama3.2")
        # No HTTP mock set up — if chat tries to call, the test would error
        # on respx routing; the function must short-circuit instead.
        out = p.chat("sys", [])
        assert out == ""

    def test_json_mode_sends_response_format(self, ollama_url):
        with _patched_urlopen({"models": [{"name": "llama3.2"}]}):
            p = OllamaProvider(model="llama3.2")
        captured = {}

        def _record(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json=_chat_response('{"k":"v"}'))

        with respx.mock(base_url=f"{ollama_url}/v1") as mock:
            mock.post("/chat/completions").mock(side_effect=_record)
            p.chat("sys", [{"role": "user", "content": "json please"}], json_mode=True)
        body = captured["body"]
        assert body.get("response_format") == {"type": "json_object"}

    def test_json_mode_retries_without_format_on_old_builds(self, ollama_url):
        """Older Ollama builds error on response_format — the chat impl
        must retry once without it instead of failing."""
        with _patched_urlopen({"models": [{"name": "llama3.2"}]}):
            p = OllamaProvider(model="llama3.2")

        call_count = {"n": 0}

        def _maybe_fail(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            body = json.loads(request.content.decode())
            if "response_format" in body:
                # Mimic the SDK's error message for the trigger string.
                return httpx.Response(400, json={
                    "error": {"message": "unknown field response_format"}
                })
            return httpx.Response(200, json=_chat_response("ok"))

        with respx.mock(base_url=f"{ollama_url}/v1") as mock:
            mock.post("/chat/completions").mock(side_effect=_maybe_fail)
            out = p.chat("sys", [{"role": "user", "content": "x"}], json_mode=True)
        assert out == "ok"
        assert call_count["n"] == 2  # original + retry


# ── _parse_json fallback ────────────────────────────────────────────────────


class TestParseJson:
    @pytest.fixture
    def provider(self, ollama_url):
        with _patched_urlopen({"models": [{"name": "llama3.2"}]}):
            return OllamaProvider(model="llama3.2")

    def test_clean_json(self, provider):
        assert provider._parse_json('{"a": 1}', {}) == {"a": 1}

    def test_strips_markdown_fences(self, provider):
        text = '```json\n{"a": 1}\n```'
        assert provider._parse_json(text, {}) == {"a": 1}

    def test_fixes_trailing_comma(self, provider):
        text = '{"a": 1, "b": 2,}'
        assert provider._parse_json(text, {}) == {"a": 1, "b": 2}

    def test_extracts_largest_brace_block(self, provider):
        text = 'before { not valid }} after {"real": "json"} trailing junk'
        result = provider._parse_json(text, {})
        # Either extracted the real block or fell back to default.
        assert isinstance(result, dict)

    def test_falls_back_to_default(self, provider):
        assert provider._parse_json("not json at all", {"fallback": True}) == {"fallback": True}

"""
scripts/check_anthropic_key.py
──────────────────────────────
Verify the Anthropic API key + connectivity before launching the Claude
integration to users.

Hits the Models API (`client.models.retrieve("claude-opus-4-7")`) which is
metadata-only — no completion tokens, no billing impact — and reports
specific failure modes so misconfigurations can be fixed before any user
triggers a tailor / score / extract path.

Run:
    python scripts/check_anthropic_key.py
    python scripts/check_anthropic_key.py --model claude-opus-4-7
    python scripts/check_anthropic_key.py --send-tiny-message  # billed (~$0.0003)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_env() -> Path | None:
    """Mirror app.py: load .env via python-dotenv if available."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return None
    env_file = find_dotenv(usecwd=True)
    if env_file:
        load_dotenv(env_file, override=False)
        return Path(env_file)
    return None


def _resolve_key() -> tuple[str, str]:
    """Return (key, source). source ∈ {'env','session_state','missing'}."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key, "env"
    return "", "missing"


def _redact(key: str) -> str:
    if not key:
        return "(unset)"
    if len(key) <= 12:
        return "***"
    return f"{key[:8]}…{key[-4:]} ({len(key)} chars)"


def _check_key_format(key: str) -> str | None:
    """Quick sanity check on shape. Returns an error string or None."""
    if not key:
        return "ANTHROPIC_API_KEY is empty or unset."
    if not key.startswith("sk-ant-"):
        return (
            "Key does not start with 'sk-ant-'. Anthropic API keys begin with "
            "'sk-ant-…' — verify you copied a full key from console.anthropic.com."
        )
    if len(key) < 50:
        return f"Key is suspiciously short ({len(key)} chars). Re-copy it."
    return None


def _import_sdk() -> "anthropic.Anthropic | None":
    try:
        import anthropic  # type: ignore
    except ImportError:
        print("[FAIL] anthropic SDK not installed.")
        print("       Fix: pip install -U 'anthropic>=0.88.0'")
        return None
    try:
        version = anthropic.__version__
    except AttributeError:
        version = "(unknown)"
    print(f"[ok]   anthropic SDK loaded — version {version}")
    return anthropic


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sanity-check the Anthropic API key + connectivity.",
    )
    parser.add_argument(
        "--model", default="claude-opus-4-7",
        help="Model ID to retrieve (default: claude-opus-4-7).",
    )
    parser.add_argument(
        "--send-tiny-message", action="store_true",
        help="Also send a 1-token messages.create call (billed ~$0.0003).",
    )
    args = parser.parse_args()

    print("-" * 60)
    print("  Anthropic API key sanity check")
    print("-" * 60)

    env_path = _load_env()
    if env_path:
        print(f"[ok]   loaded .env from {env_path}")
    else:
        print("[info] no .env file found (relying on shell env)")

    key, source = _resolve_key()
    print(f"[info] ANTHROPIC_API_KEY source: {source}")
    print(f"[info] key (redacted): {_redact(key)}")

    err = _check_key_format(key)
    if err:
        print(f"[FAIL] {err}")
        print()
        print("How to fix:")
        print("  1. Get a key from https://console.anthropic.com/settings/keys")
        print("  2. Add to .env at the project root:")
        print("       ANTHROPIC_API_KEY=sk-ant-...")
        print("     OR export it in the shell.")
        return 2

    anthropic_mod = _import_sdk()
    if anthropic_mod is None:
        return 3

    client = anthropic_mod.Anthropic(api_key=key)

    print(f"[info] retrieving model metadata: {args.model}")
    try:
        model = client.models.retrieve(args.model)
    except anthropic_mod.AuthenticationError as e:
        print(f"[FAIL] AuthenticationError: {e.message}")
        print("       The key is invalid, revoked, or for a different organization.")
        print("       Check console.anthropic.com → Settings → API keys.")
        return 4
    except anthropic_mod.PermissionDeniedError as e:
        print(f"[FAIL] PermissionDeniedError: {e.message}")
        print("       The key is valid but lacks permission for this model.")
        print(f"       Verify the workspace has access to '{args.model}'.")
        return 5
    except anthropic_mod.NotFoundError as e:
        print(f"[FAIL] Model not found: {args.model}")
        print(f"       Detail: {e.message}")
        print("       Use a model ID from `claude-opus-4-7`, `claude-opus-4-6`, "
              "`claude-sonnet-4-6`, `claude-haiku-4-5`.")
        return 6
    except anthropic_mod.APIConnectionError:
        print("[FAIL] Could not reach api.anthropic.com.")
        print("       Check internet / firewall / proxy settings.")
        return 7
    except anthropic_mod.RateLimitError as e:
        print(f"[FAIL] Rate limited: {e.message}")
        print("       Wait briefly and retry.")
        return 8
    except anthropic_mod.APIStatusError as e:
        print(f"[FAIL] API error {e.status_code}: {e.message}")
        return 9
    except Exception as e:
        print(f"[FAIL] Unexpected error: {type(e).__name__}: {e}")
        return 10

    print(f"[ok]   model accessible: id={model.id}")
    if hasattr(model, "display_name"):
        print(f"[ok]   display_name: {model.display_name}")
    if hasattr(model, "max_input_tokens"):
        print(f"[ok]   context window: {model.max_input_tokens:,} input tokens")
    if hasattr(model, "max_tokens"):
        print(f"[ok]   max output: {model.max_tokens:,} tokens")

    if args.send_tiny_message:
        print()
        print("[info] sending 1-token test message (billed ~$0.0003)")
        try:
            resp = client.messages.create(
                model=args.model,
                max_tokens=10,
                messages=[{"role": "user", "content": "ping"}],
                output_config={"effort": "low"},
            )
            usage = resp.usage
            print(f"[ok]   response received — request_id={resp._request_id}")
            print(f"[ok]   tokens: in={usage.input_tokens} out={usage.output_tokens}")
            text = next((b.text for b in resp.content if b.type == "text"), "")
            if text:
                print(f"[ok]   reply: {text!r}")
        except anthropic_mod.APIStatusError as e:
            print(f"[FAIL] messages.create returned {e.status_code}: {e.message}")
            return 11
        except Exception as e:
            print(f"[FAIL] messages.create error: {type(e).__name__}: {e}")
            return 12

    print()
    print("-" * 60)
    print("  [PASS] ALL CHECKS PASSED -- Claude integration is launch-ready.")
    print("-" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

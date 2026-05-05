"""
Tiny HTTP helpers used by the API source modules.
Stays in its own module so the source files don't all have to repeat
the urllib boilerplate.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from typing import Any


_DEFAULT_HEADERS = {
    "User-Agent": "JobsAI/1.0 (+https://github.com/Sao-06/Job_App)",
    "Accept": "application/json, text/plain, */*",
}


def http_get_json(url: str, *, params: dict | None = None,
                  headers: dict | None = None, timeout: int = 10) -> Any:
    """Fetch JSON. Returns None on failure (network, decode, non-2xx)."""
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params, doseq=True)
    h = dict(_DEFAULT_HEADERS)
    if headers:
        h.update(headers)
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
        return json.loads(data)
    except Exception:
        return None


def http_post_json(url: str, body: Any, *, headers: dict | None = None,
                   timeout: int = 12) -> Any:
    """POST a JSON body, parse a JSON response. Returns None on any failure."""
    h = dict(_DEFAULT_HEADERS)
    h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    try:
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
        return json.loads(data)
    except Exception:
        return None


def basic_auth_header(user: str, pw: str = "") -> str:
    """Build an HTTP Basic auth header value. Reed uses (api_key, '')."""
    raw = f"{user}:{pw}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")

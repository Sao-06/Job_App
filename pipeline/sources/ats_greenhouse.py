"""
Greenhouse ATS — public board JSON, no key required.
Endpoint: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs

Per the user's "metadata-only" decision we request ``content=false`` —
the body of each posting is dropped at ingest, only labels and a short
requirements list remain.

One :class:`GreenhouseSource` instance is registered per company in
``companies.yml`` so per-company health is visible on the dev page.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterator

from .base import RawJob, is_remote_location
from .registry import register
from ._http import http_get_json


_CONFIG_PATH = Path(__file__).with_name("companies.yml")


def _load_companies() -> list[str]:
    try:
        import yaml
    except ImportError:
        return []
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return [str(x).strip() for x in (data.get("greenhouse") or []) if x]
    except Exception:
        return []


class GreenhouseSource:
    cadence_seconds = 45 * 60
    timeout_seconds = 12

    def __init__(self, slug: str):
        self.slug = slug
        self.name = f"ats:greenhouse:{slug}"

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.slug}/jobs"
        data = http_get_json(url, params={"content": "false"}, timeout=self.timeout_seconds)
        if not isinstance(data, dict):
            return iter(())
        jobs = data.get("jobs") or []
        out: list = []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            apply_url = j.get("absolute_url") or ""
            title = (j.get("title") or "").strip()
            if not (apply_url and title):
                continue
            company = (j.get("company_name") or self.slug).strip().title() if not j.get("company_name") else j["company_name"]
            offices = j.get("offices") or []
            location = (j.get("location") or {}).get("name") or ""
            if not location and offices:
                location = ", ".join(o.get("name") for o in offices if o.get("name"))
            posted = (j.get("updated_at") or j.get("first_published") or "")[:10]
            out.append(RawJob(
                application_url=apply_url,
                company=company,
                title=title,
                location=location or "United States",
                remote=is_remote_location(location),
                # We deliberately don't pull `content=true` — keep DB lean.
                description="",
                posted_date=posted,
                platform="Greenhouse",
                source=self.name,
            ))
        return iter(out)


for _slug in _load_companies():
    register(GreenhouseSource(_slug))

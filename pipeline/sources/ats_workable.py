"""
Workable ATS — public board JSON, no key required.
Endpoint: https://apply.workable.com/api/v1/widget/accounts/{slug}

Workable is dominant in Europe / international SMB. One source per
company in ``companies.yml``.
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
        return [str(x).strip() for x in (data.get("workable") or []) if x]
    except Exception:
        return []


class WorkableSource:
    cadence_seconds = 45 * 60
    timeout_seconds = 12

    def __init__(self, slug: str):
        self.slug = slug
        self.name = f"ats:workable:{slug}"

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        url = f"https://apply.workable.com/api/v1/widget/accounts/{self.slug}"
        data = http_get_json(url, timeout=self.timeout_seconds)
        if not isinstance(data, dict):
            return iter(())
        company = (data.get("name") or self.slug.replace("-", " ").title()).strip()
        jobs = data.get("jobs") or []
        out: list = []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            apply_url = j.get("application_url") or j.get("url") or ""
            title = (j.get("title") or "").strip()
            if not (apply_url and title):
                continue
            city = (j.get("city") or "").strip()
            state = (j.get("state") or "").strip()
            country = (j.get("country") or "").strip()
            parts = [p for p in (city, state, country) if p]
            location = ", ".join(parts) if parts else "Remote"
            remote = bool(j.get("telecommuting")) or is_remote_location(location)
            out.append(RawJob(
                application_url=apply_url,
                company=company,
                title=title,
                location=location,
                remote=remote,
                description="",          # workable's widget doesn't expose body
                posted_date=str(j.get("published_on") or j.get("created_at") or "")[:10],
                platform="Workable",
                source=self.name,
            ))
        return iter(out)


for _slug in _load_companies():
    register(WorkableSource(_slug))

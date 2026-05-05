"""
Ashby ATS — public board JSON, no key required.
Endpoint: https://api.ashbyhq.com/posting-api/job-board/{slug}

Used by newer-wave companies (Linear, Posthog, Ramp, Notion, OpenAI, …).
One :class:`AshbySource` per company in ``companies.yml``.

Per the metadata-only policy we don't store the description, but we do
hand it to the ingester so the inference helpers can flag exp /
education / citizenship requirements.
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
        return [str(x).strip() for x in (data.get("ashby") or []) if x]
    except Exception:
        return []


class AshbySource:
    cadence_seconds = 45 * 60
    timeout_seconds = 12

    def __init__(self, slug: str):
        self.slug = slug
        self.name = f"ats:ashby:{slug}"

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{self.slug}"
        data = http_get_json(url, params={"includeCompensation": "false"},
                              timeout=self.timeout_seconds)
        if not isinstance(data, dict):
            return iter(())
        jobs = data.get("jobs") or []
        display = self.slug.replace("-", " ").replace("_", " ").title()
        out: list = []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            apply_url = j.get("jobUrl") or j.get("applyUrl") or ""
            title = (j.get("title") or "").strip()
            if not (apply_url and title):
                continue
            location = (j.get("location") or "").strip()
            secondary = j.get("secondaryLocations") or []
            extra_locs: list = []
            if isinstance(secondary, list):
                for s in secondary[:3]:
                    if isinstance(s, dict):
                        name = s.get("locationName") or s.get("location") or ""
                    else:
                        name = str(s) if s else ""
                    name = (name or "").strip()
                    if name:
                        extra_locs.append(name)
            if extra_locs:
                tail = ", ".join(extra_locs)
                if tail and tail not in location:
                    location = (location + ", " + tail) if location else tail
            remote = bool(j.get("isRemote")) or is_remote_location(location)
            description = j.get("descriptionPlain") or ""
            out.append(RawJob(
                application_url=apply_url,
                company=display,
                title=title,
                location=location or ("Remote" if remote else "United States"),
                remote=remote,
                description=description,
                posted_date=str(j.get("publishedAt") or "")[:10],
                platform="Ashby",
                source=self.name,
            ))
        return iter(out)


for _slug in _load_companies():
    register(AshbySource(_slug))

"""
RemoteOK — public API, no key required.
Docs: https://remoteok.com/api  (returns up to ~500 active remote jobs)

The first element is metadata; the rest are jobs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from .base import RawJob
from .registry import register
from ._http import http_get_json


class RemoteOkSource:
    name = "api:remoteok"
    cadence_seconds = 25 * 60
    timeout_seconds = 12

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        data = http_get_json("https://remoteok.com/api", timeout=self.timeout_seconds)
        if not isinstance(data, list) or len(data) <= 1:
            return iter(())
        out: list = []
        for r in data[1:]:
            if not isinstance(r, dict):
                continue
            url = r.get("url") or r.get("apply_url") or ""
            company = (r.get("company") or "").strip()
            title = (r.get("position") or r.get("title") or "").strip()
            if not (url and company and title):
                continue
            tags = r.get("tags") or []
            location = r.get("location") or "Remote"
            posted = (r.get("date") or "")[:10]
            out.append(RawJob(
                application_url=url,
                company=company,
                title=title,
                location=location,
                remote=True,
                description=r.get("description") or "",
                requirements=[str(t) for t in tags][:8],
                salary_range=r.get("salary") or "Unknown",
                posted_date=posted,
                platform="RemoteOK",
                source=self.name,
            ))
        return iter(out)


register(RemoteOkSource())

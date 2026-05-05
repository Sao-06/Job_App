"""
Arbeitnow — public job-board feed, no key required.
Docs: https://www.arbeitnow.com/api/job-board-api

Pages through the entire feed (all categories, all countries) and yields
each posting. Useful for non-US remote inventory.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from .base import RawJob
from .registry import register
from ._http import http_get_json


def _norm_date(value) -> str:
    """Coerce mixed string/epoch posted-date values to ISO 'YYYY-MM-DD'."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        try:
            v = float(value)
            if v > 10_000_000_000:        # millis
                v /= 1000
            return datetime.utcfromtimestamp(v).date().isoformat()
        except Exception:
            return ""
    return str(value)[:10]


class ArbeitnowSource:
    name = "api:arbeitnow"
    cadence_seconds = 30 * 60
    timeout_seconds = 12

    MAX_PAGES = 10

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        for page in range(1, self.MAX_PAGES + 1):
            data = http_get_json(
                "https://www.arbeitnow.com/api/job-board-api",
                params={"page": page},
                timeout=self.timeout_seconds,
            )
            items = (data or {}).get("data") if isinstance(data, dict) else None
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = item.get("url") or ""
                if not url:
                    slug = item.get("slug") or ""
                    if slug:
                        url = f"https://www.arbeitnow.com/jobs/{slug}"
                if not url:
                    continue
                company = item.get("company_name") or ""
                title = item.get("title") or ""
                if not (company and title):
                    continue
                location = item.get("location") or "Remote"
                remote = bool(item.get("remote"))
                tags = item.get("tags") or []
                yield RawJob(
                    application_url=url,
                    company=company, title=title,
                    location=location, remote=remote,
                    description=item.get("description") or "",
                    requirements=[str(t) for t in tags][:8],
                    posted_date=_norm_date(item.get("created_at")),
                    platform="Arbeitnow",
                    source=self.name,
                )


register(ArbeitnowSource())

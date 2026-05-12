"""
SmartRecruiters ATS — public Posting API, no key required.
Endpoint: https://api.smartrecruiters.com/v1/companies/{slug}/postings

SmartRecruiters is heavily used by non-tech tenants (healthcare,
hospitality, QSR, retail, manufacturing, public sector) — it fills the
gap left by the more tech-skewed Greenhouse / Lever / Ashby boards.

The Posting API doesn't return a candidate-facing URL on each job; we
synthesize it from ``https://jobs.smartrecruiters.com/{slug}/{id}``,
which the public job page resolves.

Per the metadata-only policy we omit ``jobAd.sections`` / full body —
just shell metadata, like Greenhouse's ``content=false``.

One :class:`SmartRecruitersSource` instance is registered per company
in ``companies.yml`` so per-company health is visible on the dev page.
Tenants with >100 active postings are paginated up to 5 pages (500
postings) per cycle to bound load on each cron tick.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterator

from .base import RawJob, is_remote_location
from .registry import register
from ._http import http_get_json


_CONFIG_PATH = Path(__file__).with_name("companies.yml")
_PAGE_SIZE = 100
_MAX_PAGES = 5  # 500 postings per cycle ceiling


def _load_companies() -> list[str]:
    try:
        import yaml
    except ImportError:
        return []
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return [str(x).strip() for x in (data.get("smartrecruiters") or []) if x]
    except Exception:
        return []


def _job_location(loc: dict | None) -> str:
    if not isinstance(loc, dict):
        return ""
    full = (loc.get("fullLocation") or "").strip()
    if full:
        return full
    parts = [
        (loc.get("city") or "").strip(),
        (loc.get("region") or "").strip(),
        (loc.get("country") or "").strip().upper(),
    ]
    return ", ".join(p for p in parts if p)


class SmartRecruitersSource:
    cadence_seconds = 45 * 60
    timeout_seconds = 12

    def __init__(self, slug: str):
        self.slug = slug
        self.name = f"ats:smartrecruiters:{slug}"

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        url = f"https://api.smartrecruiters.com/v1/companies/{self.slug}/postings"
        out: list = []
        for page in range(_MAX_PAGES):
            offset = page * _PAGE_SIZE
            data = http_get_json(
                url,
                params={"offset": offset, "limit": _PAGE_SIZE},
                timeout=self.timeout_seconds,
            )
            if not isinstance(data, dict):
                break
            jobs = data.get("content") or []
            if not isinstance(jobs, list) or not jobs:
                break
            for j in jobs:
                if not isinstance(j, dict):
                    continue
                job_id = str(j.get("id") or "").strip()
                title = (j.get("name") or "").strip()
                if not (job_id and title):
                    continue
                company_obj = j.get("company") or {}
                if isinstance(company_obj, dict):
                    company = (company_obj.get("name") or self.slug).strip()
                    identifier = (company_obj.get("identifier") or self.slug).strip() or self.slug
                else:
                    company = str(company_obj or self.slug).strip()
                    identifier = self.slug
                apply_url = f"https://jobs.smartrecruiters.com/{identifier}/{job_id}"
                location = _job_location(j.get("location"))
                loc_obj = j.get("location") or {}
                remote_flag = bool(loc_obj.get("remote")) if isinstance(loc_obj, dict) else False
                remote = remote_flag or is_remote_location(location)
                out.append(RawJob(
                    application_url=apply_url,
                    company=company,
                    title=title,
                    location=location or ("Remote" if remote else "United States"),
                    remote=remote,
                    description="",
                    posted_date=str(j.get("releasedDate") or "")[:10],
                    platform="SmartRecruiters",
                    source=self.name,
                ))
            if len(jobs) < _PAGE_SIZE:
                break
        return iter(out)


for _slug in _load_companies():
    register(SmartRecruitersSource(_slug))

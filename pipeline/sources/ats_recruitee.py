"""
Recruitee ATS — public Careers Site JSON, no key required.
Endpoint: https://{slug}.recruitee.com/api/offers/

Recruitee (now Tellent Recruitee) is a Dutch ATS dominant in EU SMB —
manufacturing, retail, logistics, scaleups, agencies. Each tenant
exposes its published offers under the ``/api/offers/`` path with no
authentication. The endpoint returns every published offer in one shot
(no native pagination), so we trim defensively to a 500-job cap.

Per the metadata-only policy we don't keep the (often HTML) description
body — only labels, location, and the apply URL.

One :class:`RecruiteeSource` instance is registered per slug in
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

# Recruitee returns the entire offer list in one response with no native
# pagination. The cap defends against pathological huge tenants — keep the
# in-memory list bounded.
_MAX_OFFERS = 500


def _load_companies() -> list[str]:
    try:
        import yaml
    except ImportError:
        return []
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return [str(x).strip() for x in (data.get("recruitee") or []) if x]
    except Exception:
        return []


class RecruiteeSource:
    cadence_seconds = 45 * 60
    timeout_seconds = 12

    def __init__(self, slug: str):
        self.slug = slug
        self.name = f"ats:recruitee:{slug}"

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        url = f"https://{self.slug}.recruitee.com/api/offers/"
        data = http_get_json(url, timeout=self.timeout_seconds)
        if not isinstance(data, dict):
            return iter(())
        offers = data.get("offers") or []
        if not isinstance(offers, list):
            return iter(())
        offers = offers[:_MAX_OFFERS]

        display_default = self.slug.replace("-", " ").replace("_", " ").title()
        out: list = []
        for j in offers:
            if not isinstance(j, dict):
                continue
            apply_url = (j.get("careers_apply_url") or j.get("careers_url") or "").strip()
            title = (j.get("title") or "").strip()
            if not (apply_url and title):
                continue
            company = (j.get("company_name") or "").strip() or display_default
            # Recruitee splits city/country/state into separate fields and
            # also exposes a pre-joined ``location`` string. Prefer the
            # joined one and fall back to assembling the parts.
            location = (j.get("location") or "").strip()
            if not location:
                parts = [
                    str(j.get("city") or "").strip(),
                    str(j.get("state_name") or j.get("state_code") or "").strip(),
                    str(j.get("country") or j.get("country_code") or "").strip(),
                ]
                location = ", ".join(p for p in parts if p)
            api_remote = bool(j.get("remote"))
            remote = api_remote or is_remote_location(location)
            posted = str(j.get("published_at") or j.get("created_at") or "")[:10]
            out.append(RawJob(
                application_url=apply_url,
                company=company,
                title=title,
                location=location or ("Remote" if remote else ""),
                remote=remote,
                description="",
                posted_date=posted,
                platform="Recruitee",
                source=self.name,
            ))
        return iter(out)


for _slug in _load_companies():
    register(RecruiteeSource(_slug))

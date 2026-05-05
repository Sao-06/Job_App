"""
Lever ATS — public board JSON, no key required.
Endpoint: https://api.lever.co/v0/postings/{slug}?mode=json

One :class:`LeverSource` instance is registered per company in
``companies.yml`` so per-company health is visible on the dev page.
Lever returns a full posting body in `descriptionPlain`; we drop it at
ingest (metadata-only policy) but use it for the experience-level
inference upstream.
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
        return [str(x).strip() for x in (data.get("lever") or []) if x]
    except Exception:
        return []


def _ms_to_iso(ms) -> str:
    """Lever timestamps are JS-style ms-since-epoch ints."""
    if not ms:
        return ""
    try:
        v = float(ms)
        if v > 10_000_000_000:
            v /= 1000
        return datetime.utcfromtimestamp(v).date().isoformat()
    except Exception:
        return ""


class LeverSource:
    cadence_seconds = 45 * 60
    timeout_seconds = 12

    def __init__(self, slug: str):
        self.slug = slug
        self.name = f"ats:lever:{slug}"

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        url = f"https://api.lever.co/v0/postings/{self.slug}"
        data = http_get_json(url, params={"mode": "json"},
                              timeout=self.timeout_seconds)
        if not isinstance(data, list):
            return iter(())
        # Pretty up the slug for display: "scale-ai" → "Scale AI"
        display = self.slug.replace("-", " ").replace("_", " ").title()
        out: list = []
        for j in data:
            if not isinstance(j, dict):
                continue
            apply_url = j.get("hostedUrl") or j.get("applyUrl") or ""
            title = (j.get("text") or "").strip()
            if not (apply_url and title):
                continue
            cats = j.get("categories") or {}
            location = (cats.get("location") or "").strip()
            if not location:
                all_locs = cats.get("allLocations") or []
                if isinstance(all_locs, list) and all_locs:
                    location = ", ".join(str(l) for l in all_locs[:3])
            workplace = (j.get("workplaceType") or "").lower()
            remote = workplace == "remote" or is_remote_location(location)
            # Lever's `descriptionPlain` is the JD body. We hand it to the
            # ingester so inference picks up exp/edu/citizenship signals,
            # but we don't store it (job_repo schema drops `description`).
            description = j.get("descriptionPlain") or ""
            out.append(RawJob(
                application_url=apply_url,
                company=display,
                title=title,
                location=location or "Remote" if remote else (location or ""),
                remote=remote,
                description=description,
                posted_date=_ms_to_iso(j.get("createdAt")),
                platform="Lever",
                source=self.name,
            ))
        return iter(out)


for _slug in _load_companies():
    register(LeverSource(_slug))

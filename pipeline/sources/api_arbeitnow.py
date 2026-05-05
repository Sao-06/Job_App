"""
Arbeitnow — public job-board feed, no key required.
Docs: https://www.arbeitnow.com/api/job-board-api

Pages the global feed (all categories, all countries) and yields each
posting that looks English-relevant for a US-focused job board. The
upstream is German-speaking-DACH-heavy, so we filter aggressively:

  * tags must contain "english" OR location/title must look ASCII / English.
  * obvious German signal words (m/w/d, Mitarbeiter, Kaufmann, Fachkraft…)
    skip the row.

This keeps us from polluting page 1 with hundreds of German listings that
nobody using the SPA can apply to.
"""

from __future__ import annotations

import re
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


# Heuristic English-only filter. Cheap (no language model) but surprisingly
# effective on Arbeitnow's data. We accept the row when ANY of:
#   * upstream tags include 'english' / 'english speaking'
#   * title is plain ASCII and contains no German signal words
# We reject when title contains the m/w/d gender hints or German-specific
# nouns that strongly imply a German-language posting.
_GERMAN_HINTS_RE = re.compile(
    r"(?:"
    r"\bm\s*/\s*w\s*/\s*[dx]\b"          # m/w/d, m/w/x
    r"|\bmitarbeiter(?:in|innen)?\b"
    r"|\bkaufmann\b|\bkauffrau\b"
    r"|\bfachkraft\b|\bfachinformatiker\b|\bsachbearbeiter(?:in)?\b"
    r"|\bfür\b|\bmit\b\s+\w+\b\s+(?:in|im|am)\b"
    r"|\bgesucht\b|\bberater(?:in)?\b"
    r"|\bingenieur(?:in)?\b|\bentwickler(?:in)?\b"
    r"|\bsenior(?:e|in|innen)\b"
    r"|\bgmbh\b|\bag\b"
    r"|werkstudent|praktikum|ausbildung|festanstellung"
    r"|\bteilzeit\b|\bvollzeit\b"
    r")",
    re.IGNORECASE,
)


def _english_relevant(title: str, tags: list, location: str) -> bool:
    if not title:
        return False
    tag_blob = " ".join(str(t).lower() for t in (tags or []))
    if "english" in tag_blob:
        return True
    # Reject obvious German signals.
    if _GERMAN_HINTS_RE.search(title):
        return False
    if _GERMAN_HINTS_RE.search(location or ""):
        return False
    # Plain ASCII titles with mostly word characters → assume English.
    try:
        title.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


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
                if not _english_relevant(title, tags, location):
                    continue
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

"""
We Work Remotely — public RSS, no key required.

Docs: https://weworkremotely.com/categories
Endpoints: https://weworkremotely.com/categories/remote-{slug}-jobs.rss

WWR is the largest curated remote-job board globally — postings span
every major timezone and every category they classify (programming,
design, marketing, sales, customer support, devops/sysadmin, product,
copywriting, business/management, all-other-remote-jobs). Unlike
Jobicy/Himalayas which lean US-remote, WWR's catalog is genuinely
international across the EU, UK, APAC, and LatAm — adding it materially
broadens the index's geographic coverage.

The feed is RSS so we use stdlib xml.etree to avoid pulling feedparser
just for one source. RSS items expose: title, link, pubDate, description
(HTML — stripped to plain text). Company name lives at the start of the
title before a colon, in the WWR-canonical format
``Company Name: Job Title``.
"""

from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Iterator

from .base import RawJob
from .registry import register


# Lookup of WWR category slugs → human-readable platform tags. Each
# slug becomes one HTTP fetch per cycle; small enough that we fan out
# across every major category instead of rotating.
_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("remote-programming-jobs",          "Programming"),
    ("remote-design-jobs",               "Design"),
    ("remote-devops-sysadmin-jobs",      "DevOps / SysAdmin"),
    ("remote-customer-support-jobs",     "Customer Support"),
    ("remote-sales-and-marketing-jobs",  "Sales / Marketing"),
    ("remote-product-jobs",              "Product"),
    ("remote-copywriting-jobs",          "Writing"),
    ("remote-business-exec-management-jobs", "Business / Mgmt"),
    ("all-other-remote-jobs",            "Other"),
)

_HEADERS = {
    "User-Agent": "JobsAI/1.0 (+https://github.com/Sao-06/Job_App)",
    "Accept": "application/rss+xml, text/xml, */*",
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = _HTML_TAG_RE.sub(" ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&nbsp;", " ")
                .replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def _split_company_title(rss_title: str) -> tuple[str, str]:
    """WWR titles are ``Company Name: Job Title``. Some posters break the
    convention and use a hyphen; handle both. Returns (company, title)."""
    if not rss_title:
        return "", ""
    for sep in (": ", " – ", " — ", " - "):
        if sep in rss_title:
            company, _, title = rss_title.partition(sep)
            company = company.strip()
            title = title.strip()
            if company and title:
                return company, title
    # Couldn't split — treat the whole string as the title; company stays
    # blank and the row will be dropped upstream by the company-required
    # check in job_repo.upsert_many.
    return "", rss_title.strip()


def _iso_date(rfc822: str) -> str:
    if not rfc822:
        return ""
    try:
        return parsedate_to_datetime(rfc822).date().isoformat()
    except (TypeError, ValueError):
        return ""


class WeWorkRemotelySource:
    name = "api:weworkremotely"
    cadence_seconds = 30 * 60
    timeout_seconds = 15

    def fetch(self, since: datetime | None) -> Iterator[RawJob]:
        seen: set[str] = set()
        for slug, platform_label in _CATEGORIES:
            url = f"https://weworkremotely.com/categories/{slug}.rss"
            try:
                req = urllib.request.Request(url, headers=_HEADERS)
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    raw = resp.read()
            except Exception:
                continue
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                continue
            # RSS structure: <rss><channel><item>…</item></channel></rss>
            for item in root.iter("item"):
                link = (item.findtext("link") or "").strip()
                if not link or link in seen:
                    continue
                seen.add(link)
                rss_title = (item.findtext("title") or "").strip()
                company, title = _split_company_title(rss_title)
                if not (company and title):
                    continue
                description = _strip_html(item.findtext("description") or "")
                # WWR doesn't expose a structured location field per item;
                # the upstream board treats every posting as fully remote.
                yield RawJob(
                    application_url=link,
                    company=company,
                    title=title,
                    location="Remote",
                    remote=True,
                    description=description[:1500],
                    posted_date=_iso_date(item.findtext("pubDate") or ""),
                    platform=f"WeWorkRemotely · {platform_label}",
                    source=self.name,
                )


register(WeWorkRemotelySource())

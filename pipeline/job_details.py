"""
pipeline/job_details.py
───────────────────────
On-demand fetcher for the FULL job posting + parsed sections + a
company summary from Wikipedia.

This is the source of truth for ``GET /api/jobs/{id}/details``. Index
ingest stays metadata-only (descriptions can run 5-15 KB each, and
storing them would balloon the SQLite working set well past comfort);
instead we re-fetch from the upstream source's per-job API on demand
and cache for 1 h. Repeated views of the same posting hit the in-memory
cache, never the upstream.

Three layers:

* ``fetch_full_description(canonical_url, source)`` — per-source
  description fetcher. Knows how to read greenhouse / lever / ashby /
  workable per-job APIs from the canonical URL + the source slug. Other
  sources (RemoteOK / Adzuna / Jobicy / etc.) return None — the SPA
  renders a "view original posting" prompt for those.

* ``parse_sections(text)`` — splits the description into ``lead`` /
  ``responsibilities`` / ``required`` / ``preferred`` / ``benefits``
  using a header lexicon tuned to real job postings. Robust to varied
  bullet markers and missing sections.

* ``fetch_company_info(name)`` — Wikipedia REST summary lookup. Tries
  the raw name, then ``"<name> (company)"``, then with the legal suffix
  (Inc / LLC / Corp / etc.) stripped.

Entry point ``get_job_details(...)`` glues all three together and
returns a single payload the SPA renders against.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional
from urllib.parse import quote

from .sources._http import http_get_json


# ══════════════════════════════════════════════════════════════════════
# In-memory caches
# ══════════════════════════════════════════════════════════════════════

_DETAILS_CACHE_LOCK = threading.Lock()
_DETAILS_CACHE: dict[str, tuple[float, dict]] = {}
_DETAILS_CACHE_TTL = 3600.0    # 1 h
_DETAILS_CACHE_MAX = 500

_COMPANY_CACHE_LOCK = threading.Lock()
_COMPANY_CACHE: dict[str, tuple[float, dict]] = {}
_COMPANY_CACHE_TTL = 86400.0   # 24 h — company info changes far less often
_COMPANY_CACHE_MAX = 500


def _cached(cache: dict, lock: threading.Lock, key: str, ttl: float):
    now = time.time()
    with lock:
        entry = cache.get(key)
        if entry and (now - entry[0]) < ttl:
            return entry[1]
    return None


def _cache_set(cache: dict, lock: threading.Lock, key: str,
                value, max_size: int) -> None:
    now = time.time()
    with lock:
        if len(cache) >= max_size:
            # Evict oldest 10% to amortize the cost.
            victims = sorted(cache.items(), key=lambda kv: kv[1][0])[: max(10, max_size // 10)]
            for k, _ in victims:
                cache.pop(k, None)
        cache[key] = (now, value)


# ══════════════════════════════════════════════════════════════════════
# HTML stripper — keeps bullet structure while flattening tags
# ══════════════════════════════════════════════════════════════════════

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTS = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": " ",
    "&#39;": "'", "&quot;": '"', "&rsquo;": "'", "&lsquo;": "'",
    "&rdquo;": '"', "&ldquo;": '"', "&mdash;": "—", "&ndash;": "–",
    "&apos;": "'", "&hellip;": "…",
}


def strip_html(html: str) -> str:
    """Convert HTML to a normalized plain-text blob with bullets preserved.

    Block-level tags become newlines; ``<li>`` becomes a leading bullet
    so the section parser downstream still recognises list items.
    """
    if not html:
        return ""
    text = html
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|h[1-6]|tr|table|ul|ol)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "\n• ", text, flags=re.IGNORECASE)
    text = re.sub(r"<h[1-6][^>]*>", "\n\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub("", text)
    for ent, ch in _HTML_ENTS.items():
        text = text.replace(ent, ch)
    text = re.sub(r"&#\d+;", "", text)            # numeric entities
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


# ══════════════════════════════════════════════════════════════════════
# Section parser — buckets lines into responsibilities / required / etc.
# ══════════════════════════════════════════════════════════════════════

# Order matters — more-specific phrases first so "preferred qualifications"
# doesn't match the "qualifications" bucket before reaching the "preferred"
# one. Each phrase is a regex anchored at fullmatch.
_SECTION_PATTERNS: list[tuple[str, list[str]]] = [
    ("preferred", [
        r"preferred\s+qualifications?",
        r"nice[\s-]to[\s-]have(?:s)?",
        r"preferred\s+skills?",
        r"bonus(?:\s+points)?",
        r"good[\s-]to[\s-]have",
        r"additional\s+qualifications?",
        r"preferred\s+experience",
        r"preferred",
        r"plus(?:es)?",
    ]),
    ("required", [
        r"required\s+qualifications?",
        r"basic\s+qualifications?",
        r"minimum\s+qualifications?",
        r"requirements?",
        r"must[\s-]have(?:s)?",
        r"required\s+skills?",
        r"qualifications?",
        r"what\s+you'?(?:ll)?\s+(?:bring|need)",
        r"who\s+you\s+are",
        r"about\s+you",
        r"required",
        r"you'?(?:ll)?\s+(?:have|should\s+have)",
        r"the\s+ideal\s+candidate",
        r"skills?\s+(?:and|&)\s+experience",
    ]),
    ("responsibilities", [
        r"responsibilities",
        r"key\s+responsibilities",
        r"primary\s+responsibilities",
        r"what\s+you'?(?:ll)?\s+(?:do|be\s+doing)",
        r"the\s+role",
        r"your\s+role",
        r"duties",
        r"day[\s-]to[\s-]day",
        r"in\s+this\s+role",
        r"you'?(?:ll)?\s+(?:will)?\s*(?:be\s+)?",
        r"what\s+you'?ll\s+work\s+on",
        r"your\s+impact",
        r"role\s+overview",
    ]),
    ("benefits", [
        r"benefits?",
        r"what\s+we\s+offer",
        r"perks(?:\s+(?:and|&)\s+benefits)?",
        r"compensation\s+(?:and|&)\s+benefits",
        r"why\s+(?:join|work\s+(?:with|at))(?:\s+us)?",
        r"we\s+offer",
        r"what\s+you'?(?:ll)?\s+get",
    ]),
]


def _classify_header(line: str) -> Optional[str]:
    """Return section name if `line` looks like a section header.

    Headers tend to be short, often Title-Case or ALL-CAPS, and may end
    with ":". Returns None for prose so the parser keeps them in the
    current section.
    """
    s = line.strip()
    if not s or len(s) > 70:
        return None
    cleaned = re.sub(r"[\s:_\-=•.*]+$", "", s).strip()
    if not cleaned:
        return None
    cleaned = re.sub(r"^[\s:_\-=•.*]+", "", cleaned).strip()
    if not cleaned:
        return None

    is_caps = cleaned == cleaned.upper() and any(c.isalpha() for c in cleaned)
    words = cleaned.split()
    is_title = (
        len(words) <= 6
        and sum(1 for w in words if w[:1].isupper()) >= max(1, len(words) - 1)
    )
    if not (is_caps or is_title or len(cleaned) <= 30):
        return None

    low = cleaned.lower()
    for section, patterns in _SECTION_PATTERNS:
        for pat in patterns:
            if re.fullmatch(pat, low):
                return section
    return None


_BULLET_RE = re.compile(r"^[\s]*[•\-*–·▪◦►☆✓→][\s]+(.+)$")
_NUM_BULLET_RE = re.compile(r"^[\s]*\d+[.)][\s]+(.+)$")


def _is_bullet(raw: str) -> Optional[str]:
    m = _BULLET_RE.match(raw) or _NUM_BULLET_RE.match(raw)
    return m.group(1).strip() if m else None


def parse_sections(text: str) -> dict:
    """Bucket lines into ``lead`` / ``responsibilities`` / ``required`` /
    ``preferred`` / ``benefits``. Robust to varied bullet styles and
    missing sections; if the parser can't find any explicit bucket the
    full description goes into ``lead``.
    """
    empty = {"lead": "", "responsibilities": [], "required": [],
             "preferred": [], "benefits": []}
    if not text:
        return empty

    lines = [l.rstrip() for l in text.splitlines()]
    sections = {"lead": [], "responsibilities": [], "required": [],
                "preferred": [], "benefits": []}
    current = "lead"
    saw_section_header = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        sec = _classify_header(line)
        if sec is not None:
            current = sec
            saw_section_header = True
            continue
        body = _is_bullet(raw) or line
        if current == "lead":
            sections["lead"].append(body)
        else:
            # Skill / responsibility lines are usually bulleted or short.
            # Long paragraphs in those sections are typically intro prose
            # that we drop (they break the visual rhythm of the bulleted
            # list). 240 chars is generous enough to keep most useful
            # multi-clause requirements.
            is_bullet = bool(_is_bullet(raw))
            if is_bullet or len(body) < 240:
                sections[current].append(body)

    # If we never saw a recognised section header, surface the full text
    # as lead so the SPA can at least render a paragraph rather than an
    # empty pane.
    if not saw_section_header:
        sections["lead"] = lines[:60]

    lead_joined = " ".join(sections["lead"][:8]).strip()
    if len(lead_joined) > 800:
        lead_joined = lead_joined[:800].rsplit(" ", 1)[0] + "…"

    return {
        "lead":             lead_joined,
        "responsibilities": [b for b in sections["responsibilities"] if b][:14],
        "required":         [b for b in sections["required"] if b][:16],
        "preferred":        [b for b in sections["preferred"] if b][:12],
        "benefits":         [b for b in sections["benefits"] if b][:10],
    }


# ══════════════════════════════════════════════════════════════════════
# Per-source description fetchers
# ══════════════════════════════════════════════════════════════════════

def _fetch_greenhouse(canonical_url: str, slug: str) -> Optional[dict]:
    """Greenhouse: per-job endpoint with `content=true` returns the full
    HTML body. URL pattern: ``boards.greenhouse.io/{slug}/jobs/{numeric_id}``.
    """
    m = re.search(r"/jobs/(\d+)", canonical_url)
    if not m:
        return None
    job_id = m.group(1)
    data = http_get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}",
        params={"content": "true"},
        timeout=10,
    )
    if not data or not isinstance(data, dict):
        return None
    text = strip_html(data.get("content", ""))
    return {"description": text} if text else None


def _fetch_lever(canonical_url: str, slug: str) -> Optional[dict]:
    """Lever: per-posting endpoint returns ``descriptionPlain`` plus a
    ``lists`` array of named bullet sections. URL pattern:
    ``jobs.lever.co/{slug}/{posting_uuid}``.
    """
    m = re.search(r"/lever\.co/[^/]+/([0-9a-f-]{36})", canonical_url, re.IGNORECASE)
    if not m:
        return None
    posting_id = m.group(1)
    data = http_get_json(
        f"https://api.lever.co/v0/postings/{slug}/{posting_id}",
        params={"mode": "json"},
        timeout=10,
    )
    if not data or not isinstance(data, dict):
        return None

    blocks = []
    desc = (data.get("descriptionPlain") or "").strip()
    if desc:
        blocks.append(desc)
    # Lever exposes structured bullet groups as ``lists`` — the visible
    # subheaders inside the Lever posting (Responsibilities, Requirements,
    # ...). Their text is HTML so strip it before joining.
    for lst in (data.get("lists") or []):
        name = (lst.get("text") or "").strip()
        body = strip_html(lst.get("content") or "").strip()
        if not body:
            continue
        if name:
            blocks.append(f"\n\n{name}\n{body}")
        else:
            blocks.append(f"\n\n{body}")
    full_text = "\n".join(blocks).strip()
    return {"description": full_text} if full_text else None


def _fetch_ashby(canonical_url: str, slug: str) -> Optional[dict]:
    """Ashby: no per-job public API; refetch the board and look up by URL.

    The board endpoint is small (100-700 KB), and we cache the result, so
    the cost is bounded.
    """
    data = http_get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        params={"includeCompensation": "false"},
        timeout=12,
    )
    if not data or not isinstance(data, dict):
        return None
    target = canonical_url.rstrip("/")
    for j in (data.get("jobs") or []):
        if not isinstance(j, dict):
            continue
        url = (j.get("jobUrl") or j.get("applyUrl") or "").rstrip("/")
        if url == target:
            text = (j.get("descriptionPlain") or "").strip()
            if not text:
                text = strip_html(j.get("descriptionHtml") or "")
            return {"description": text} if text else None
    return None


def _fetch_workable(canonical_url: str, slug: str) -> Optional[dict]:
    """Workable: refetch the board widget and look up by URL.

    Same cost reasoning as Ashby — bounded payload, cached.
    """
    data = http_get_json(
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
        timeout=12,
    )
    if not data or not isinstance(data, dict):
        return None
    target = canonical_url.rstrip("/")
    for j in (data.get("jobs") or []):
        if not isinstance(j, dict):
            continue
        url = (j.get("application_url") or j.get("url") or "").rstrip("/")
        if url == target:
            text = strip_html(j.get("description") or j.get("full_description") or "")
            return {"description": text} if text else None
    return None


def fetch_full_description(canonical_url: str, source: str) -> Optional[dict]:
    """Re-fetch the full description from the upstream source's per-job
    API. Returns ``{"description": text}`` or ``None`` when the source
    isn't supported / fetch fails.
    """
    if not canonical_url or not source:
        return None
    parts = source.split(":")
    if len(parts) < 3:
        return None
    provider, slug = parts[1], parts[2]
    try:
        if provider == "greenhouse":
            return _fetch_greenhouse(canonical_url, slug)
        if provider == "lever":
            return _fetch_lever(canonical_url, slug)
        if provider == "ashby":
            return _fetch_ashby(canonical_url, slug)
        if provider == "workable":
            return _fetch_workable(canonical_url, slug)
    except Exception:
        return None
    return None


# ══════════════════════════════════════════════════════════════════════
# Wikipedia company info
# ══════════════════════════════════════════════════════════════════════

_LEGAL_SUFFIX_RE = re.compile(
    r"[,.]?\s*(?:Inc|LLC|Ltd|Limited|Corp|Corporation|Co|Company|GmbH|"
    r"S\.?A\.?|PLC|Holdings|Group|Labs?|Technologies|Technology)\.?\s*$",
    re.IGNORECASE,
)


def _wiki_query(name: str) -> Optional[dict]:
    """Hit the Wikipedia REST summary endpoint. Returns a normalized
    dict or ``None`` for disambiguation / missing pages.
    """
    if not name:
        return None
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(name)}"
    data = http_get_json(url, timeout=8)
    if not isinstance(data, dict):
        return None
    if data.get("type") != "standard":
        # disambiguation, missing, no-extract — skip
        return None
    extract = (data.get("extract") or "").strip()
    if not extract:
        return None
    return {
        "summary":     extract,
        "image":       (data.get("thumbnail") or {}).get("source") or "",
        "title":       data.get("title", name),
        "wiki_url":    ((data.get("content_urls") or {}).get("desktop") or {}).get("page", ""),
        "description": (data.get("description") or "").strip(),
    }


def fetch_company_info(name: str) -> dict:
    """Best-effort company info from Wikipedia. Returns ``{}`` on miss.

    Tries the raw company name, then ``"<name> (company)"``, then with
    the legal suffix stripped — Wikipedia titles aren't consistent.
    Cached for 24 h per company.
    """
    if not name:
        return {}
    cache_key = name.strip().lower()
    cached = _cached(_COMPANY_CACHE, _COMPANY_CACHE_LOCK, cache_key, _COMPANY_CACHE_TTL)
    if cached is not None:
        return cached

    candidates = [
        name,
        name + " (company)",
        _LEGAL_SUFFIX_RE.sub("", name).strip(),
    ]
    out: dict = {}
    seen: set[str] = set()
    for c in candidates:
        c = c.strip()
        if not c or c.lower() in seen:
            continue
        seen.add(c.lower())
        result = _wiki_query(c)
        if result:
            out = result
            break

    _cache_set(_COMPANY_CACHE, _COMPANY_CACHE_LOCK, cache_key, out, _COMPANY_CACHE_MAX)
    return out


# ══════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════

def get_job_details(job_id: str, canonical_url: str, source: str,
                     company: str) -> dict:
    """Single function the SPA endpoint calls. Composes description,
    parsed sections, and Wikipedia-derived company info.

    Returns:
        {
            description, lead_paragraph,
            responsibilities[], required_qualifications[],
            preferred_qualifications[], benefits[],
            has_description, fetched_at,

            company_summary, company_short_description,
            company_image, company_wiki_url,
        }
    """
    cached = _cached(_DETAILS_CACHE, _DETAILS_CACHE_LOCK, job_id, _DETAILS_CACHE_TTL)
    if cached is None:
        result = fetch_full_description(canonical_url, source)
        desc_text = (result or {}).get("description", "") or ""
        if desc_text:
            sections = parse_sections(desc_text)
        else:
            sections = {"lead": "", "responsibilities": [], "required": [],
                        "preferred": [], "benefits": []}
        # Truncate the raw description for the wire payload — the SPA
        # already gets the parsed sections; the full text is fallback only.
        payload = {
            "description":              desc_text[:8000],
            "lead_paragraph":           sections["lead"],
            "responsibilities":         sections["responsibilities"],
            "required_qualifications":  sections["required"],
            "preferred_qualifications": sections["preferred"],
            "benefits":                 sections["benefits"],
            "has_description":          bool(desc_text),
            "fetched_at":               time.time(),
        }
        _cache_set(_DETAILS_CACHE, _DETAILS_CACHE_LOCK, job_id, payload, _DETAILS_CACHE_MAX)
    else:
        payload = dict(cached)

    company_info = fetch_company_info(company) if company else {}
    payload.update({
        "company_summary":           company_info.get("summary", ""),
        "company_short_description": company_info.get("description", ""),
        "company_image":             company_info.get("image", ""),
        "company_wiki_url":          company_info.get("wiki_url", ""),
    })
    return payload

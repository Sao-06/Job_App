"""
pipeline/job_details.py
───────────────────────
On-demand fetcher for the FULL job posting + parsed sections + a
company summary from Wikipedia.

This is the source of truth for ``GET /api/jobs/{id}/details`` and the
back-end of Phase 3's ``_ensure_description`` lazy-fetch. ATS detection
runs URL-first (so a ``gh:simplify/new-grad`` row pointing at
``boards.greenhouse.io/spacex/jobs/123`` still hits the Greenhouse
endpoint) and falls back to the ``source`` field's prefix only when
the URL doesn't match a known ATS host.

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
# URL-based ATS detection — works for any source whose apply_url points
# at a known ATS host, not just rows whose `source` field matches.
# ══════════════════════════════════════════════════════════════════════

# Each tuple is (provider_name, host_regex, path_regex_with_named_groups).
# `host_regex` is `re.search`-tested against the URL's netloc; `path_regex`
# pulls out whatever per-provider identifiers the fetcher needs (slug,
# job_id, host shard, site, ...). Order matters: the first hit wins.
_ATS_URL_PATTERNS: list[tuple[str, str, str]] = [
    # https://boards.greenhouse.io/{slug}/jobs/{numeric_id}
    ("greenhouse",
     r"(?:^|\.)(?:boards|job-boards)\.greenhouse\.io$",
     r"^/(?P<slug>[\w\-]+)/jobs/(?P<job_id>\d+)"),
    # https://jobs.lever.co/{slug}/{posting_uuid}
    ("lever",
     r"(?:^|\.)jobs\.lever\.co$",
     r"^/(?P<slug>[\w\-]+)/(?P<job_id>[0-9a-f-]{36})"),
    # https://jobs.ashbyhq.com/{slug}/{posting_uuid}
    ("ashby",
     r"(?:^|\.)jobs\.ashbyhq\.com$",
     r"^/(?P<slug>[\w\-]+)(?:/(?P<job_id>[\w\-]+))?"),
    # https://apply.workable.com/{slug}/j/{id} (legacy) or /{slug}/...
    ("workable",
     r"(?:^|\.)apply\.workable\.com$",
     r"^/(?P<slug>[\w\-]+)"),
    # https://{slug}.{wdN}.myworkdayjobs.com/{site}/job/...
    ("workday",
     r"^(?P<slug>[\w\-]+)\.(?P<host>wd\d+)\.myworkdayjobs\.com$",
     r"^/(?P<site>[\w\-]+)/job/.+"),
    # https://jobs.smartrecruiters.com/{slug}/{job_id}
    ("smartrecruiters",
     r"(?:^|\.)jobs\.smartrecruiters\.com$",
     r"^/(?P<slug>[\w\-]+)/(?P<job_id>\d+)"),
]


def _detect_ats_from_url(url: str) -> Optional[dict]:
    """Identify the ATS + extract its routing parameters from any apply URL.

    Returns ``{"provider": str, ...captured groups...}`` or ``None``. This
    is the primary routing path for ``fetch_full_description`` — falling
    back to the ``source`` field's prefix only when no URL pattern matches
    (legacy callers, demo jobs, hand-curated lists, etc.).
    """
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        parts = urlparse(url)
    except Exception:
        return None
    netloc = (parts.netloc or "").lower()
    path = parts.path or ""
    for provider, host_re, path_re in _ATS_URL_PATTERNS:
        if not re.search(host_re, netloc):
            continue
        m = re.search(path_re, path)
        if not m:
            # Workday: capture host even when the path lacks /job/... so the
            # caller can build the per-job endpoint from later URL components.
            if provider == "workday":
                m_host = re.search(host_re, netloc)
                if m_host:
                    return {"provider": provider, **m_host.groupdict()}
            continue
        # For Workday we need both host (from netloc) and site (from path).
        out = {"provider": provider, **m.groupdict()}
        if provider == "workday":
            m_host = re.search(host_re, netloc)
            if m_host:
                out.update(m_host.groupdict())
        return out
    return None


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
    Entities are decoded via stdlib ``html.unescape`` (covers every named +
    numeric entity, not just the dozen we used to handle by hand — fixes
    leaks like ``&times;`` / ``&bull;`` showing up raw in cleaned output).
    Decode runs BEFORE tag stripping so double-encoded payloads (e.g.
    Greenhouse's ``content`` field returning ``&lt;div&gt;``) unescape to
    real tags before the tag regex runs — otherwise literal ``<div>``
    survives.
    """
    if not html:
        return ""
    import html as _html_mod
    text = _html_mod.unescape(html)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|h[1-6]|tr|table|ul|ol)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "\n• ", text, flags=re.IGNORECASE)
    text = re.sub(r"<h[1-6][^>]*>", "\n\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub("", text)
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


def _fetch_workday(canonical_url: str, slug: str, host: str, site: str) -> Optional[dict]:
    """Workday: per-job GET against ``/wday/cxs/{slug}/{site}/job/{externalPath}``.

    The externalPath is the trailing portion of the canonical URL after
    ``/{site}``. Workday's response carries ``jobPostingInfo.jobDescription``
    as HTML — strip it before returning.
    """
    if not (slug and host and site):
        return None
    # Path layout: /{site}/job/.../R-12345 — capture everything after /{site}/.
    m = re.match(rf"^/{re.escape(site)}(/job/.+?)$", canonical_url.split(host + ".myworkdayjobs.com", 1)[-1])
    if not m:
        # Defensive — try a looser match if the simple split missed.
        path_match = re.search(r"(/job/[^?#]+)", canonical_url)
        if not path_match:
            return None
        external_path = path_match.group(1)
    else:
        external_path = m.group(1)

    api = f"https://{slug}.{host}.myworkdayjobs.com/wday/cxs/{slug}/{site}/job{external_path[len('/job'):]}"
    data = http_get_json(api, timeout=12)
    if not isinstance(data, dict):
        return None
    info = data.get("jobPostingInfo") or {}
    raw = info.get("jobDescription") or info.get("description") or ""
    text = strip_html(raw)
    return {"description": text} if text else None


def _fetch_smartrecruiters(canonical_url: str, slug: str, job_id: str) -> Optional[dict]:
    """SmartRecruiters: public Posting API. The endpoint
    ``/v1/companies/{slug}/postings/{job_id}`` returns ``jobAd.sections``
    with separate ``companyDescription`` / ``jobDescription`` /
    ``qualifications`` / ``additionalInformation`` keys (each ``text`` is
    HTML). Concatenated and HTML-stripped here.
    """
    if not (slug and job_id):
        return None
    api = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}"
    data = http_get_json(api, timeout=12)
    if not isinstance(data, dict):
        return None
    sections = (data.get("jobAd") or {}).get("sections") or {}
    blocks: list[str] = []
    for key in ("jobDescription", "qualifications", "additionalInformation",
                "companyDescription"):
        section = sections.get(key) or {}
        title = (section.get("title") or "").strip()
        body = strip_html(section.get("text") or "")
        if not body:
            continue
        if title:
            blocks.append(f"\n{title}\n{body}")
        else:
            blocks.append(body)
    text = "\n\n".join(blocks).strip()
    return {"description": text} if text else None


# Generic HTML fallback — last-ditch fetch for apply URLs that don't match
# any known ATS pattern (company career pages, regional job boards, etc.).
# We do a single GET, cap the response, and strip HTML. Most JS-rendered
# SPAs return a thin shell here, which is fine — caller treats empty as
# "no description" and falls back to title-only scoring.

_GENERIC_FETCH_TIMEOUT = 10
_GENERIC_FETCH_MAX_BYTES = 800_000  # 800 KB — big enough for any real posting
_GENERIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; JobsAI/1.0; +https://github.com/Sao-06/Job_App)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    # Request uncompressed content. Most servers honor this; we still
    # decode gzip/deflate fallbacks below for the ones that don't.
    "Accept-Encoding": "identity",
}


# Hosts that aggregate other boards. Their public detail pages are
# wrapped in newsletter signups, "looking for more?" CTAs, and copyright
# footers that bleed into the body — generic page-scraping produces
# garbage for them. The right path for these is the upstream API, which
# the per-source connectors handle at ingest time. If a row from one of
# these makes it to the lazy fetcher with no description, we'd rather
# return ``None`` (honest: title-only score, surface "preview score"
# badge) than ship the user newsletter chrome.
_AGGREGATOR_HOSTS = frozenset({
    "findwork.dev", "simplifyjobs.com", "jobright.ai", "speedyapply.com",
    "wellfound.com", "angel.co", "linkedin.com", "indeed.com",
    "glassdoor.com", "ziprecruiter.com", "monster.com", "dice.com",
    "themuse.com", "remoteok.com", "remoteok.io", "weworkremotely.com",
    "jobicy.com", "himalayas.app", "remotive.com", "arbeitnow.com",
    "github.com", "raw.githubusercontent.com",
})

# Class/id substrings on container elements that almost always carry
# page chrome rather than job-description content. Dropped before the
# stripper runs.
_CHROME_CLASS_RE = re.compile(
    r'<(?P<tag>div|section|aside|footer|nav|form|p|ul)\b[^>]*'
    r'(?:class|id)\s*=\s*["\'][^"\']*'
    r'(?:newsletter|sign[-_]?up|signup|subscribe|cta\b|cookie|gdpr|consent|'
    r'footer|copyright|related[-_]?(?:jobs|posts)|sidebar|breadcrumb|share[-_]?'
    r'(?:bar|buttons)?|social|comments?|disclaimer|legal|nav[-_]?bar|menu|'
    r'pagination|recommended|promo|banner|popup|modal)'
    r'[^"\']*["\'][^>]*>.*?</(?P=tag)>',
    flags=re.DOTALL | re.IGNORECASE,
)

# Sectioning tags that should never carry job content. Stripped wholesale.
_CHROME_TAG_RE = re.compile(
    r"<(?:nav|footer|header|aside|form|button|input|select|textarea)\b[^>]*>"
    r".*?</(?:nav|footer|header|aside|form|button|input|select|textarea)>",
    flags=re.DOTALL | re.IGNORECASE,
)

# Markers that real job descriptions tend to have. We require ≥2 of these
# in the cleaned text — pages that lack them (aggregator landing pages,
# generic homepages) get rejected as not-a-job-description.
_JOB_SECTION_MARKERS = (
    "qualific", "requir", "responsib", "experience", "skills",
    "about the role", "what you", "you'll", "you will", "we're looking",
    "ideal candidate", "must have", "nice to have", "duties",
    "day-to-day", "the role", "your role", "job description", "key skills",
    "minimum qualifications", "preferred qualifications", "what we offer",
    "benefits", "salary", "compensation",
)

# Phrases that strongly suggest chrome / promotional noise. If they
# dominate the cleaned text we reject the whole fetch.
_NOISE_PATTERNS = (
    "newsletter", "subscribe", "sign up for", "sign-up", "follow us",
    "join our community", "all rights reserved", "copyright ©",
    "cookie policy", "privacy policy", "terms of service",
    "looking for more", "never miss out", "get notified",
)


def _try_isolate_main(html: str) -> str:
    """Narrow ``html`` to the page's ``<main>`` / ``<article>`` /
    ``role="main"`` block when one is clearly identifiable. Falls back
    to the input unchanged when no such container is found — the caller
    still runs the chrome stripper on whatever is returned.
    """
    for pat in (
        r"<main\b[^>]*>(?P<body>.*?)</main>",
        r"<article\b[^>]*>(?P<body>.*?)</article>",
        r"<(?P<tag>div|section)\b[^>]*role\s*=\s*[\"']main[\"'][^>]*>(?P<body>.*?)</(?P=tag)>",
        # Common job-detail container ids/classes
        r"<(?P<tag2>div|section)\b[^>]*(?:class|id)\s*=\s*[\"'][^\"']*"
        r"(?:job[-_]?description|job[-_]?details?|posting[-_]?body|content[-_]?body)"
        r"[^\"']*[\"'][^>]*>(?P<body>.*?)</(?P=tag2)>",
    ):
        m = re.search(pat, html, flags=re.DOTALL | re.IGNORECASE)
        if m:
            inner = m.group("body")
            # Don't isolate to a <main> that's tiny — likely a SPA shell.
            if len(inner) > 300:
                return inner
    return html


def _is_aggregator_host(url: str) -> bool:
    """True for known aggregator hosts whose detail pages are page-scrape
    poison. The upstream API path handles those at ingest time."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _AGGREGATOR_HOSTS)


def _fetch_generic_html(url: str) -> Optional[dict]:
    """Fetch any URL, isolate the main content, return cleaned plain text
    iff it looks like a real job description. Returns ``None`` when:

      • URL host is a known aggregator (their pages are wrapped in
        newsletter chrome — the upstream API is the proper source).
      • Fetched page lacks at least 2 typical job-description section
        markers (qualifications / responsibilities / skills / etc.).
      • Page is dominated by promotional/legal phrases (newsletter,
        copyright, cookie policy) — > ~3% of body length.
      • Cleaned body is under 200 chars (SPA shells, cookie banners).

    Restricted to https for safety. gzip / deflate / brotli encodings
    auto-decoded even when the server ignores ``Accept-Encoding: identity``.
    """
    if not url or not url.lower().startswith("https://"):
        return None
    if _is_aggregator_host(url):
        return None
    try:
        import urllib.request
        req = urllib.request.Request(url, headers=_GENERIC_HEADERS)
        with urllib.request.urlopen(req, timeout=_GENERIC_FETCH_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "text" not in ctype:
                return None
            raw = resp.read(_GENERIC_FETCH_MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
            import gzip
            try:
                raw = gzip.decompress(raw)
            except Exception:
                return None
        elif encoding == "deflate":
            import zlib
            try:
                raw = zlib.decompress(raw)
            except Exception:
                try:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                except Exception:
                    return None
        elif encoding == "br":
            try:
                import brotli  # type: ignore
                raw = brotli.decompress(raw)
            except Exception:
                return None
        html = raw.decode(charset, errors="replace")
    except Exception:
        return None

    # Remove non-content chrome tags first so they can't bleed into the
    # main-isolation pass.
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html,
                   flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html,
                   flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<noscript\b[^>]*>.*?</noscript>", "", html,
                   flags=re.DOTALL | re.IGNORECASE)
    # Drop containers whose class/id screams chrome (newsletter, signup,
    # cookie, footer, related-jobs, share buttons, etc.). Run twice in
    # case nested chrome wrappers reveal each other.
    for _ in range(2):
        html = _CHROME_CLASS_RE.sub("", html)
    # Drop full sectioning chrome tags.
    html = _CHROME_TAG_RE.sub("", html)

    # Try to narrow to the page's main content area. Many job pages wrap
    # the description in <main>/<article>/<div class="job-description">.
    html = _try_isolate_main(html)

    text = strip_html(html)
    if len(text) < 200:
        return None

    # Quality filter 1: must look like a job description.
    text_lower = text.lower()
    marker_hits = sum(1 for m in _JOB_SECTION_MARKERS if m in text_lower)
    if marker_hits < 2:
        return None

    # Quality filter 2: drop pages dominated by chrome/promotional noise.
    # Each occurrence of a noise phrase is roughly worth its length plus
    # surrounding chrome that survived stripping. >3% of body == reject.
    noise_chars = sum(text_lower.count(p) * len(p) for p in _NOISE_PATTERNS)
    if noise_chars * 33 > len(text):  # ~3% threshold
        return None

    # Trailing-chrome trimmer: many job pages append "Apply now" CTAs,
    # related-jobs lists, etc. AFTER the description. Cut at the first
    # appearance of a strong terminator phrase if it's past 60% of the
    # body — preserves the description while dropping the tail.
    cutpoint = len(text)
    for term in ("Looking for more?", "Sign up for", "Subscribe to",
                 "Newsletter", "Related Jobs", "More jobs at",
                 "Copyright ©", "All rights reserved",
                 "Apply for this job", "Other Jobs"):
        idx = text.find(term)
        if 0 <= idx < cutpoint and idx > len(text) * 0.4:
            cutpoint = idx
    text = text[:cutpoint].rstrip()
    if len(text) < 200:
        return None

    if len(text) > 24000:
        text = text[:24000].rsplit(" ", 1)[0] + "…"
    return {"description": text}


def fetch_full_description(canonical_url: str, source: str) -> Optional[dict]:
    """Re-fetch the full description from the upstream source's per-job
    API. Returns ``{"description": text}`` or ``None`` when no fetcher
    can produce one.

    Routing order (first hit wins):
      1. URL-based ATS detection — covers GitHub-aggregated jobs that
         forward to greenhouse/lever/ashby/workable/workday/smartrecruiters
         even when their ``source`` field doesn't say so.
      2. Source-prefix routing — legacy fallback for ``ats:greenhouse:foo``
         style sources.
      3. Generic HTML fetch — last-ditch for company career pages and
         everything else. Returns None when the page renders <200 chars
         (SPA shells, cookie banners) so callers can keep using
         title-only scoring with the honest UI badge.
    """
    if not canonical_url:
        return None

    # Pass 1: URL-based detection (the new, better path — works regardless
    # of the source field).
    detected = _detect_ats_from_url(canonical_url)
    if detected:
        provider = detected.get("provider")
        try:
            if provider == "greenhouse":
                return _fetch_greenhouse(canonical_url, detected["slug"])
            if provider == "lever":
                return _fetch_lever(canonical_url, detected["slug"])
            if provider == "ashby":
                return _fetch_ashby(canonical_url, detected["slug"])
            if provider == "workable":
                return _fetch_workable(canonical_url, detected["slug"])
            if provider == "workday":
                return _fetch_workday(
                    canonical_url, detected.get("slug", ""),
                    detected.get("host", ""), detected.get("site", ""),
                )
            if provider == "smartrecruiters":
                return _fetch_smartrecruiters(
                    canonical_url, detected.get("slug", ""),
                    detected.get("job_id", ""),
                )
        except Exception:
            pass  # fall through to source-prefix and HTML fallbacks

    # Pass 2: source-prefix routing (kept for callers that pass a source
    # but a URL we don't recognize — rare, but cheap to keep).
    if source:
        parts = source.split(":")
        if len(parts) >= 3:
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
                pass

    # Pass 3: generic HTML — last-ditch for unknown apply URLs.
    return _fetch_generic_html(canonical_url)


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

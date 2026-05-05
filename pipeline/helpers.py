"""
pipeline/helpers.py
───────────────────
Stateless job-metadata inference functions and deduplication logic.

_last_merge_count is a module-level integer updated by deduplicate_jobs()
so callers (e.g. the Streamlit UI) can read how many duplicates were merged
in the most recent call without changing the function's return type.
"""

import re
from concurrent.futures import ThreadPoolExecutor

# Updated by deduplicate_jobs(); read by Streamlit Phase 2 metric display.
_last_merge_count: int = 0

# Updated by validate_job_urls(); read by Streamlit/CLI for status reporting.
_last_url_broken:        int = 0
_last_url_reconstructed: int = 0


# ── URL validation & fallback ─────────────────────────────────────────────────

_ATS_URL_TEMPLATES = {
    "greenhouse": "https://boards.greenhouse.io/{company}/jobs/{job_id}",
    "lever":      "https://jobs.lever.co/{company}/{job_id}",
}


def _slugify_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _head_ok(url: str, timeout: float = 5.0) -> bool:
    if not url:
        return False
    try:
        import requests  # noqa: PLC0415 — optional dependency; deferred import
    except ImportError:
        return True  # best-effort: treat as valid when requests is unavailable
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        if r.status_code < 400:
            return True
        # Some ATS pages refuse HEAD — fall through to GET.
        if r.status_code in (403, 405):
            r = requests.get(url, allow_redirects=True, timeout=timeout, stream=True)
            return r.status_code < 400
        return False
    except Exception:
        return False


def _reconstruct_url(job: dict) -> str | None:
    platform = (job.get("platform") or "").lower()
    job_id   = job.get("job_id") or job.get("id")
    company  = _slugify_company(job.get("company", ""))
    for key, tpl in _ATS_URL_TEMPLATES.items():
        if key in platform and job_id and company:
            return tpl.format(company=company, job_id=job_id)
    return None


def validate_job_urls(jobs: list, max_workers: int = 8) -> list:
    """HEAD-check every job URL; attempt reconstruction on failure.

    Mutates each job dict in place with:
      - url_status: "ok" | "reconstructed" | "broken"
      - url_original: previous URL if it was changed
    Returns the same list for chaining. Does NOT drop broken jobs —
    they stay visible for manual review.
    """
    global _last_url_broken, _last_url_reconstructed
    _last_url_broken = 0
    _last_url_reconstructed = 0

    def _check(job: dict) -> dict:
        global _last_url_broken, _last_url_reconstructed
        url = job.get("application_url") or job.get("url")
        if url and _head_ok(url):
            job["url_status"] = "ok"
            return job
        candidate = _reconstruct_url(job)
        if candidate and _head_ok(candidate):
            job["url_original"] = url
            if job.get("application_url") is not None:
                job["application_url"] = candidate
            job["url"] = candidate
            job["url_status"] = "reconstructed"
            _last_url_reconstructed += 1
            return job
        job["url_status"] = "broken"
        _last_url_broken += 1
        return job

    if not jobs:
        return jobs
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_check, jobs))
    return jobs

# Updated by filter_jobs_by_education(); read by Streamlit Phase 2 metric display.
_last_education_dropped_unknown: int = 0
_last_education_dropped_mismatch: int = 0

# Directional rank — higher means more advanced.
EDUCATION_RANK = {
    "high_school": 0,
    "hs":          0,
    "associates":  1,
    "bachelors":   2,
    "masters":     3,
    "phd":         4,
}


def education_matches(required: str, user: str) -> bool:
    """Return True iff the user's degree level meets the job's required level.

    Comparison is directional: a job that requires `bachelors` is acceptable
    for a `masters` user, but a job that requires `masters` is NOT acceptable
    for a `bachelors` user. Unknown values on either side return False here;
    callers decide how to treat unknowns.
    """
    r = EDUCATION_RANK.get((required or "").lower())
    u = EDUCATION_RANK.get((user or "").lower())
    if r is None or u is None:
        return False
    return r <= u


def filter_jobs_by_education(jobs: list, user_education_levels,
                              include_unknown: bool = True) -> list:
    """Drop jobs whose required education exceeds the user's highest level.

    `user_education_levels` may be a single string or a list of acceptable
    levels (the user's highest known level wins). Jobs tagged "unknown" are
    kept iff `include_unknown=True`. Updates module-level counters so
    Streamlit can display "X dropped as unknown / Y dropped as mismatch".
    """
    global _last_education_dropped_unknown, _last_education_dropped_mismatch
    _last_education_dropped_unknown = 0
    _last_education_dropped_mismatch = 0

    if not user_education_levels:
        return list(jobs)

    if isinstance(user_education_levels, str):
        levels = [user_education_levels]
    else:
        levels = list(user_education_levels)

    # User's highest known degree level dictates which jobs are reachable.
    user_ranks = [EDUCATION_RANK[l] for l in levels if l in EDUCATION_RANK]
    if not user_ranks:
        return list(jobs)
    user_top = max(user_ranks)

    kept: list = []
    for j in jobs:
        req = (j.get("education_required") or "unknown").lower()
        if req == "unknown":
            if include_unknown:
                kept.append(j)
            else:
                _last_education_dropped_unknown += 1
            continue
        r = EDUCATION_RANK.get(req)
        if r is None:
            if include_unknown:
                kept.append(j)
            else:
                _last_education_dropped_unknown += 1
            continue
        if r <= user_top:
            kept.append(j)
        else:
            _last_education_dropped_mismatch += 1
    return kept


# ── Cross-industry job-family inference ──────────────────────────────────────
# Title-keyword buckets used to attach a coarse `category` label at ingest.
# The cold (no-profile) feed balances results across these so a brand-new
# visitor sees a true cross-section instead of one industry's roles.
#
# Order matters — first hit wins. We list the more-specific buckets first
# so e.g. "data scientist" doesn't get bucketed under "engineering" simply
# because the title contains "engineer".

_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("healthcare",   ("nurse", "physician", "rn ", "lpn ", "doctor",
                      "medical assistant", "pharmacy", "clinical",
                      "therapist", "radiology", "dental", "patient")),
    ("legal",        ("paralegal", "attorney", "lawyer", "counsel",
                      "compliance officer", "litigation")),
    ("education",    ("teacher", "professor", "instructor", "tutor",
                      "curriculum", "principal", "school ", "preschool")),
    ("design",       ("designer", "ux", "ui ", "art director",
                      "creative director", "illustrator", "copywriter",
                      "brand designer")),
    ("marketing",    ("marketing", "brand ", "social media", "growth ",
                      "seo", "content writer", "communications",
                      "public relations")),
    ("sales",        ("account executive", " ae ", "sales rep", "sales ",
                      "business development", " bdr ", " sdr ",
                      "customer success")),
    ("finance",      ("accountant", "auditor", "controller", "actuar",
                      "investment", " finance ", "financial analyst",
                      "treasurer", "bookkeeper")),
    ("hr",           ("recruiter", "talent acquisition", "people ops",
                      "human resources", "hrbp")),
    ("consulting",   ("consultant", "consulting")),
    ("operations",   ("operations", "logistics", "supply chain",
                      "warehouse", "procurement", "fulfillment")),
    ("data",         ("data scientist", "data analyst", "data engineer",
                      "machine learning", "ml engineer", "ai engineer",
                      "research scientist")),
    ("product",      ("product manager", "program manager",
                      "project manager", "product owner")),
    ("engineering",  ("software engineer", "swe", "developer",
                      "engineer", "engineering", "fpga", "hardware",
                      "firmware", "devops", "site reliability",
                      "qa engineer", "test engineer")),
    ("support",      ("customer service", "customer support",
                      "support specialist", "help desk", "service desk")),
    ("public_sector",("policy", "public administration", "government",
                      "civic", "federal ")),
    ("trades",       ("technician", "electrician", "mechanic",
                      "plumber", "machinist", "welder", "installer")),
    ("media",        ("journalist", "editor", "producer", "videographer",
                      "podcast")),
]

_GENERAL_CATEGORY = "general"


def infer_job_category(job: dict) -> str:
    """Return a coarse job-family label (engineering, sales, marketing,
    healthcare, finance, design, …) from the title and description.
    Falls back to ``"general"`` when nothing matches.
    """
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()
    text = " " + title + " " + desc[:400] + " "
    for label, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return label
    return _GENERAL_CATEGORY


def infer_experience_level(job: dict) -> str:
    """Infer experience level from job title and description."""
    title = job.get("title", "").lower()
    text  = (title + " " + job.get("description", "")).lower()

    # Senior title cues take priority — a senior role mentioning "internal
    # candidates" should not be tagged as an internship.
    if any(k in title for k in ["senior", "sr.", "sr ", "staff", "principal",
                                "lead ", "manager", "director", "advisor",
                                "sme", "architect", "fellow"]):
        return "senior"
    # Use word-boundary-ish matching for intern to avoid "internal/international".
    if (" intern " in f" {title} " or " intern," in title or
        title.endswith(" intern") or "internship" in text or
        "co-op" in text or "coop" in text):
        return "internship"
    if any(k in text for k in ["entry-level", "entry level", "new grad", "new-grad",
                                "0-2 years", "junior", "associate"]):
        return "entry-level"
    if any(k in text for k in ["2-5 years", "3+ years", "mid-level"]):
        return "mid-level"
    if any(k in text for k in ["5+ years", "7+ years", "10+ years"]):
        return "senior"
    return "unknown"


def infer_education_required(job: dict) -> str:
    """Infer the GATING (minimum required) education level from job text.

    Returns the HIGHEST degree explicitly stated as required/minimum, since
    that is the gate the candidate must clear. JDs that list alternate
    experience paths ("PhD OR Master's + 5yrs OR Bachelor's + 20yrs") are
    senior roles — we tag them by their top requirement, not their fallback.

    Falls back to "unknown" rather than guessing low.
    """
    reqs = job.get("requirements", [])
    req_str = " ".join(reqs) if isinstance(reqs, list) else str(reqs)
    text = (job.get("title", "") + " " + job.get("description", "") + " " + req_str).lower()

    phd_kw = [
        "ph.d", "phd", "doctorate", "doctoral", "doctor of philosophy",
        "d.sc", "d.phil",
    ]
    masters_kw = [
        "master's", "masters", "m.s.", "ms degree", "m.sc", "msc",
        "meng", "m.eng", "master of science", "master of engineering",
        "graduate degree", "graduate-level", "advanced degree",
        "postgraduate",
        "ms in ", "ms or ", "an ms ", "ms required", "ms preferred",
        "ms/phd", "ms,",
    ]
    bachelors_kw = [
        "bachelor's", "bachelors", "b.s.", "bs degree",
        "b.sc", " bsc", "b.eng", " beng", "bachelor of science",
        "bachelor of engineering", "undergraduate degree", "pursuing a degree",
        "four-year degree", "4-year degree",
        "bs in ", "bs or ", "a bs ", "bs required", "bs preferred",
        "bs/ms", "bs,",
    ]
    associates_kw = ["associate's", "associates degree", "a.s.", "a.a.s"]
    hs_kw = ["high school diploma", "ged", "no degree required"]

    def _mentions(keywords: list) -> bool:
        return any(k in text for k in keywords)

    # Senior-role indicators in title/description override degree inference.
    title = job.get("title", "").lower()
    senior_title_kw = ["senior", "sr.", "staff", "principal", "lead",
                       "manager", "director", "advisor", "sme", "architect",
                       "fellow"]
    is_senior_title = any(k in title for k in senior_title_kw)

    # Pass 1: explicit "X required / minimum" phrasing — HIGHEST wins.
    phd_required = [
        "phd required", "ph.d required", "ph.d. required", "doctorate required",
        "phd preferred", "ph.d preferred", "must have a phd", "must have a ph.d",
        "phd in ", "ph.d. in ", "doctoral degree",
    ]
    masters_required = [
        "master's required", "masters required", "ms required", "m.s. required",
        "master's degree required", "masters degree required",
        "master's in ", "masters in ", "master of",
        "ms in ", "m.s. in ",
        "ms/phd", "ms or phd", "ms / phd",
        "graduate degree", "advanced degree", "postgraduate degree",
    ]
    bachelors_required = [
        "bachelor's required", "bachelors required", "bs required",
        "b.s. required", "bachelor's degree required", "bachelors degree required",
        "bachelor of", "undergraduate degree required",
        "bachelor's in ", "bachelors in ", "bs in ", "b.s. in ",
    ]

    def _phrase_hit(phrases: list) -> bool:
        return any(p in text for p in phrases)

    if _phrase_hit(phd_required):
        return "phd"
    if _phrase_hit(masters_required):
        return "masters"
    if _phrase_hit(bachelors_required):
        # If title screams senior and masters/phd are also mentioned anywhere,
        # the bachelor's hit is almost certainly the alternate-experience path.
        if is_senior_title and (_mentions(masters_kw) or _mentions(phd_kw)):
            return "masters" if _mentions(masters_kw) else "phd"
        return "bachelors"

    # Pass 2: no explicit "required" phrase. Use HIGHEST mention seen.
    if _mentions(phd_kw):
        return "phd"
    if _mentions(masters_kw):
        return "masters"
    if _mentions(bachelors_kw):
        if is_senior_title:
            return "unknown"
        return "bachelors"
    if _mentions(associates_kw):
        return "associates"
    if _mentions(hs_kw):
        return "high_school"
    return "unknown"


# ── Citizenship: hard blocks vs. soft mentions ────────────────────────────────

# HARD BLOCKS — these phrases mean the role is genuinely closed to non-citizens
# / non-cleared candidates. Match → return True from check_citizenship_requirement().
_HARD_CITIZENSHIP_RE = re.compile(
    r"\b("
    r"u\.?\s*s\.?\s*citizenship\s+(?:is\s+)?required"
    r"|must\s+be\s+(?:a\s+)?u\.?\s*s\.?\s*citizen"
    r"|us\s+citizens?\s+only"
    r"|us\s+persons?\s+only"
    r"|sole\s+u\.?\s*s\.?\s*citizenship"
    r"|citizenship\s+required"
    r"|security\s+clearance"
    r"|active\s+(?:secret|ts/sci|top\s+secret)\s+clearance"
    r"|secret\s+clearance"
    r"|top\s+secret"
    r"|ts/sci"
    r"|itar"
    r"|export[-\s]control(?:led)?"
    r"|no\s+sponsorship"
    r"|sponsorship\s+(?:is\s+)?not\s+available"
    r"|unable\s+to\s+sponsor"
    r"|cannot\s+sponsor"
    r"|will\s+not\s+sponsor"
    r")\b",
    re.IGNORECASE,
)

# SOFT MENTIONS — neutral / boilerplate language that previously caused false
# positives. These must NOT cause a hard block.
_SOFT_CITIZENSHIP_RE = re.compile(
    r"\b("
    r"authorized\s+to\s+work\s+in\s+the\s+u\.?\s*s\.?"
    r"|work\s+authorization"
    r"|equal\s+opportunity\s+employer"
    r"|visa\s+sponsorship\s+available"
    r"|open\s+to\s+all\s+work\s+authorizations"
    r"|sponsorship\s+available"
    r"|will\s+sponsor"
    r")\b",
    re.IGNORECASE,
)


def check_citizenship_requirement(description: str) -> bool:
    """Return True iff *description* contains a HARD citizenship/clearance block.

    Soft mentions like "Authorized to work in the US", "Work Authorization",
    "Equal Opportunity Employer", or "Visa sponsorship available" are
    explicitly ignored. Handles None / empty input gracefully.
    """
    if not description:
        return False
    text = str(description)
    # Strip soft phrases first so they cannot accidentally match a hard regex.
    cleaned = _SOFT_CITIZENSHIP_RE.sub(" ", text)
    return bool(_HARD_CITIZENSHIP_RE.search(cleaned))


def infer_citizenship_required(job: dict) -> str:
    """Infer US citizenship / clearance requirement from job text."""
    if not job:
        return "unknown"
    reqs = job.get("requirements", [])
    req_str = " ".join(reqs) if isinstance(reqs, list) else str(reqs)
    text = (job.get("title", "") + " " + job.get("description", "") + " " + req_str)
    if check_citizenship_requirement(text):
        return "yes"
    if _SOFT_CITIZENSHIP_RE.search(text):
        return "no"
    return "unknown"


# ── Inclusive-by-default per-job education filter ─────────────────────────────

_UNKNOWN_EDU_TOKENS = {"", "unknown", "unspecified", "none", "n/a"}


def education_filter(job: dict) -> bool:
    """Return True if *job* should be passed downstream to Phase 3.

    Inclusive-by-default: jobs with missing / unknown education metadata are
    KEPT so the LLM can read the full description in Phase 3. Only jobs with
    an explicit, recognized education level are eligible to be dropped here
    (and even then this function returns True — caller decides the cutoff).
    Handles None / missing fields gracefully.
    """
    if not job:
        return True
    raw = job.get("education_level") or job.get("education_required")
    if raw is None:
        return True
    val = str(raw).strip().lower()
    if val in _UNKNOWN_EDU_TOKENS:
        return True
    return True


# ── Glassdoor location normalization ──────────────────────────────────────────

# Glassdoor's API rejects very broad strings ("United States", "USA"). Map
# them to forms it accepts, and pass remote-style queries through unchanged.
_GLASSDOOR_LOCATION_OVERRIDES = {
    "united states":      "United States",
    "usa":                "United States",
    "u.s.":               "United States",
    "us":                 "United States",
    "america":            "United States",
    "north america":      "United States",
    "anywhere":           "Remote",
    "anywhere in the us": "Remote",
    "remote":             "Remote",
    "remote, us":         "Remote",
    "remote - us":        "Remote",
}


def clean_location_for_glassdoor(location: str) -> str:
    """Normalize *location* into a string Glassdoor's location parser accepts.

    - None / empty            → "United States"
    - Broad US synonyms       → "United States"
    - Remote-style strings    → "Remote"
    - "City, ST" or specific  → returned unchanged (trimmed)
    Handles None / non-string input gracefully.
    """
    if location is None:
        return "United States"
    s = str(location).strip()
    if not s:
        return "United States"
    key = s.lower()
    if key in _GLASSDOOR_LOCATION_OVERRIDES:
        return _GLASSDOOR_LOCATION_OVERRIDES[key]
    if "remote" in key:
        return "Remote"
    return s


def deduplicate_jobs(jobs: list) -> list:
    """Merge jobs with the same company+title, combining their locations.

    Also updates the module-level _last_merge_count with the number of
    duplicate entries that were absorbed.
    """
    global _last_merge_count

    def _norm(s: str) -> str:
        return re.sub(r'[^\w\s]', '', s.strip().lower())

    result: list = []
    seen: dict = {}  # normalised key → index in result

    for job in jobs:
        key = _norm(job.get("company", "")) + "|" + _norm(job.get("title", ""))
        if key not in seen:
            result.append(dict(job))
            seen[key] = len(result) - 1
        else:
            idx = seen[key]
            existing = result[idx]
            new_loc = job.get("location", "")
            if new_loc and new_loc not in existing.get("location", ""):
                existing["location"] = existing.get("location", "") + f", {new_loc}"
            if not existing.get("application_url") and job.get("application_url"):
                existing["application_url"] = job["application_url"]
            if not existing.get("salary_range") and job.get("salary_range"):
                existing["salary_range"] = job["salary_range"]
            if job.get("remote"):
                existing["remote"] = True

    _last_merge_count = len(jobs) - len(result)
    return result

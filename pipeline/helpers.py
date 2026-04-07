"""
pipeline/helpers.py
───────────────────
Stateless job-metadata inference functions and deduplication logic.

_last_merge_count is a module-level integer updated by deduplicate_jobs()
so callers (e.g. the Streamlit UI) can read how many duplicates were merged
in the most recent call without changing the function's return type.
"""

import re

# Updated by deduplicate_jobs(); read by Streamlit Phase 2 metric display.
_last_merge_count: int = 0


def infer_experience_level(job: dict) -> str:
    """Infer experience level from job title and description."""
    text = (job.get("title", "") + " " + job.get("description", "")).lower()
    if any(k in text for k in ["intern", "internship", "co-op", "coop"]):
        return "internship"
    if any(k in text for k in ["entry", "new grad", "graduate", "0-2 years",
                                "junior", "associate"]):
        return "entry-level"
    if any(k in text for k in ["2-5 years", "3+ years", "mid-level"]):
        return "mid-level"
    if any(k in text for k in ["senior", "sr.", "staff", "principal", "lead",
                                "5+ years", "7+ years"]):
        return "senior"
    return "unknown"


def infer_education_required(job: dict) -> str:
    """Infer the MINIMUM education requirement from job text.

    Returns the lowest degree that appears to be accepted. Phrases like
    "Bachelor's required, Master's preferred" must resolve to "bachelors",
    not "masters" — we scan explicit "required" clauses first, then fall
    back to raw keyword presence for the lowest-seen level.
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
        # Disambiguated short forms — avoid bare "ms" which collides
        # with words like "systems", "forms", etc.
        "ms in ", "ms or ", "an ms ", "ms required", "ms preferred",
        "ms/phd", "ms,",
    ]
    bachelors_kw = [
        "bachelor's", "bachelors", "bachelor", "b.s.", "bs degree",
        "b.sc", "bsc", "b.eng", "beng", "bachelor of science",
        "bachelor of engineering", "undergraduate", "pursuing a degree",
        "four-year degree", "4-year degree",
        # Disambiguated short forms
        "bs in ", "bs or ", "a bs ", "bs required", "bs preferred",
        "bs/ms", "bs,",
    ]
    associates_kw = ["associate's", "associates degree", "a.s.", "a.a.s"]
    hs_kw = ["high school diploma", "ged", "no degree required"]

    def _mentions(keywords: list) -> bool:
        return any(k in text for k in keywords)

    # Pass 1: explicit "X required" phrasing wins. Lowest level that is
    # explicitly stated as required becomes the minimum.
    required_phrases = [
        "bachelor's required", "bachelors required", "bs required",
        "b.s. required", "bachelor of", "undergraduate degree required",
    ]
    if any(p in text for p in required_phrases) and _mentions(bachelors_kw):
        return "bachelors"

    # Pass 2: fall back to lowest-seen mention.
    if _mentions(hs_kw):
        return "high_school"
    if _mentions(associates_kw):
        return "associates"
    if _mentions(bachelors_kw):
        return "bachelors"
    if _mentions(masters_kw):
        return "masters"
    if _mentions(phd_kw):
        return "phd"
    return "unknown"


def infer_citizenship_required(job: dict) -> str:
    """Infer US citizenship / clearance requirement from job text."""
    reqs = job.get("requirements", [])
    req_str = " ".join(reqs) if isinstance(reqs, list) else str(reqs)
    text = (job.get("title", "") + " " + job.get("description", "") + " " + req_str).lower()
    if any(k in text for k in [
        "us citizen", "u.s. citizen", "united states citizen",
        "security clearance", "secret clearance", "top secret",
        "itar", "us persons only", "must be a us citizen",
        "citizenship required", "due to export control",
    ]):
        return "yes"
    if any(k in text for k in [
        "visa sponsorship available", "open to all work authorizations",
        "no clearance required",
    ]):
        return "no"
    return "unknown"


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

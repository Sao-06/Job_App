"""
pipeline/job_search.py
──────────────────────
Single entry point :func:`search` that powers ``/api/jobs/feed``.

Pipeline:
    1. Build a SQL query against ``job_postings`` with indexed predicates
       for filters (deleted, experience_level, education_required,
       remote, posted_at).
    2. Optionally MATCH against the FTS5 virtual table using a query
       composed from the user's free-text query plus the profile's
       target titles + top hard skills. Use bm25() as a relevance
       signal SQLite returns natively.
    3. Re-rank the SQL top-N (default 200) in Python using a weighted
       score: 0.55*bm25 + 0.20*skill_overlap + 0.15*freshness +
       0.10*title_match.
    4. Trim to ``limit``, build a stable opaque cursor from the last
       row, return ``{jobs, next_cursor, total_estimate}``.
"""

from __future__ import annotations

import base64
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence


# ── Public dataclasses ────────────────────────────────────────────────────────

@dataclass
class SearchFilters:
    q: str = ""
    location: str = ""
    remote_only: bool = False
    experience_levels: Sequence[str] = field(default_factory=tuple)   # e.g. ('internship','entry-level')
    education_levels: Sequence[str] = field(default_factory=tuple)    # e.g. ('bachelors','masters')
    citizenship_filter: str = "all"        # 'all'|'exclude_required'|'only_required'
    posted_within_days: int | None = None
    blacklist: Sequence[str] = field(default_factory=tuple)           # company names (case-insensitive)
    whitelist: Sequence[str] = field(default_factory=tuple)
    include_unknown_education: bool = True
    # Coarse industry / job-family filter — values must match the labels emitted
    # by ``pipeline.helpers.infer_job_category`` (e.g. 'engineering', 'sales',
    # 'healthcare', 'general'). Multiple values OR together at the SQL layer.
    job_categories: Sequence[str] = field(default_factory=tuple)


@dataclass
class JobDTO:
    id: str
    url: str
    company: str
    title: str
    location: str
    remote: bool
    requirements: list
    salary_range: str
    experience_level: str
    education_required: str
    citizenship_required: str
    job_category: str
    posted_at: str | None
    source: str
    description: str = ""        # may be empty when source didn't carry one
    score: float = 0.0           # final ranking score [0..1]


@dataclass
class SearchPage:
    jobs: list[JobDTO]
    next_cursor: str | None
    total_estimate: int


# ── Helpers ───────────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z0-9+#.\-]{2,}")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _profile_terms(profile: dict | None, max_skills: int = 8,
                   max_titles: int = 4) -> list[str]:
    if not profile:
        return []
    titles = [str(t) for t in (profile.get("target_titles") or [])[:max_titles] if t]
    skills = [str(s) for s in (profile.get("top_hard_skills") or [])[:max_skills] if s]
    seen: set[str] = set()
    out: list[str] = []
    for tok in [*titles, *skills]:
        for piece in _tokenize(tok):
            if piece not in seen:
                seen.add(piece)
                out.append(piece)
    return out


def _build_fts_query(filters: SearchFilters, profile: dict | None) -> str:
    """Build the FTS5 query string. Returns "" when no terms.

    Two regimes:

    * **User typed something** (``filters.q`` non-empty): every typed token
      is REQUIRED (FTS5 implicit-AND). Profile terms are dropped from the
      MATCH — they still influence relevance via skill_overlap and
      title_match in the Python rerank below. Without this, a user who
      types "hardware engineer" but whose profile is marketing-themed sees
      the query OR'd with ~12 marketing tokens, the marketing jobs match
      via OR, and the actual hardware-engineer hits get outranked off the
      first page (the bug the user hit).

    * **No typed query**: profile-driven feed. OR every profile term
      together so any candidate role surfaces; the rerank picks order.
    """
    user_tokens: list[str] = []
    seen_user: set[str] = set()
    for tok in _tokenize(filters.q):
        if tok and tok not in seen_user and tok.isascii():
            seen_user.add(tok)
            user_tokens.append(tok)

    if user_tokens:
        # Quote each token to guard against FTS5 reserved words; whitespace-
        # join means implicit AND in FTS5. Cap at 24 to bound query size.
        return " ".join(f"\"{p}\"" for p in user_tokens[:24])

    profile_tokens: list[str] = []
    seen_profile: set[str] = set()
    for tok in _profile_terms(profile):
        if tok and tok not in seen_profile and tok.isascii():
            seen_profile.add(tok)
            profile_tokens.append(tok)
    if not profile_tokens:
        return ""
    return " OR ".join(f"\"{p}\"" for p in profile_tokens[:24])


def _decode_cursor(cursor: str | None) -> tuple[float, str, int] | None:
    """Returns (last_score, last_id, page_offset) or None for "first page"."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
        return float(obj.get("s", 0.0)), str(obj.get("i", "")), int(obj.get("o", 0))
    except Exception:
        return None


def _encode_cursor(score: float, jid: str, page_offset: int) -> str:
    payload = json.dumps({"s": round(score, 6), "i": jid, "o": page_offset},
                         separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


# ── Ranking ───────────────────────────────────────────────────────────────────

def _freshness(posted_at: str | None) -> float:
    if not posted_at:
        return 0.3
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.3
    days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    # Exp decay over 30 days; >90 days clamps at ~0.05.
    return max(0.05, math.exp(-days / 30.0))


def _skill_overlap(profile_skills: Iterable[str], requirements: Iterable[str]) -> float:
    """Coverage of the JOB's requirements by the user's skills, in [0, 1].

    Denominator is the count of distinct requirement strings — i.e. "what
    fraction of this job's requirements does the user satisfy?". Using the
    profile's skill count as the denominator (the previous behavior) penalized
    users with broad skill sets: matching 3 of 3 reqs out of a 30-skill
    profile produced 0.10, not 1.00, and crushed the entire scoring scale.

    Empty requirements OR empty profile → 0.3 (neutral) so jobs whose source
    didn't expose tags don't get pinned to zero on this signal.
    """
    skills = {s.lower().strip() for s in profile_skills if s and str(s).strip()}
    reqs = [str(r).lower().strip() for r in requirements if r and str(r).strip()]
    if not skills or not reqs:
        return 0.3
    matched = 0
    for req in reqs:
        if any(s in req or req in s for s in skills):
            matched += 1
            continue
        req_tokens = {t for t in _tokenize(req) if len(t) >= 3}
        if req_tokens and any(req_tokens & {t for t in _tokenize(s) if len(t) >= 3} for s in skills):
            matched += 1
    return matched / len(reqs)


def _title_match(profile_titles: Iterable[str], title: str) -> float:
    if not title:
        return 0.0
    t = title.lower()
    hits = 0
    total = 0
    for pt in profile_titles:
        if not pt:
            continue
        total += 1
        words = [w for w in re.split(r"\s+", pt.lower()) if len(w) > 2]
        if any(w in t for w in words):
            hits += 1
    return (hits / total) if total else 0.0


def _normalize_bm25(raw_scores: list[float]) -> list[float]:
    """SQLite bm25() returns negative scores where lower is better. We
    invert and min-max normalize to [0..1]. Empty input = []."""
    if not raw_scores:
        return []
    inverted = [-s for s in raw_scores]
    lo = min(inverted)
    hi = max(inverted)
    if hi - lo < 1e-9:
        return [0.5] * len(inverted)
    return [(x - lo) / (hi - lo) for x in inverted]


# ── Listing-level dedup (cross-source + within-source multi-city) ─────────────

# Generic affixes that vary between mirrors of the same posting and would
# otherwise prevent (company, title) collapse. Stripped during normalization.
_TITLE_DROP_RE = re.compile(
    r"\b(?:"
    r"remote|hybrid|onsite|on[-\s]?site|"
    r"full[-\s]?time|part[-\s]?time|contractor|contract|"
    r"summer|fall|spring|winter|"
    r"\d{4}|\d+(?:st|nd|rd|th)?|"
    r"q[1-4]|h[12]|"
    r"u\.?s\.?\s*(?:only|remote|based)?|"
    r"north\s*america|americas|emea|apac|"
    r"intern(?:ship)?|new\s*grad|entry[-\s]?level"
    r")\b",
    re.IGNORECASE,
)


def _normalize_title(title: str) -> str:
    """Strip differentiators that vary between mirrors of the same posting
    (year, season, work-model parens, "Intern" vs "Internship", etc.).
    Used as part of the dedup key.
    """
    if not title:
        return ""
    t = title.lower()
    # Drop parenthetical/bracketed qualifiers
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", t)
    # Drop common varying tokens
    t = _TITLE_DROP_RE.sub(" ", t)
    # Collapse non-word runs to single spaces
    t = re.sub(r"[^\w]+", " ", t).strip()
    return t


def _location_key(location: str) -> str:
    """First city/state token of the location, lowercased. None / blank → ''."""
    if not location:
        return ""
    first = location.split(",")[0].strip().lower()
    if first in ("remote", "anywhere", "worldwide", "us", "usa", "united states"):
        return "remote"
    return first


def _dedupe_by_listing(ranked: list[tuple[float, tuple]]) -> list[tuple[float, tuple]]:
    """Drop lower-ranked rows that share (company_norm, title_norm, loc_key).

    Keeps the FIRST occurrence per dedup key. Because ``ranked`` is already
    sorted by descending score, the highest-scored mirror of each posting
    wins. Cross-source duplicates (same Stripe SWE intern listed via
    Greenhouse + SimplifyJobs README + Adzuna) collapse to one card.

    Listings that genuinely differ by location (Stripe SWE intern in SF vs
    NYC) are preserved because the location_key differs.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[float, tuple]] = []
    for score, row in ranked:
        company = (row[3] or "").strip().lower()
        title_norm = _normalize_title(row[4] or "")
        loc_key = _location_key(row[5] or "")
        key = (company, title_norm, loc_key)
        if not company or not title_norm:
            # Garbage rows — skip rather than blanket-collapse via empty key.
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append((score, row))
    return out


def _diversify_by_category(ranked: list[tuple[float, tuple]],
                            *, max_per_round: int = 2) -> list[tuple[float, tuple]]:
    """Round-robin over coarse job categories (engineering, sales,
    marketing, healthcare, finance, …) so a brand-new visitor with no
    profile sees a true cross-industry sample on page 1 instead of an
    all-tech wall.

    Order within each round is by the bucket's top-scored entry. Tech-
    heavy buckets stay in the rotation longer because they have more
    inventory, but they no longer monopolize the first 30 cards.
    """
    if not ranked:
        return []
    from collections import OrderedDict
    buckets: "OrderedDict[str, list]" = OrderedDict()
    for score, row in ranked:
        cat = (row[12] or "general").strip().lower() or "general"
        buckets.setdefault(cat, []).append((score, row))
    ordered_buckets = sorted(
        buckets.values(),
        key=lambda b: (-b[0][0], b[0][1][12] or ""),
    )
    out: list[tuple[float, tuple]] = []
    max_round = max(len(b) for b in ordered_buckets)
    take = max(1, int(max_per_round))
    for r0 in range(0, max_round, take):
        for bucket in ordered_buckets:
            for offset in range(take):
                if r0 + offset < len(bucket):
                    out.append(bucket[r0 + offset])
    return out


def _diversify_by_company(ranked: list[tuple[float, tuple]],
                          *, max_per_round: int = 1) -> list[tuple[float, tuple]]:
    """Round-robin layout: each round pulls one (or up to *max_per_round*)
    listings from each company, in descending order of that company's
    top score. Round 1 = best from each unique company; round 2 = next-best
    from each company that still has rows; etc.

    Effect: the user's first page always shows a variety of companies
    instead of 30 listings in a row from whichever company matches best.
    Subsequent scrolls bring in second / third entries from each company.
    Companies with deeper inventories stay in the rotation longer.
    """
    if not ranked:
        return []
    from collections import defaultdict, OrderedDict
    # Use OrderedDict over the input so ties resolve to the original
    # ranking order (which carries deterministic id-based tie-break).
    buckets: "OrderedDict[str, list]" = OrderedDict()
    for score, row in ranked:
        company = (row[3] or "").strip().lower()
        buckets.setdefault(company, []).append((score, row))
    # Order companies by their best entry's score, descending.
    ordered_buckets = sorted(
        buckets.values(),
        key=lambda b: (-b[0][0], b[0][1][3] or ""),
    )
    out: list[tuple[float, tuple]] = []
    max_round = max(len(b) for b in ordered_buckets)
    take = max(1, int(max_per_round))
    for r0 in range(0, max_round, take):
        for bucket in ordered_buckets:
            for offset in range(take):
                if r0 + offset < len(bucket):
                    out.append(bucket[r0 + offset])
    return out


# ── Core query ────────────────────────────────────────────────────────────────

# Bump the rank pool because the dedup + diversification steps drop a
# meaningful fraction of rows (≥ 30% on the live index). We need enough
# raw material to fill the requested page after thinning.
_DEFAULT_RANK_POOL = 500

# How many entries from any single company can land in one "round" of the
# round-robin layout. 1 = strict interleave (no two adjacent rows share a
# company). Higher values are more clumpy but show more depth per company.
_DEFAULT_PER_ROUND = 1


def _row_to_dto(row: tuple, score: float) -> JobDTO:
    # Row layout: 14 base columns, optionally followed by ``description``,
    # always followed by ``bm25_score``. So a 15-tuple has no description
    # and ``row[14]`` is bm25; a 16-tuple has description at ``row[14]`` and
    # bm25 at ``row[15]``. Both shapes coexist while callers migrate.
    (jid, url, source, company, title, location, remote, reqs_json,
     salary, exp, edu, cit, category, posted_at) = row[:14]
    description = (row[14] if len(row) >= 16 else "") or ""
    reqs = json.loads(reqs_json) if reqs_json else []
    return JobDTO(
        id=jid, url=url, source=source, company=company, title=title,
        location=location or "", remote=bool(remote), requirements=reqs,
        salary_range=salary or "Unknown",
        experience_level=exp or "unknown",
        education_required=edu or "unknown",
        citizenship_required=cit or "unknown",
        job_category=category or "general",
        posted_at=posted_at,
        description=description,
        score=round(float(score), 4),
    )


def search(*, conn: sqlite3.Connection,
           filters: SearchFilters,
           profile: dict | None,
           cursor: str | None,
           limit: int = 30,
           rank_pool: int = _DEFAULT_RANK_POOL,
           dedupe: bool = True,
           per_round: int = _DEFAULT_PER_ROUND) -> SearchPage:
    """One-shot read against the active jobs index. p95 target < 250 ms."""
    cur_state = _decode_cursor(cursor)
    page_offset = cur_state[2] if cur_state else 0

    fts_q = _build_fts_query(filters, profile)
    where: list[str] = ["jp.deleted = 0"]
    # Track FTS-MATCH bindings separately from WHERE-clause bindings. The
    # COUNT-fallback query below has no JOIN, so it only needs WHERE params;
    # passing the shared `params` list with fts_q baked in is what produced
    # the "uses 8, supplies 9" sqlite3.ProgrammingError.
    fts_params: list[Any] = []
    where_params: list[Any] = []
    join_fts = ""

    if fts_q:
        join_fts = (
            "JOIN job_postings_fts fts ON fts.rowid = jp.rowid "
            "AND fts.job_postings_fts MATCH ? "
        )
        fts_params.append(fts_q)
        bm25_select = "bm25(job_postings_fts) AS bm25_score"
        order_extra = "bm25_score ASC,"   # lower bm25 = better match
    else:
        bm25_select = "0.0 AS bm25_score"
        order_extra = ""

    if filters.experience_levels:
        placeholders = ",".join("?" * len(filters.experience_levels))
        where.append(f"jp.experience_level IN ({placeholders})")
        where_params.extend(filters.experience_levels)
    if filters.education_levels:
        edus = list(filters.education_levels)
        if filters.include_unknown_education:
            edus = list(set(edus) | {"unknown"})
        placeholders = ",".join("?" * len(edus))
        where.append(f"jp.education_required IN ({placeholders})")
        where_params.extend(edus)
    if filters.remote_only:
        where.append("jp.remote = 1")
    if filters.posted_within_days:
        where.append(
            "(jp.posted_at IS NULL OR jp.posted_at >= date('now', ?))"
        )
        where_params.append(f"-{int(filters.posted_within_days)} days")
    if filters.location:
        where.append("LOWER(jp.location) LIKE ?")
        where_params.append(f"%{filters.location.strip().lower()}%")
    if filters.job_categories:
        # De-dup + lowercase to match the labels written at ingest time. We do
        # NOT auto-include "general" — if the caller wants the unbucketed
        # remainder they pass it explicitly. That keeps "Engineering only" from
        # silently surfacing 23k uncategorized rows.
        cats_clean = sorted({(c or "").strip().lower() for c in filters.job_categories if c and str(c).strip()})
        if cats_clean:
            placeholders = ",".join("?" * len(cats_clean))
            where.append(f"LOWER(COALESCE(jp.job_category, 'general')) IN ({placeholders})")
            where_params.extend(cats_clean)
    if filters.citizenship_filter == "exclude_required":
        where.append("(jp.citizenship_required IS NULL OR jp.citizenship_required != 'yes')")
    elif filters.citizenship_filter == "only_required":
        where.append("jp.citizenship_required = 'yes'")
    if filters.blacklist:
        # _csv() in the caller already strips empties; defensive double-check
        # here keeps placeholder count and binding count exactly aligned.
        bl_clean = [b.strip().lower() for b in filters.blacklist if b and b.strip()]
        if bl_clean:
            placeholders = ",".join("?" * len(bl_clean))
            where.append(f"LOWER(jp.company) NOT IN ({placeholders})")
            where_params.extend(bl_clean)
    # NOTE: whitelist is intentionally NOT applied as a SQL filter. Per the
    # spec (Workflow/job-application-agent.md), whitelist companies are
    # *priority targets* that should be surfaced first regardless of score —
    # not a hard inclusion list that hides everyone else. It's applied as a
    # ranking boost in the Python re-rank stage below.

    # Grow the ranked pool with page depth so users can scroll indefinitely.
    # Each subsequent page expands the SQL LIMIT proportionally — page 0 ranks
    # the configured pool, page 5 ranks 6× that, etc. SQLite + FTS5 handle a
    # 3000-row rank in well under a second, and we cap to keep memory bounded.
    effective_pool = min(rank_pool * max(1, page_offset + 1), 8000)

    sql = f"""
        SELECT jp.id, jp.canonical_url, jp.source, jp.company, jp.title,
               jp.location, jp.remote, jp.requirements_json, jp.salary_range,
               jp.experience_level, jp.education_required,
               jp.citizenship_required, jp.job_category, jp.posted_at,
               jp.description,
               {bm25_select}
          FROM job_postings jp
          {join_fts}
         WHERE {' AND '.join(where)}
         ORDER BY {order_extra} jp.posted_at DESC, jp.id ASC
         LIMIT ?
    """
    main_params = [*fts_params, *where_params, effective_pool]
    pool = conn.execute(sql, main_params).fetchall()
    if not pool:
        # Total estimate uses ONLY WHERE bindings — the COUNT(*) query has no
        # FTS JOIN, so handing it fts_q would produce a binding-count mismatch.
        total = conn.execute(
            f"SELECT COUNT(*) FROM job_postings jp WHERE {' AND '.join(where)}",
            where_params,
        ).fetchone()[0]
        return SearchPage(jobs=[], next_cursor=None, total_estimate=int(total))

    bm25_scores = _normalize_bm25([row[-1] for row in pool])
    profile_titles = (profile or {}).get("target_titles") or []
    profile_skills = (profile or {}).get("top_hard_skills") or []
    whitelist_lower = {w.strip().lower() for w in (filters.whitelist or ()) if w and w.strip()}

    ranked: list[tuple[float, tuple]] = []
    for row, bm in zip(pool, bm25_scores):
        reqs = json.loads(row[7]) if row[7] else []
        sk_ov = _skill_overlap(profile_skills, reqs)
        fr    = _freshness(row[13])              # posted_at column
        tm    = _title_match(profile_titles, row[4])
        if not fts_q and not profile_skills and not profile_titles:
            # No personalization signals at all — sort by recency only.
            final = fr
        else:
            final = (0.45 * bm) + (0.30 * sk_ov) + (0.15 * fr) + (0.10 * tm)
        # Whitelist boost: priority companies surface first per spec. Substring
        # match handles "Apple" vs "Apple Inc." and similar suffix drift.
        if whitelist_lower:
            company_lower = (row[3] or "").strip().lower()
            if company_lower and any(
                w == company_lower or w in company_lower or company_lower in w
                for w in whitelist_lower
            ):
                final = min(1.0, final + 0.25)
        ranked.append((final, row))

    ranked.sort(key=lambda x: (-x[0], x[1][13] or "", x[1][0]))

    # Drop cross-source / multi-city duplicates so the same listing can't
    # appear three times under different URLs. Runs BEFORE the company
    # round-robin so each bucket already contains unique listings.
    if dedupe:
        ranked = _dedupe_by_listing(ranked)

    # Cold (no-profile) feed gets an additional category-balancing pass
    # so a brand-new visitor sees a true cross-section: round 1 takes the
    # best entry from each of {engineering, sales, marketing, healthcare,
    # finance, …}, round 2 the second-best, etc. Once the user has a
    # profile we trust the BM25 + skill_overlap signals to surface what
    # they actually want, so we skip this layer.
    profile_terms_present = bool(profile_skills or profile_titles or fts_q)
    if not profile_terms_present and ranked:
        ranked = _diversify_by_category(ranked, max_per_round=2)

    # Round-robin layout — interleaves companies so a single dominant
    # employer can't take over the top page. Round 1 = best from each
    # company, round 2 = second-best, etc. Disabled when per_round <= 0.
    if per_round and per_round > 0:
        ranked = _diversify_by_company(ranked, max_per_round=per_round)

    # Apply cursor offset (seek to the row *after* the last id). Falls back
    # to a position-based start when the last id can't be located in the
    # ranked pool (dedup may have dropped it, or ranking shifted between
    # requests). Without this fallback, a cursor-id miss reset start=0 and
    # the user got the same first page on every "load more" — appearing as
    # a stuck infinite-scroll.
    start = 0
    if cur_state:
        last_score, last_id, _po = cur_state
        found = False
        for i, (s, r) in enumerate(ranked):
            if r[0] == last_id and abs(s - last_score) < 1e-4:
                start = i + 1
                found = True
                break
        if not found:
            start = min(page_offset * limit, len(ranked))

    window = ranked[start:start + limit]
    dtos = [_row_to_dto(row, score) for score, row in window]

    # Emit a next_cursor whenever there *might* be more rows. The client
    # treats a non-null cursor as "keep scrolling"; we'd rather over-emit
    # and let the next call return [] than under-emit and stop scrolling
    # prematurely. The total_estimate check below covers the case where
    # the ranked pool was exhausted but the SQL universe still has rows
    # we'd surface with a deeper rank_pool on the next call.
    next_cursor: str | None = None
    has_more_in_pool = (start + limit) < len(ranked)
    pool_was_capped = len(pool) >= effective_pool
    if len(window) == limit and (has_more_in_pool or pool_was_capped):
        last = window[-1]
        next_cursor = _encode_cursor(last[0], last[1][0], page_offset + 1)

    # Lightweight total estimate. COUNT has no FTS JOIN, so it only takes
    # WHERE-clause bindings — never fts_q. `where` always includes the
    # deleted=0 baseline; >1 means at least one filter clause was added.
    if len(where) > 1:
        total = conn.execute(
            f"SELECT COUNT(*) FROM job_postings jp WHERE {' AND '.join(where)}",
            where_params,
        ).fetchone()[0]
    else:
        total = conn.execute("SELECT COUNT(*) FROM job_postings WHERE deleted = 0").fetchone()[0]
    return SearchPage(jobs=dtos, next_cursor=next_cursor, total_estimate=int(total))


# ── since_id helper for the polling path ──────────────────────────────────────

def newer_than(*, conn: sqlite3.Connection, top_id: str, limit: int = 30) -> list[JobDTO]:
    """Rows whose last_seen_at is newer than the row with id=top_id, sorted
    newest-first. Used by the SPA's 25s polling tick to prepend fresh
    inventory."""
    row = conn.execute(
        "SELECT last_seen_at FROM job_postings WHERE id = ?", (top_id,)
    ).fetchone()
    cutoff = row[0] if row else None
    if not cutoff:
        return []
    rows = conn.execute(
        """
        SELECT id, canonical_url, source, company, title, location, remote,
               requirements_json, salary_range, experience_level,
               education_required, citizenship_required, job_category,
               posted_at, description, 0.0 AS bm25_score
          FROM job_postings
         WHERE deleted = 0 AND last_seen_at > ?
         ORDER BY last_seen_at DESC
         LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()
    return [_row_to_dto(r, 0.0) for r in rows]

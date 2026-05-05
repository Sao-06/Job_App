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
    posted_at: str | None
    source: str
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
    """Build a permissive FTS5 query string. Returns "" when no terms.

    SQLite FTS5 syntax: a list of bare tokens joined by space is an OR
    in unicode61 tokenizer; quoting with double-quotes phrase-locks. We
    OR the user's query tokens with the profile-derived ones so the
    same query that drives the SQL filter also drives the relevance.
    """
    parts: list[str] = []
    parts.extend(_tokenize(filters.q))
    parts.extend(_profile_terms(profile))
    seen: set[str] = set()
    deduped: list[str] = []
    for p in parts:
        if p and p not in seen and p.isascii():
            seen.add(p)
            deduped.append(p)
    if not deduped:
        return ""
    # Quote each token to guard against FTS5 reserved words / operators.
    return " OR ".join(f"\"{p}\"" for p in deduped[:24])


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
    a = {s.lower().strip() for s in profile_skills if s}
    b: set[str] = set()
    for r in requirements:
        for tok in _tokenize(r):
            b.add(tok)
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a))


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
    # row is the full SELECT — drop trailing bm25_score column (always present).
    (jid, url, source, company, title, location, remote, reqs_json,
     salary, exp, edu, cit, posted_at) = row[:13]
    reqs = json.loads(reqs_json) if reqs_json else []
    return JobDTO(
        id=jid, url=url, source=source, company=company, title=title,
        location=location or "", remote=bool(remote), requirements=reqs,
        salary_range=salary or "Unknown",
        experience_level=exp or "unknown",
        education_required=edu or "unknown",
        citizenship_required=cit or "unknown",
        posted_at=posted_at,
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
    params: list[Any] = []
    join_fts = ""

    if fts_q:
        join_fts = (
            "JOIN job_postings_fts fts ON fts.rowid = jp.rowid "
            "AND fts.job_postings_fts MATCH ? "
        )
        params.append(fts_q)
        # bm25 column expr is the FTS table itself
        bm25_select = "bm25(job_postings_fts) AS bm25_score"
        order_extra = "bm25_score ASC,"   # lower bm25 = better match
    else:
        bm25_select = "0.0 AS bm25_score"
        order_extra = ""

    if filters.experience_levels:
        placeholders = ",".join("?" * len(filters.experience_levels))
        where.append(f"jp.experience_level IN ({placeholders})")
        params.extend(filters.experience_levels)
    if filters.education_levels:
        placeholders = ",".join("?" * len(filters.education_levels))
        # honor "include unknowns" = always add 'unknown' as a passthrough
        edus = list(filters.education_levels)
        if filters.include_unknown_education:
            edus = list(set(edus) | {"unknown"})
        placeholders = ",".join("?" * len(edus))
        where.append(f"jp.education_required IN ({placeholders})")
        params.extend(edus)
    if filters.remote_only:
        where.append("jp.remote = 1")
    if filters.posted_within_days:
        where.append(
            "(jp.posted_at IS NULL OR jp.posted_at >= date('now', ?))"
        )
        params.append(f"-{int(filters.posted_within_days)} days")
    if filters.location:
        where.append("LOWER(jp.location) LIKE ?")
        params.append(f"%{filters.location.strip().lower()}%")
    if filters.citizenship_filter == "exclude_required":
        where.append("(jp.citizenship_required IS NULL OR jp.citizenship_required != 'yes')")
    elif filters.citizenship_filter == "only_required":
        where.append("jp.citizenship_required = 'yes'")
    if filters.blacklist:
        placeholders = ",".join("?" * len(filters.blacklist))
        where.append(f"LOWER(jp.company) NOT IN ({placeholders})")
        params.extend(b.strip().lower() for b in filters.blacklist if b)
    if filters.whitelist:
        placeholders = ",".join("?" * len(filters.whitelist))
        where.append(f"LOWER(jp.company) IN ({placeholders})")
        params.extend(w.strip().lower() for w in filters.whitelist if w)

    sql = f"""
        SELECT jp.id, jp.canonical_url, jp.source, jp.company, jp.title,
               jp.location, jp.remote, jp.requirements_json, jp.salary_range,
               jp.experience_level, jp.education_required,
               jp.citizenship_required, jp.posted_at,
               {bm25_select}
          FROM job_postings jp
          {join_fts}
         WHERE {' AND '.join(where)}
         ORDER BY {order_extra} jp.posted_at DESC, jp.id ASC
         LIMIT ?
    """
    params.append(rank_pool)
    pool = conn.execute(sql, params).fetchall()
    if not pool:
        # Total estimate query — also indexed.
        total = conn.execute(
            f"SELECT COUNT(*) FROM job_postings jp WHERE {' AND '.join(where)}",
            params[:-1],            # drop the LIMIT
        ).fetchone()[0]
        return SearchPage(jobs=[], next_cursor=None, total_estimate=int(total))

    bm25_scores = _normalize_bm25([row[-1] for row in pool])
    profile_titles = (profile or {}).get("target_titles") or []
    profile_skills = (profile or {}).get("top_hard_skills") or []

    ranked: list[tuple[float, tuple]] = []
    for row, bm in zip(pool, bm25_scores):
        reqs = json.loads(row[7]) if row[7] else []
        sk_ov = _skill_overlap(profile_skills, reqs)
        fr    = _freshness(row[12])
        tm    = _title_match(profile_titles, row[4])
        if not fts_q and not profile_skills and not profile_titles:
            # No personalization signals at all — sort by recency only.
            final = fr
        else:
            final = (0.55 * bm) + (0.20 * sk_ov) + (0.15 * fr) + (0.10 * tm)
        ranked.append((final, row))

    ranked.sort(key=lambda x: (-x[0], x[1][12] or "", x[1][0]))

    # Drop cross-source / multi-city duplicates so the same listing can't
    # appear three times under different URLs. Runs BEFORE the company
    # round-robin so each bucket already contains unique listings.
    if dedupe:
        ranked = _dedupe_by_listing(ranked)

    # Round-robin layout — interleaves companies so a single dominant
    # employer can't take over the top page. Round 1 = best from each
    # company, round 2 = second-best, etc. Disabled when per_round <= 0.
    if per_round and per_round > 0:
        ranked = _diversify_by_company(ranked, max_per_round=per_round)

    # Apply cursor offset (seek to the row *after* the last id).
    start = 0
    if cur_state:
        last_score, last_id, _po = cur_state
        for i, (s, r) in enumerate(ranked):
            if r[0] == last_id and abs(s - last_score) < 1e-4:
                start = i + 1
                break

    window = ranked[start:start + limit]
    dtos = [_row_to_dto(row, score) for score, row in window]

    next_cursor: str | None = None
    if len(window) == limit and start + limit < len(ranked):
        last = window[-1]
        next_cursor = _encode_cursor(last[0], last[1][0], page_offset + 1)
    elif len(window) == limit:
        # We'd need to re-query with a higher LIMIT. For simplicity we just
        # offer the next page by encoding the new offset and bumping LIMIT
        # client-side via a re-call. The client always sees a non-null
        # cursor as long as there might be more rows.
        last = window[-1]
        next_cursor = _encode_cursor(last[0], last[1][0], page_offset + 1)

    # Lightweight total estimate (no FTS — that's expensive to count).
    where_no_fts = [w for w in where if "fts" not in w.lower()]
    if where_no_fts:
        total = conn.execute(
            f"SELECT COUNT(*) FROM job_postings jp WHERE {' AND '.join(where_no_fts)}",
            [p for p in params[:-1] if not isinstance(p, str) or p != fts_q],
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
               education_required, citizenship_required, posted_at,
               0.0 AS bm25_score
          FROM job_postings
         WHERE deleted = 0 AND last_seen_at > ?
         ORDER BY last_seen_at DESC
         LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()
    return [_row_to_dto(r, 0.0) for r in rows]

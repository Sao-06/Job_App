"""
pipeline/user_scoring.py
────────────────────────
Persistent per-(user, job) job scorer.

Why
───
`pipeline/job_search.search` ranks the live job index against a profile on
every request, but the ranking pool is finite (≤8000 rows) and the score is
recomputed per request. When a user uploads / switches their primary resume,
we want every job in the index scored against that resume up-front so the
feed can ``ORDER BY user_job_scores.score DESC`` in a single SQL query and
return the very top matches first regardless of recency / BM25 noise.

What this module does
─────────────────────
* ``score_jobs_for_user(conn, user_id, profile, …)`` — bulk-score every
  non-deleted job in the index against ``profile`` using the same
  ``compute_skill_coverage`` deterministic scorer the lazy
  ``/api/jobs/score-batch`` endpoint uses, write rows to
  ``user_job_scores``. Idempotent: rows whose profile hash matches the
  caller's ``profile_hash`` (and which were computed after the row's
  ``last_seen_at``) are skipped. Safe to interrupt — partial state is fine.
* ``score_new_jobs_for_user(conn, user_id, profile, …)`` — incremental
  variant: only scores rows we don't have a stored score for. Used by the
  periodic refresh task once the user is fully primed.
* ``profile_signature(profile)`` — stable short hash so we can short-circuit
  rescoring when the profile didn't actually change between calls.
* ``known_user_ids_with_scores(conn)`` — used by the scheduler hook so it
  can refresh "incremental" scores for users who have at least one row.

All writes are serialized through ``pipeline.ingest._WRITE_LOCK`` when the
caller passes ``write_lock=...``; tests / one-shot scripts can omit it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from .providers import RUBRIC_WEIGHTS, compute_skill_coverage


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_BATCH_SIZE = 500            # rows fetched per SELECT pass
_WRITE_BATCH_SIZE = 200      # rows per executemany
_MAX_JOBS_PER_CALL = 50_000  # safety cap so a malicious / runaway call can't pin the worker


def profile_signature(profile: dict | None) -> str:
    """A stable short digest of the fields user-scoring actually consumes.

    Only includes signals that change the score: target_titles,
    top_hard_skills, location, configured experience_levels. Other profile
    fields (name, education, projects, …) don't affect the math and would
    cause spurious cache invalidations.
    """
    if not profile:
        return ""
    payload = {
        "titles": sorted(
            (str(t).strip().lower() for t in (profile.get("target_titles") or []) if t),
        ),
        "skills": sorted(
            (str(s).strip().lower() for s in (profile.get("top_hard_skills") or []) if s),
        ),
        "loc": str(profile.get("location") or "").strip().lower(),
        "exp": sorted(
            (str(e).strip().lower() for e in (profile.get("experience_levels") or []) if e),
        ),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ── Title / loc / seniority sub-scorers (mirror the lazy score-batch path) ───

_TITLE_TOKEN_RE = re.compile(r"\s+")


def _title_match(target_titles: list[str], title: str) -> float:
    """Best partial token-overlap across target_titles."""
    if not title or not target_titles:
        return 0.0
    t = title.lower()
    best = 0.0
    for pt in target_titles:
        words = [w for w in _TITLE_TOKEN_RE.split(str(pt).lower()) if len(w) > 2]
        if not words:
            continue
        hits = sum(1 for w in words if w in t)
        if hits:
            best = max(best, hits / len(words))
    return best


def _location_match(profile_loc: str, job_loc: str, job_remote: bool) -> float:
    prof = (profile_loc or "").strip().lower()
    jloc = (job_loc or "").lower()
    if not prof:
        return 0.6 if job_remote else 0.5
    first = prof.split(",")[0].strip()
    if first and first in jloc:
        return 1.0
    if any(p.strip() in jloc for p in prof.split(",") if p.strip()):
        return 0.7
    if job_remote:
        return 0.9
    return 0.25


def _seniority_match(exp_prefs: list[str], job_exp: str) -> float:
    levels = [str(e).strip().lower() for e in exp_prefs or [] if e]
    je = (job_exp or "").strip().lower()
    if not levels:
        return 0.5
    if not je or je == "unknown":
        return 0.6
    return 1.0 if je in levels else 0.2


def _row_to_job(row: tuple) -> dict:
    """Adapt a job_postings row into the dict shape compute_skill_coverage
    expects. Same column order as the SELECT in `_iter_jobs`."""
    (jid, title, location, remote, requirements_json, experience_level,
     description) = row
    reqs: list = []
    if requirements_json:
        try:
            reqs = json.loads(requirements_json)
        except Exception:
            reqs = []
    return {
        "id": jid,
        "title": title or "",
        "location": location or "",
        "remote": bool(remote),
        "requirements": reqs,
        "experience_level": experience_level or "unknown",
        "description": description or "",
    }


def _iter_jobs(conn: sqlite3.Connection, *,
               only_unscored_for: str | None = None,
               limit: int = _MAX_JOBS_PER_CALL):
    """Stream candidate jobs to score.

    With ``only_unscored_for`` set, returns only jobs the named user has no
    stored row for — used by the incremental scheduler refresh so we don't
    rescore every job every tick.
    """
    if only_unscored_for:
        sql = (
            "SELECT jp.id, jp.title, jp.location, jp.remote, jp.requirements_json, "
            "       jp.experience_level, jp.description "
            "FROM job_postings jp "
            "LEFT JOIN user_job_scores ujs "
            "  ON ujs.user_id = ? AND ujs.job_id = jp.id "
            "WHERE jp.deleted = 0 AND ujs.user_id IS NULL "
            "ORDER BY jp.last_seen_at DESC LIMIT ?"
        )
        params: tuple = (only_unscored_for, limit)
    else:
        sql = (
            "SELECT jp.id, jp.title, jp.location, jp.remote, jp.requirements_json, "
            "       jp.experience_level, jp.description "
            "FROM job_postings jp "
            "WHERE jp.deleted = 0 "
            "ORDER BY jp.last_seen_at DESC LIMIT ?"
        )
        params = (limit,)
    cur = conn.execute(sql, params)
    while True:
        rows = cur.fetchmany(_BATCH_SIZE)
        if not rows:
            return
        for r in rows:
            yield r


# ── Public API ───────────────────────────────────────────────────────────────

def score_jobs_for_user(conn: sqlite3.Connection, user_id: str,
                         profile: dict | None,
                         *, write_lock: threading.Lock | None = None,
                         only_new: bool = False,
                         max_jobs: int = _MAX_JOBS_PER_CALL,
                         progress: callable | None = None) -> dict:
    """Compute & persist scores for every (or every new) live job vs *profile*.

    Returns a small summary dict — useful for the dev console / logs.

    ``only_new=True`` is the fast path for the periodic scheduler tick:
    rows we already scored for this user stay put, only freshly-ingested
    jobs without a row get computed.
    """
    if not user_id or not profile:
        return {"scored": 0, "skipped_no_input": True}
    sig = profile_signature(profile)
    target_titles = [str(t) for t in (profile.get("target_titles") or []) if t]
    if not target_titles:
        # Fallback to work-experience titles (mirrors `_profile_for_search`).
        for bucket in ("work_experience", "experience", "research_experience"):
            for r in (profile.get(bucket) or []):
                t = (r.get("title") if isinstance(r, dict) else "") or ""
                t = str(t).strip()
                if t:
                    target_titles.append(t)
                if len(target_titles) >= 6:
                    break
            if len(target_titles) >= 6:
                break
    prof_loc = str(profile.get("location") or "")
    exp_prefs = profile.get("experience_levels") or []
    now = _utc_now()

    started = time.time()
    scored = 0
    skipped = 0
    pending: list[tuple] = []

    def _flush(rows: list[tuple]) -> None:
        if not rows:
            return
        sql = (
            "INSERT INTO user_job_scores "
            "(user_id, job_id, score, coverage, title_match, loc_sen, "
            " matched_json, missing_json, profile_hash, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, job_id) DO UPDATE SET "
            "  score = excluded.score, "
            "  coverage = excluded.coverage, "
            "  title_match = excluded.title_match, "
            "  loc_sen = excluded.loc_sen, "
            "  matched_json = excluded.matched_json, "
            "  missing_json = excluded.missing_json, "
            "  profile_hash = excluded.profile_hash, "
            "  computed_at = excluded.computed_at"
        )
        if write_lock is not None:
            with write_lock:
                with conn:
                    conn.executemany(sql, rows)
        else:
            with conn:
                conn.executemany(sql, rows)

    for row in _iter_jobs(conn,
                           only_unscored_for=(user_id if only_new else None),
                           limit=max_jobs):
        try:
            job = _row_to_job(row)
            cov, matched, missing = compute_skill_coverage(job, profile)
            tm = _title_match(target_titles, job["title"])
            loc = _location_match(prof_loc, job["location"], job["remote"])
            sen = _seniority_match(exp_prefs, job["experience_level"])
            loc_sen = (loc + sen) / 2.0
            pts = (
                RUBRIC_WEIGHTS["required_skills"] * cov
                + RUBRIC_WEIGHTS["industry"] * tm
                + RUBRIC_WEIGHTS["location_seniority"] * loc_sen
            )
            score_int = max(0, min(100, int(round(pts))))
            pending.append((
                user_id, job["id"], score_int,
                round(float(cov), 4), round(float(tm), 4), round(float(loc_sen), 4),
                json.dumps(matched[:6], ensure_ascii=False),
                json.dumps(missing[:6], ensure_ascii=False),
                sig, now,
            ))
            scored += 1
            if len(pending) >= _WRITE_BATCH_SIZE:
                _flush(pending)
                pending.clear()
                if progress:
                    try:
                        progress(scored)
                    except Exception:
                        pass
        except Exception:
            skipped += 1
            continue

    _flush(pending)
    elapsed = time.time() - started
    return {
        "scored": scored, "skipped": skipped, "profile_hash": sig,
        "elapsed_s": round(elapsed, 2), "only_new": only_new,
    }


def score_new_jobs_for_user(conn: sqlite3.Connection, user_id: str,
                             profile: dict | None,
                             *, write_lock: threading.Lock | None = None,
                             max_jobs: int = 5000) -> dict:
    """Score only jobs this user has no stored row for. Cheap to call on
    every scheduler tick."""
    return score_jobs_for_user(
        conn, user_id, profile,
        write_lock=write_lock, only_new=True, max_jobs=max_jobs,
    )


def delete_user_scores(conn: sqlite3.Connection, user_id: str,
                        *, write_lock: threading.Lock | None = None) -> int:
    """Forget every row for *user_id* — used by /api/reset and account
    deletion paths. Returns the deletion count."""
    if not user_id:
        return 0
    sql = "DELETE FROM user_job_scores WHERE user_id = ?"
    if write_lock is not None:
        with write_lock:
            with conn:
                cur = conn.execute(sql, (user_id,))
    else:
        with conn:
            cur = conn.execute(sql, (user_id,))
    return cur.rowcount or 0


def known_user_ids_with_scores(conn: sqlite3.Connection,
                                 *, limit: int = 500) -> list[str]:
    """Users with at least one stored score row. The periodic scheduler
    hook iterates these so we only refresh users who actually engaged
    with the feed."""
    rows = conn.execute(
        "SELECT user_id, MAX(computed_at) AS last "
        "FROM user_job_scores GROUP BY user_id "
        "ORDER BY last DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows if r and r[0]]

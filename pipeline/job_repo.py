"""
pipeline/job_repo.py
────────────────────
Persistent job-postings repository backed by the same SQLite file as the
session store. Exposes:

  init_schema(conn)              one-shot table + index + FTS5 + trigger setup
  upsert_many(conn, rows)        bulk insert/update by canonical URL
  mark_missing(conn, source, since)
                                 increment miss_count for rows not seen this run
  record_source_run(conn, ...)   append a row to source_runs
  bulk_get_by_ids(conn, ids)     fetch many rows by id

Schema lives next to the existing tables in
``output/jobs_ai_sessions.sqlite3``. Inference fields
(experience_level / education_required / citizenship_required) are computed
at ingest time; the JD body is intentionally NOT stored to keep the DB
under ~50 MB at scale.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


# ── URL canonicalization ──────────────────────────────────────────────────────

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_brand", "utm_referrer", "utm_name",
    "fbclid", "gclid", "msclkid", "yclid",
    "gh_src", "gh_jid", "ref", "src", "source", "lever-source",
    "_referrer", "tracking_id", "trk", "trkCampaign",
}


def canonical_url(url: str) -> str:
    """Lowercase host, strip tracking params, drop fragment + trailing slash.

    Returns "" for empty / unparseable input. We deliberately accept
    junk (no schema check) — the upsert path treats empty canonical
    URLs as a hard skip.
    """
    if not url:
        return ""
    u = url.strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    try:
        parts = urlparse(u)
    except ValueError:
        return ""
    netloc = (parts.netloc or "").lower()
    path = parts.path or ""
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    # strip tracking query params
    qs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
          if k.lower() not in _TRACKING_PARAMS]
    query = urlencode(qs, doseq=True)
    return urlunparse((parts.scheme.lower() or "https", netloc, path, "", query, ""))


def _job_id(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


_NORM_RE = re.compile(r"[^\w\s]")


def _norm(s: str) -> str:
    return _NORM_RE.sub("", (s or "").strip().lower())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS job_postings (
        id                   TEXT PRIMARY KEY,
        canonical_url        TEXT UNIQUE NOT NULL,
        source               TEXT NOT NULL,
        company              TEXT NOT NULL,
        company_norm         TEXT NOT NULL,
        title                TEXT NOT NULL,
        title_norm           TEXT NOT NULL,
        location             TEXT,
        remote               INTEGER NOT NULL DEFAULT 0,
        requirements_json    TEXT,
        salary_range         TEXT,
        experience_level     TEXT,
        education_required   TEXT,
        citizenship_required TEXT,
        job_category         TEXT,
        posted_at            TEXT,
        fetched_at           TEXT NOT NULL,
        last_seen_at         TEXT NOT NULL,
        miss_count           INTEGER NOT NULL DEFAULT 0,
        deleted              INTEGER NOT NULL DEFAULT 0
    )
    """,
    # Live migration for existing DBs that pre-date job_category.
    # SQLite ignores ALTER TABLE failures via the wrapping try below.
    "CREATE INDEX IF NOT EXISTS ix_jobs_seen     ON job_postings(deleted, last_seen_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_posted   ON job_postings(deleted, posted_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_filter   ON job_postings(deleted, experience_level, education_required, remote)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_company  ON job_postings(company_norm)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_source   ON job_postings(source)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_category ON job_postings(deleted, job_category)",
    # Standalone FTS5 (no content= link) so the triggers fully own the body.
    # Indexed columns are: title, company, requirements (joined bullets).
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS job_postings_fts USING fts5(
        title, company, requirements,
        tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS job_postings_ai AFTER INSERT ON job_postings BEGIN
        INSERT INTO job_postings_fts(rowid, title, company, requirements)
        VALUES (new.rowid, new.title, new.company, COALESCE(new.requirements_json, ''));
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS job_postings_ad AFTER DELETE ON job_postings BEGIN
        DELETE FROM job_postings_fts WHERE rowid = old.rowid;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS job_postings_au AFTER UPDATE ON job_postings BEGIN
        DELETE FROM job_postings_fts WHERE rowid = old.rowid;
        INSERT INTO job_postings_fts(rowid, title, company, requirements)
        VALUES (new.rowid, new.title, new.company, COALESCE(new.requirements_json, ''));
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS source_runs (
        source       TEXT NOT NULL,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        ok           INTEGER NOT NULL DEFAULT 0,
        fetched      INTEGER NOT NULL DEFAULT 0,
        inserted     INTEGER NOT NULL DEFAULT 0,
        updated      INTEGER NOT NULL DEFAULT 0,
        error        TEXT,
        PRIMARY KEY (source, started_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_source_runs_source ON source_runs(source, started_at DESC)",
]


def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotent — safe to call on every server boot."""
    for stmt in _SCHEMA_SQL:
        conn.execute(stmt)
    # Idempotent column additions for existing DBs that predate the field.
    try:
        conn.execute("ALTER TABLE job_postings ADD COLUMN job_category TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()


# ── Upsert ────────────────────────────────────────────────────────────────────

_UPSERT_SQL = """
INSERT INTO job_postings (
    id, canonical_url, source, company, company_norm, title, title_norm,
    location, remote, requirements_json, salary_range,
    experience_level, education_required, citizenship_required, job_category,
    posted_at, fetched_at, last_seen_at, miss_count, deleted
)
VALUES (
    :id, :canonical_url, :source, :company, :company_norm, :title, :title_norm,
    :location, :remote, :requirements_json, :salary_range,
    :experience_level, :education_required, :citizenship_required, :job_category,
    :posted_at, :fetched_at, :last_seen_at, 0, 0
)
ON CONFLICT(canonical_url) DO UPDATE SET
    source               = excluded.source,
    company              = excluded.company,
    company_norm         = excluded.company_norm,
    title                = excluded.title,
    title_norm           = excluded.title_norm,
    location             = COALESCE(excluded.location, job_postings.location),
    remote               = excluded.remote,
    requirements_json    = COALESCE(excluded.requirements_json, job_postings.requirements_json),
    salary_range         = CASE
        WHEN excluded.salary_range IS NOT NULL
         AND excluded.salary_range NOT IN ('', 'Unknown')
        THEN excluded.salary_range
        ELSE job_postings.salary_range
    END,
    experience_level     = excluded.experience_level,
    education_required   = excluded.education_required,
    citizenship_required = excluded.citizenship_required,
    job_category         = COALESCE(excluded.job_category, job_postings.job_category),
    posted_at            = COALESCE(excluded.posted_at, job_postings.posted_at),
    last_seen_at         = excluded.last_seen_at,
    miss_count           = 0,
    deleted              = 0
"""


def upsert_many(conn: sqlite3.Connection, rows: Iterable[Mapping]) -> tuple[int, int]:
    """Insert or refresh many rows. Returns (inserted_or_updated, skipped).

    Each ``row`` must carry at minimum:
        canonical_url, source, company, title.

    Optional fields default to None / 0 / "Unknown".
    Rows whose ``canonical_url`` is empty are skipped.
    """
    now = _utc_now()
    prepared: list[dict] = []
    skipped = 0
    for r in rows:
        url = canonical_url(r.get("canonical_url") or r.get("application_url") or r.get("url") or "")
        if not url:
            skipped += 1
            continue
        company = (r.get("company") or "").strip()
        title = (r.get("title") or "").strip()
        if not company or not title:
            skipped += 1
            continue
        reqs = r.get("requirements")
        if isinstance(reqs, (list, tuple)):
            reqs_json = json.dumps([str(x) for x in reqs if x])
        elif isinstance(reqs, str) and reqs.strip():
            reqs_json = json.dumps([reqs.strip()])
        else:
            reqs_json = None
        prepared.append({
            "id": _job_id(url),
            "canonical_url": url,
            "source": r.get("source") or "unknown",
            "company": company,
            "company_norm": _norm(company),
            "title": title,
            "title_norm": _norm(title),
            "location": r.get("location") or None,
            "remote": 1 if r.get("remote") else 0,
            "requirements_json": reqs_json,
            "salary_range": r.get("salary_range") or "Unknown",
            "experience_level": r.get("experience_level") or "unknown",
            "education_required": r.get("education_required") or "unknown",
            "citizenship_required": r.get("citizenship_required") or "unknown",
            "job_category": r.get("job_category") or "general",
            "posted_at": r.get("posted_at") or r.get("posted_date") or None,
            "fetched_at": now,
            "last_seen_at": now,
        })
    if not prepared:
        return (0, skipped)
    with conn:
        conn.executemany(_UPSERT_SQL, prepared)
    return (len(prepared), skipped)


# ── Soft-delete sweep ─────────────────────────────────────────────────────────

def mark_missing(conn: sqlite3.Connection, source: str, run_started_at: str,
                 deleted_threshold: int = 3) -> tuple[int, int]:
    """Bump miss_count for rows of this source that weren't refreshed this run.

    Rows reaching ``deleted_threshold`` flips the ``deleted`` flag. Returns
    ``(missed_now, soft_deleted_now)``.
    """
    with conn:
        cur = conn.execute(
            """UPDATE job_postings
               SET miss_count = miss_count + 1
               WHERE source = ? AND deleted = 0 AND last_seen_at < ?""",
            (source, run_started_at),
        )
        missed = cur.rowcount
        cur2 = conn.execute(
            """UPDATE job_postings
               SET deleted = 1
               WHERE source = ? AND deleted = 0 AND miss_count >= ?""",
            (source, deleted_threshold),
        )
        soft = cur2.rowcount
    return (missed, soft)


# ── Source-run telemetry ──────────────────────────────────────────────────────

def record_source_run(conn: sqlite3.Connection, *, source: str,
                      started_at: str, finished_at: str | None,
                      ok: bool, fetched: int = 0, inserted: int = 0,
                      updated: int = 0, error: str | None = None) -> None:
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO source_runs
               (source, started_at, finished_at, ok, fetched, inserted, updated, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, started_at, finished_at, 1 if ok else 0,
             int(fetched), int(inserted), int(updated), error),
        )


def latest_source_runs(conn: sqlite3.Connection) -> list[dict]:
    """One row per source — the most recent run."""
    rows = conn.execute(
        """SELECT source, started_at, finished_at, ok, fetched, inserted,
                  updated, error
             FROM source_runs sr
            WHERE started_at = (
                SELECT MAX(started_at) FROM source_runs WHERE source = sr.source
            )
            ORDER BY started_at DESC"""
    ).fetchall()
    return [
        {
            "source": r[0], "started_at": r[1], "finished_at": r[2],
            "ok": bool(r[3]), "fetched": r[4], "inserted": r[5],
            "updated": r[6], "error": r[7],
        }
        for r in rows
    ]


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """Single-row lookup by id from the persistent job_postings store.
    Returns None if no active row matches. Used by the per-job Ask Atlas
    endpoint when a session has no in-memory `scored`/`jobs` list to scan."""
    if not job_id:
        return None
    rows = bulk_get_by_ids(conn, [job_id])
    return rows[0] if rows else None


def bulk_get_by_ids(conn: sqlite3.Connection, ids: Sequence[str]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT id, canonical_url, source, company, title, location, remote,
                   requirements_json, salary_range, experience_level,
                   education_required, citizenship_required, posted_at,
                   last_seen_at
              FROM job_postings
             WHERE id IN ({placeholders}) AND deleted = 0""",
        list(ids),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def total_active(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM job_postings WHERE deleted = 0").fetchone()[0]


def _row_to_dict(r) -> dict:
    return {
        "id": r[0],
        "url": r[1],
        "source": r[2],
        "company": r[3],
        "title": r[4],
        "location": r[5] or "",
        "remote": bool(r[6]),
        "requirements": json.loads(r[7]) if r[7] else [],
        "salary_range": r[8] or "Unknown",
        "experience_level": r[9] or "unknown",
        "education_required": r[10] or "unknown",
        "citizenship_required": r[11] or "unknown",
        "posted_at": r[12],
        "last_seen_at": r[13],
    }

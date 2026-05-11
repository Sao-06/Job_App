"""
pipeline/migrations.py
──────────────────────
Idempotent SQLite schema migrations applied at every server boot.

Why this module exists
──────────────────────
Earlier the project sprinkled `try: ALTER TABLE…; except sqlite3.OperationalError: pass`
blocks across `session_store._init_db` and `job_repo.init_schema`. That pattern
silently swallowed REAL migration failures (not just "column already exists"), so
when `git pull` added a new column the Pi could end up with a stale schema
and no error in the journal — every INSERT would just bounce with
`sqlite3.OperationalError: no such column: …`.

The helpers below detect missing columns explicitly via PRAGMA, only ALTER
when the column is genuinely missing, and re-raise anything that's NOT a
benign duplicate-column race so failures surface in `journalctl -u jobapp`.

Workflow
────────
On every `git pull && systemctl restart jobapp` (or when the SQLiteSessionStore
is constructed in tests / CLI), `apply_all_migrations(conn)` runs and brings
the on-disk schema up to the version the code expects. Add a new column
exactly once here — never inline a fresh `try/except: pass` block in
session_store or job_repo again.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any


# Per-process accumulator of what got applied this boot. Surfaced by
# `app.py`'s startup hook so the journalctl tail tells you whether a
# migration actually ran.
_LOG: list[str] = []


def _log(msg: str) -> None:
    _LOG.append(msg)
    # stderr so systemd captures it into journalctl regardless of stdout buffering.
    print(f"[migrations] {msg}", file=sys.stderr, flush=True)


def get_log() -> list[str]:
    return list(_LOG)


def reset_log() -> None:
    _LOG.clear()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True iff the named column is present in the table schema. Returns
    False (not raises) if the table itself doesn't exist — callers can
    treat 'no table yet' the same as 'no column yet'.
    """
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return False
    # PRAGMA table_info returns: (cid, name, type, notnull, dflt_value, pk)
    return any(row[1] == column for row in rows)


def ensure_column(conn: sqlite3.Connection, table: str, column: str,
                  type_decl: str, *, default: Any = None) -> bool:
    """Add `column` to `table` if it doesn't already exist.

    Returns True iff the column is present after this call (either pre-existing
    or freshly added). Returns False if the table itself doesn't exist (caller
    must run CREATE TABLE first — usually via the constants in `_SCHEMA_SQL`).

    Raises sqlite3.Error if the ALTER fails for any reason OTHER than a
    duplicate-column race. The caller MUST NOT swallow that exception;
    surfacing it in journalctl is the whole point of this module.
    """
    if not table_exists(conn, table):
        _log(f"skip add {table}.{column} — table does not exist yet (run CREATE first)")
        return False
    if column_exists(conn, table, column):
        return True
    sql = f"ALTER TABLE {table} ADD COLUMN {column} {type_decl}"
    if default is not None:
        if isinstance(default, str):
            sql += f" DEFAULT {default!r}"
        else:
            sql += f" DEFAULT {default}"
    try:
        conn.execute(sql)
        _log(f"added column {table}.{column} ({type_decl})")
        return True
    except sqlite3.OperationalError as exc:
        # Duplicate-column race: a concurrent writer beat us between our
        # PRAGMA check and the ALTER. Treat as success.
        if "duplicate column" in str(exc).lower():
            return True
        _log(f"FAILED adding {table}.{column}: {type(exc).__name__}: {exc}")
        raise


def ensure_index(conn: sqlite3.Connection, index_name: str, table: str,
                 columns: str) -> bool:
    """`CREATE INDEX IF NOT EXISTS` with logging. Idempotent."""
    if not table_exists(conn, table):
        _log(f"skip index {index_name} — table {table} does not exist yet")
        return False
    try:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({columns})")
        return True
    except sqlite3.OperationalError as exc:
        _log(f"FAILED creating index {index_name}: {type(exc).__name__}: {exc}")
        raise


# ── The canonical migration list ─────────────────────────────────────────────
# Order matters only when later migrations depend on earlier ones (e.g. an
# index that references a freshly-added column). Otherwise additions can go
# in any order.
#
# To add a new column / index in the future:
#   1. Add an `ensure_column(...)` line below.
#   2. Update the corresponding CREATE TABLE in session_store / job_repo so
#      a fresh DB also gets the column at create time.
#   3. Push. On `git pull && systemctl restart jobapp` the migration runs
#      automatically — no manual SQL needed.

def apply_all_migrations(conn: sqlite3.Connection) -> list[str]:
    """Bring `conn`'s schema up to the version the code expects.

    Assumes `_SCHEMA_SQL` (CREATE TABLE statements) has already been run by
    the caller. This function only handles the column / index additions
    that may be missing on older DBs.

    Returns the human-readable log of actions taken this run (also printed
    to stderr so they show up in journalctl).
    """
    reset_log()

    # users table additions over the project's history
    ensure_column(conn, "users", "is_developer",
                  "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "users", "plan_tier",
                  "TEXT NOT NULL DEFAULT 'pro'")
    ensure_column(conn, "users", "stripe_customer_id", "TEXT")
    ensure_column(conn, "users", "stripe_subscription_id", "TEXT")
    ensure_index(conn, "ix_users_stripe_customer", "users",
                 "stripe_customer_id")

    # Testing-phase: everyone is Pro. Promote any pre-existing 'free' rows
    # AND refresh the cached plan_tier on every active auth_token so the
    # next /api/state poll reflects the bump without a re-login (the cached
    # user_json in auth_tokens otherwise wins over the live users-row read).
    promoted = conn.execute(
        "UPDATE users SET plan_tier = 'pro' WHERE plan_tier != 'pro'"
    ).rowcount
    if promoted:
        _log(f"promoted {promoted} user(s) to pro (testing phase)")
        rows = conn.execute("SELECT token, user_json FROM auth_tokens").fetchall()
        for token, user_json in rows:
            try:
                payload = json.loads(user_json or "{}")
            except (TypeError, ValueError):
                payload = {}
            if payload.get("plan_tier") != "pro":
                payload["plan_tier"] = "pro"
                conn.execute(
                    "UPDATE auth_tokens SET user_json = ? WHERE token = ?",
                    (json.dumps(payload), token),
                )

    # job_postings — added the cross-industry category label after launch.
    # The matching index lives here too: CREATE INDEX validates the referenced
    # column exists, so it MUST run after the ensure_column above (and not in
    # _SCHEMA_SQL where it would fire too early on stale DBs).
    ensure_column(conn, "job_postings", "job_category", "TEXT")
    ensure_index(conn, "ix_jobs_category", "job_postings",
                 "deleted, job_category")

    # job_postings.description — full posting body (HTML stripped, plain text).
    # Empty / NULL on rows whose source doesn't expose a description in the
    # listing response; lazy-filled by Phase 3 LLM-scoring + the SPA detail
    # view via pipeline.job_details. ~3-5 KB per row when populated; bounded
    # naturally by how much of the index ever gets scored or viewed.
    ensure_column(conn, "job_postings", "description", "TEXT")

    conn.commit()
    return get_log()

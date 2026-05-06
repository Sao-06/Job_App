#!/usr/bin/env python3
"""
scripts/migrate_db.py
─────────────────────
Standalone schema-migration runner.

When to use this directly (instead of just restarting the service):
  • You just `git pull`-ed and want to verify the migration applied
    cleanly BEFORE bouncing the service.
  • The systemd unit is wedged and you want to fix the schema first.
  • You're inspecting the migration log without scrolling journalctl.

Usage:
  cd ~/Job_App && source venv/bin/activate
  python scripts/migrate_db.py                  # run migrations + print log
  python scripts/migrate_db.py --check          # just print current schema state
  python scripts/migrate_db.py --db /path/to/jobs_ai_sessions.sqlite3

The script imports `pipeline.migrations.apply_all_migrations`, so the source
of truth for what migrations exist is always pipeline/migrations.py — never
copy migrations into this script directly.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Make the pipeline package importable when running from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _default_db_path() -> Path:
    """Same resolution rule as `pipeline.config.DB_PATH`. Hardcoding it here
    would silently drift; importing keeps them in lockstep.
    """
    from pipeline.config import DB_PATH
    return Path(DB_PATH)


def _print_schema_summary(conn: sqlite3.Connection) -> None:
    """Show every table + columns so the operator can see whether the
    migration actually landed. No-ops on a fresh DB.
    """
    tables = [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    if not tables:
        print("  (no tables — fresh DB)")
        return
    for table in tables:
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        print(f"  {table:<22} ({len(cols)} cols): {', '.join(cols)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=None,
                        help="Path to jobs_ai_sessions.sqlite3 (default: pipeline.config.DB_PATH)")
    parser.add_argument("--check", action="store_true",
                        help="Only print the current schema; do not apply migrations")
    args = parser.parse_args()

    db_path = args.db or _default_db_path()
    if not db_path.exists():
        print(f"[migrate_db] DB does not exist at {db_path} — nothing to do.")
        print("  (a fresh DB will be created automatically the next time the app starts)")
        return 0

    print(f"[migrate_db] target: {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        if args.check:
            print("[migrate_db] --check requested — schema dump only:")
            _print_schema_summary(conn)
            return 0

        print("[migrate_db] schema BEFORE:")
        _print_schema_summary(conn)

        # `init_schema` runs CREATE TABLE statements (no-op on existing
        # tables) and then internally invokes `apply_all_migrations` —
        # which adds any missing columns / indexes via PRAGMA detection.
        # We only need the one call; reading get_log() afterwards tells
        # us what actually happened. Calling apply_all_migrations a second
        # time would correctly return 0 steps (idempotent), but the report
        # would mislead.
        from pipeline.job_repo import init_schema as _init_jobs_schema
        from pipeline.migrations import get_log

        _init_jobs_schema(conn)
        applied = get_log()

        print(f"[migrate_db] migrations applied this run: {len(applied)} step(s)")
        for line in applied:
            print(f"  · {line}")

        print("[migrate_db] schema AFTER:")
        _print_schema_summary(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

import copy
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_token(token: str) -> str:
    """One-way digest of a session bearer token. Stored in
    ``auth_tokens.token`` so a DB read alone is *not* sufficient to
    impersonate a user — the attacker would still need to crack
    SHA-256, which (for unguessable 32+ byte secrets) is infeasible.
    """
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def json_default(value: Any):
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _coerce_int_keyed_dict(value) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {}
    for k, v in value.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


def normalize_state(state: dict) -> dict:
    normalized = copy.deepcopy(state)
    for key in ("done", "liked_ids", "hidden_ids", "extracting_ids"):
        raw = normalized.get(key)
        if isinstance(raw, (list, tuple, set)):
            normalized[key] = set(raw)
        else:
            normalized[key] = set()
    normalized["error"] = _coerce_int_keyed_dict(normalized.get("error"))
    normalized["elapsed"] = _coerce_int_keyed_dict(normalized.get("elapsed"))
    return normalized


class SQLiteSessionStore:
    def __init__(self, db_path: Path, default_state_factory):
        self.db_path = Path(db_path)
        self.default_state_factory = default_state_factory
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Detect a malformed file BEFORE we let the rest of the schema /
        # ingestion layer touch it. A corrupted DB can otherwise cause
        # every Phase-2 search and every /api/state poll to crash with
        # "database disk image is malformed" — far worse than rebuilding.
        if self.db_path.exists():
            self._quarantine_if_corrupt()
        self._init_db()

    def _quarantine_if_corrupt(self) -> None:
        """If the on-disk DB fails ``PRAGMA quick_check``, move it aside
        with a ``.corrupt-<timestamp>`` suffix (alongside its WAL/SHM
        siblings) so the next ``_init_db`` rebuilds a fresh schema.

        Recovery cost: users + auth tokens + session state are gone (users
        re-sign-in; resume profiles re-extract on next upload). The job
        index re-fills from sources within a few minutes of boot. That's
        acceptable vs. the entire app being unusable.
        """
        try:
            probe = sqlite3.connect(self.db_path, timeout=2)
        except sqlite3.DatabaseError:
            self._move_aside("open_failed")
            return
        try:
            try:
                row = probe.execute("PRAGMA quick_check").fetchone()
            except sqlite3.DatabaseError as exc:
                # quick_check itself can raise on severe corruption.
                self._move_aside(f"quick_check_raised_{type(exc).__name__}")
                return
            if not row or str(row[0]).strip().lower() != "ok":
                self._move_aside(f"quick_check={row}")
                return
        finally:
            try:
                probe.close()
            except Exception:
                pass

    def _move_aside(self, reason: str) -> None:
        """Rename the malformed DB file (and its WAL/SHM siblings) so a
        fresh schema can be rebuilt. The original file is preserved with
        a ``.corrupt-<unix_ts>`` suffix in case manual forensics is
        wanted later. NEVER deletes — that's the user's call.
        """
        from datetime import datetime as _dt
        stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
        suffix = f".corrupt-{stamp}"
        try:
            print(
                f"[session_store] {self.db_path} is malformed ({reason}); "
                f"quarantining as *{suffix} and rebuilding from scratch."
            )
        except Exception:
            pass
        for tail in ("", "-wal", "-shm", "-journal"):
            old = self.db_path.with_name(self.db_path.name + tail)
            if not old.exists():
                continue
            target = old.with_name(old.name + suffix)
            try:
                old.rename(target)
            except OSError:
                # File still locked by a sibling process; best-effort try
                # to copy + truncate.
                try:
                    import shutil
                    shutil.copy2(old, target)
                    old.unlink()
                except Exception:
                    pass

    def _connect(self):
        # 60 s busy timeout — `pipeline.ingest._WRITE_LOCK` already serializes
        # ingest writes, but request-thread writes (session_state save, auth
        # token refresh) can still contend with an in-flight ingest upsert on
        # the Pi's slow IO. Doubling the budget from 30 s gives the Python-
        # level lock + SQLite-level wait room to absorb the worst case
        # (~200-row executemany) without surfacing "database is locked".
        conn = sqlite3.connect(self.db_path, timeout=60)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # Some Windows workspaces reject journal-mode changes; keep the DB usable.
            pass
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT,
                    google_id TEXT UNIQUE,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_state (
                    session_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    user_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            # All idempotent column / index additions delegate to the central
            # migrations module. It uses PRAGMA table_info to detect missing
            # columns explicitly and re-raises real failures (instead of the
            # old `try: ALTER…; except: pass` pattern that silently hid the
            # job_category migration when it failed on the Pi). See
            # pipeline/migrations.py.
            from pipeline.migrations import apply_all_migrations
            apply_all_migrations(conn)

            # One-shot purge of any auth_tokens rows that pre-date the
            # SHA-256 hashing migration. Existing rows store the raw bearer
            # cookie verbatim, which would still allow impersonation if
            # someone had already exfiltrated them. Force a re-login by
            # nuking those rows. (Safe: the purge keys off the row
            # length — hashed digests are exactly 64 hex chars, so any
            # row that doesn't match that pattern is legacy raw-token
            # storage.)
            try:
                conn.execute(
                    "DELETE FROM auth_tokens "
                    "WHERE LENGTH(token) != 64 "
                    "   OR token GLOB '*[!0-9a-f]*'"
                )
            except sqlite3.OperationalError:
                pass
            # Job postings + FTS5 + source_runs live in the same DB so the
            # ingestion worker doesn't need a second writer connection.
            # If the jobs schema fails to initialize, LOG the exception
            # explicitly — do not let the failure vanish silently the way it
            # did pre-migrations refactor. Auth/session tables still come up.
            try:
                from pipeline.job_repo import init_schema as _init_jobs_schema
                _init_jobs_schema(conn)
            except Exception as exc:
                import sys, traceback
                print(
                    f"[session_store] WARNING: pipeline.job_repo.init_schema "
                    f"failed — jobs feed may be broken: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr, flush=True,
                )
                traceback.print_exc(file=sys.stderr)
                # Re-raise in DEBUG mode would be friendlier, but auth must
                # still come up so users can sign in even if jobs are broken.

    def connect(self) -> sqlite3.Connection:
        """Public accessor for the ingestion worker; mirrors `_connect`."""
        return self._connect()

    def create_user(self, email: str, password_hash: str = None, google_id: str = None) -> str:
        user_id = uuid.uuid4().hex
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, password_hash, google_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, email, password_hash, google_id, now),
            )
        return user_id

    # Canonical column order for every user-row SELECT. Keep this in sync with
    # _user_row_to_dict — every caller of get_user_by_* must SELECT in this order.
    _USER_COLUMNS = (
        "id, email, password_hash, google_id, is_developer, plan_tier, "
        "stripe_customer_id, stripe_subscription_id"
    )

    def _user_row_to_dict(self, row) -> dict:
        return {
            "id": row[0],
            "email": row[1],
            "password_hash": row[2],
            "google_id": row[3],
            "is_developer": bool(row[4]) if len(row) > 4 else False,
            "plan_tier": (row[5] if len(row) > 5 and row[5] else "free"),
            "stripe_customer_id": row[6] if len(row) > 6 else None,
            "stripe_subscription_id": row[7] if len(row) > 7 else None,
        }

    def get_user_by_email(self, email: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._USER_COLUMNS} FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            if row:
                return self._user_row_to_dict(row)
        return None

    def get_user_by_google_id(self, google_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._USER_COLUMNS} FROM users WHERE google_id = ?",
                (google_id,),
            ).fetchone()
            if row:
                return self._user_row_to_dict(row)
        return None

    def get_user_by_id(self, user_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._USER_COLUMNS} FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row:
                return self._user_row_to_dict(row)
        return None

    def get_user_by_stripe_customer(self, customer_id: str) -> dict | None:
        """Lookup used by the Stripe webhook. Returns None when no user is
        linked to the given customer (e.g. a customer that was created but
        never went through Checkout, or a webhook for a deleted account)."""
        if not customer_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._USER_COLUMNS} FROM users WHERE stripe_customer_id = ?",
                (customer_id,),
            ).fetchone()
            if row:
                return self._user_row_to_dict(row)
        return None

    def set_user_developer(self, user_id: str, is_developer: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET is_developer = ? WHERE id = ?",
                (1 if is_developer else 0, user_id),
            )

    def set_user_plan_tier(self, user_id: str, tier: str) -> None:
        if tier not in ("free", "pro"):
            raise ValueError(f"invalid plan tier: {tier}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET plan_tier = ? WHERE id = ?",
                (tier, user_id),
            )

    def set_user_stripe_customer(self, user_id: str, customer_id: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
                (customer_id, user_id),
            )

    def set_user_subscription(self, user_id: str, subscription_id: str | None,
                              tier: str | None = None) -> None:
        """Persist subscription_id and (optionally) plan_tier in one round trip.

        Pass ``tier=None`` to leave plan_tier untouched — used when a webhook
        only needs to record the subscription_id (e.g. on
        ``checkout.session.completed`` before the subscription is active).
        """
        if tier is not None and tier not in ("free", "pro"):
            raise ValueError(f"invalid plan tier: {tier}")
        with self._connect() as conn:
            if tier is None:
                conn.execute(
                    "UPDATE users SET stripe_subscription_id = ? WHERE id = ?",
                    (subscription_id, user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET stripe_subscription_id = ?, plan_tier = ? WHERE id = ?",
                    (subscription_id, tier, user_id),
                )

    def refresh_user_plan_in_tokens(self, user_id: str, tier: str) -> int:
        """Update the cached plan_tier in every active auth_token for this user.

        Without this, a user who just upgraded would still see ``plan_tier='free'``
        in ``/api/state`` until they signed out and back in (the auth token's
        cached user_json wins over the live users-row read inside ``get_auth_user``).
        Returns the number of token rows touched.
        """
        if tier not in ("free", "pro"):
            raise ValueError(f"invalid plan tier: {tier}")
        touched = 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT token, user_json FROM auth_tokens WHERE user_id = ?", (user_id,)
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row[1] or "{}")
                except (TypeError, ValueError):
                    payload = {}
                payload["plan_tier"] = tier
                conn.execute(
                    "UPDATE auth_tokens SET user_json = ? WHERE token = ?",
                    (json.dumps(payload), row[0]),
                )
                touched += 1
        return touched

    def list_users(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._USER_COLUMNS}, created_at "
                "FROM users ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {**self._user_row_to_dict(row), "created_at": row[8] if len(row) > 8 else None}
                for row in rows
            ]

    def create_auth_token(self, token: str, user_id: str, user_payload: dict) -> None:
        if not token:
            return
        digest = _hash_token(token)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_tokens (token, user_id, user_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(token) DO UPDATE SET
                    user_id = excluded.user_id,
                    user_json = excluded.user_json,
                    created_at = excluded.created_at
                """,
                (digest, user_id, json.dumps(user_payload), utc_now()),
            )

    def get_auth_user(self, token: str) -> dict | None:
        if not token:
            return None
        digest = _hash_token(token)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, user_json FROM auth_tokens WHERE token = ?",
                (digest,),
            ).fetchone()
            if not row:
                return None
            try:
                payload = json.loads(row[1])
            except (TypeError, ValueError):
                payload = {}
            payload["id"] = row[0]
            return payload

    def delete_auth_token(self, token: str) -> None:
        if not token:
            return
        digest = _hash_token(token)
        with self._connect() as conn:
            conn.execute("DELETE FROM auth_tokens WHERE token = ?", (digest,))

    def associate_session_with_user(self, session_id: str, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET user_id = ? WHERE id = ?",
                (user_id, session_id),
            )

    def get_session_user_id(self, session_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row:
                return row[0]
        return None

    def get_user_sessions(self, user_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
            return [row[0] for row in rows]

    def peek_state(self, session_id: str) -> dict | None:
        """Read-only state load. Returns None if no row exists. Use this for
        anonymous sessions so we don't INSERT a session_state row that would
        later show up as a 'ghost' user in the Dev Ops console."""
        if not session_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                return normalize_state(json.loads(row[0]))
        return None

    def get_state(self, session_id: str) -> dict:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (session_id, now, now),
            )
            row = conn.execute(
                "SELECT state_json FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                return normalize_state(json.loads(row[0]))

            state = self.default_state_factory()
            conn.execute(
                """
                INSERT INTO session_state (session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (session_id, json.dumps(state, default=json_default), now),
            )
            return normalize_state(state)

    def save_state(self, session_id: str, state: dict) -> None:
        now = utc_now()
        payload = json.dumps(state, default=json_default)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (session_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO session_state (session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (session_id, payload, now),
            )

    def delete_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def reset_state(self, session_id: str) -> dict:
        state = self.default_state_factory()
        self.save_state(session_id, state)
        return normalize_state(state)

    def list_sessions(self, limit: int = 200) -> list[dict]:
        # Only return sessions tied to a real authenticated user. Anonymous
        # sessions don't get an INSERT anymore (see peek_state), but legacy
        # rows from before that change can still exist — filter them out.
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.created_at, s.updated_at, ss.state_json,
                       s.user_id, u.email AS u_email, u.plan_tier, u.is_developer
                FROM sessions s
                LEFT JOIN session_state ss ON ss.session_id = s.id
                LEFT JOIN users u ON u.id = s.user_id
                WHERE s.user_id IS NOT NULL
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        sessions = []
        for row in rows:
            session_id, created_at, updated_at, state_json = row[0], row[1], row[2], row[3]
            user_id, u_email, plan_tier, u_is_dev = row[4], row[5], row[6], row[7]
            state = normalize_state(json.loads(state_json)) if state_json else self.default_state_factory()
            profile = state.get("profile") or {}
            scored = state.get("scored") or []
            applications = state.get("applications") or []
            feedback = state.get("feedback") or []
            sessions.append(
                {
                    "id": session_id,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "name": profile.get("name") or "Unprofiled user",
                    "email": u_email or profile.get("email") or "",
                    "user_id": user_id,
                    "plan_tier": plan_tier or ("free" if user_id else None),
                    "is_developer": bool(u_is_dev) if u_is_dev is not None else False,
                    "has_resume": bool(state.get("resume_text")),
                    "resume_filename": state.get("resume_filename") or "",
                    "mode": state.get("mode", "demo"),
                    "done": sorted(state.get("done") or []),
                    "errors": state.get("error") or {},
                    "job_count": len(state.get("jobs") or []),
                    "scored_count": len(scored),
                    "application_count": len(applications),
                    "applied_count": sum(1 for app in applications if app.get("status") == "Applied"),
                    "manual_count": sum(1 for app in applications if app.get("status") == "Manual Required"),
                    "target": state.get("job_titles") or "",
                    "location": state.get("location") or "",
                    "feedback_count": len(feedback),
                    "unread_feedback_count": sum(1 for f in feedback if not f.get("read")),
                }
            )
        return sessions

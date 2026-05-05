import copy
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
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
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN is_developer INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN plan_tier TEXT NOT NULL DEFAULT 'free'"
                )
            except sqlite3.OperationalError:
                pass
            # Job postings + FTS5 + source_runs live in the same DB so the
            # ingestion worker doesn't need a second writer connection.
            try:
                from pipeline.job_repo import init_schema as _init_jobs_schema
                _init_jobs_schema(conn)
            except Exception:
                # Don't let a job-schema migration failure block the auth/session
                # tables from coming up.
                pass

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

    def _user_row_to_dict(self, row) -> dict:
        return {
            "id": row[0],
            "email": row[1],
            "password_hash": row[2],
            "google_id": row[3],
            "is_developer": bool(row[4]) if len(row) > 4 else False,
            "plan_tier": (row[5] if len(row) > 5 and row[5] else "free"),
        }

    def get_user_by_email(self, email: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, google_id, is_developer, plan_tier FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            if row:
                return self._user_row_to_dict(row)
        return None

    def get_user_by_google_id(self, google_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, google_id, is_developer, plan_tier FROM users WHERE google_id = ?",
                (google_id,),
            ).fetchone()
            if row:
                return self._user_row_to_dict(row)
        return None

    def get_user_by_id(self, user_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, google_id, is_developer, plan_tier FROM users WHERE id = ?",
                (user_id,),
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

    def list_users(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, email, password_hash, google_id, is_developer, plan_tier, created_at "
                "FROM users ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {**self._user_row_to_dict(row), "created_at": row[6] if len(row) > 6 else None}
                for row in rows
            ]

    def create_auth_token(self, token: str, user_id: str, user_payload: dict) -> None:
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
                (token, user_id, json.dumps(user_payload), utc_now()),
            )

    def get_auth_user(self, token: str) -> dict | None:
        if not token:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, user_json FROM auth_tokens WHERE token = ?",
                (token,),
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
        with self._connect() as conn:
            conn.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))

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
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.created_at, s.updated_at, ss.state_json,
                       s.user_id, u.email AS u_email, u.plan_tier, u.is_developer
                FROM sessions s
                LEFT JOIN session_state ss ON ss.session_id = s.id
                LEFT JOIN users u ON u.id = s.user_id
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

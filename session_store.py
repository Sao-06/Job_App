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


def normalize_state(state: dict) -> dict:
    normalized = copy.deepcopy(state)
    normalized["done"] = set(normalized.get("done") or [])
    normalized["liked_ids"] = set(normalized.get("liked_ids") or [])
    normalized["hidden_ids"] = set(normalized.get("hidden_ids") or [])
    normalized["error"] = {int(k): v for k, v in (normalized.get("error") or {}).items()}
    normalized["elapsed"] = {int(k): v for k, v in (normalized.get("elapsed") or {}).items()}
    return normalized


class SQLiteSessionStore:
    def __init__(self, db_path: Path, default_state_factory):
        self.db_path = Path(db_path)
        self.default_state_factory = default_state_factory
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
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

    def get_user_by_email(self, email: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, google_id FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            if row:
                return {
                    "id": row[0],
                    "email": row[1],
                    "password_hash": row[2],
                    "google_id": row[3],
                }
        return None

    def get_user_by_google_id(self, google_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, google_id FROM users WHERE google_id = ?",
                (google_id,),
            ).fetchone()
            if row:
                return {
                    "id": row[0],
                    "email": row[1],
                    "password_hash": row[2],
                    "google_id": row[3],
                }
        return None

    def get_user_by_id(self, user_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, google_id FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row:
                return {
                    "id": row[0],
                    "email": row[1],
                    "password_hash": row[2],
                    "google_id": row[3],
                }
        return None

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
                SELECT s.id, s.created_at, s.updated_at, ss.state_json
                FROM sessions s
                LEFT JOIN session_state ss ON ss.session_id = s.id
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        sessions = []
        for session_id, created_at, updated_at, state_json in rows:
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
                    "email": profile.get("email") or "",
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

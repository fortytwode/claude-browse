"""Persistent user-owned work queue stored beside Agent Board session state."""

from __future__ import annotations

import re
import time
import uuid
from datetime import date

from claude_browse.board import projects, store

STATUSES = ("todo", "waiting", "done")
PROVIDERS = ("claude", "codex")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    task_id          TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    project_key      TEXT NOT NULL,
    project_name     TEXT NOT NULL,
    project_path     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'todo',
    due_date         TEXT,
    session_id       TEXT UNIQUE,
    session_provider TEXT,
    notes            TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    completed_at     REAL
)
"""


def _conn():
    conn = store.get_conn()
    conn.execute(_SCHEMA)
    return conn


def _text(value: object, field: str, *, limit: int, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return result


def _due(value: object) -> str | None:
    result = str(value or "").strip()
    if not result:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", result):
        raise ValueError("due_date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(result)
    except ValueError as exc:
        raise ValueError("due_date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != result:
        raise ValueError("due_date must be YYYY-MM-DD")
    return parsed.isoformat()


def _status(value: object) -> str:
    result = str(value or "todo").strip().lower()
    if result not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    return result


def _provider(value: object) -> str:
    result = str(value or "claude").strip().lower()
    if result not in PROVIDERS:
        raise ValueError(f"provider must be one of: {', '.join(PROVIDERS)}")
    return result


def create(
    *,
    title: object,
    project_path: object = "",
    status: object = "todo",
    due_date: object = None,
    session_id: object = None,
    provider: object = "claude",
    notes: object = "",
) -> dict:
    clean_title = _text(title, "title", limit=500, required=True)
    clean_path = _text(project_path, "project_path", limit=4096)
    project = projects.resolve_project(clean_path or None)
    clean_session = _text(session_id, "session_id", limit=500) or None
    clean_status = _status(status)
    clean_provider = _provider(provider)
    clean_due = _due(due_date)
    clean_notes = _text(notes, "notes", limit=4000)
    now = time.time()
    task_id = str(uuid.uuid4())
    completed_at = now if clean_status == "done" else None
    with _conn() as conn:
        try:
            conn.execute(
                """INSERT INTO work_items
                   (task_id, title, project_key, project_name, project_path,
                    status, due_date, session_id, session_provider, notes,
                    created_at, updated_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    clean_title,
                    project["key"],
                    project["name"],
                    project["path"],
                    clean_status,
                    clean_due,
                    clean_session,
                    clean_provider,
                    clean_notes,
                    now,
                    now,
                    completed_at,
                ),
            )
        except Exception as exc:
            if "UNIQUE constraint failed: work_items.session_id" in str(exc):
                raise ValueError("this thread is already in the work queue") from exc
            raise
    return get(task_id)  # type: ignore[return-value]


def get(task_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM work_items WHERE task_id = ?", (task_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def get_for_session(session_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM work_items WHERE session_id = ?", (session_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def list_items(*, include_done: bool = False) -> list[dict]:
    where = "" if include_done else "WHERE status != 'done'"
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT * FROM work_items {where}
                ORDER BY
                  CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
                  due_date,
                  updated_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def update(task_id: str, **changes: object) -> dict | None:
    allowed = {"title", "status", "due_date", "notes", "provider", "project_path"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    if not changes:
        return get(task_id)
    existing = get(task_id)
    if existing is None:
        return None
    if "provider" in changes and existing.get("session_id"):
        raise ValueError("provider cannot change while a thread is linked")

    values: dict[str, object] = {}
    if "title" in changes:
        values["title"] = _text(changes["title"], "title", limit=500, required=True)
    if "status" in changes:
        values["status"] = _status(changes["status"])
        values["completed_at"] = time.time() if values["status"] == "done" else None
    if "due_date" in changes:
        values["due_date"] = _due(changes["due_date"])
    if "notes" in changes:
        values["notes"] = _text(changes["notes"], "notes", limit=4000)
    if "provider" in changes:
        values["session_provider"] = _provider(changes["provider"])
    if "project_path" in changes:
        clean_path = _text(changes["project_path"], "project_path", limit=4096)
        project = projects.resolve_project(clean_path or None)
        values.update(
            project_key=project["key"],
            project_name=project["name"],
            project_path=project["path"],
        )
    values["updated_at"] = time.time()

    assignments = ", ".join(f"{field} = ?" for field in values)
    with _conn() as conn:
        cursor = conn.execute(
            f"UPDATE work_items SET {assignments} WHERE task_id = ?",
            (*values.values(), task_id),
        )
    return get(task_id) if cursor.rowcount else None


def attach_session(task_id: str, session_id: str, provider: str) -> dict | None:
    """Link the session created by a queued task's Start action."""
    clean_session = _text(session_id, "session_id", limit=500, required=True)
    clean_provider = _provider(provider)
    with _conn() as conn:
        cursor = conn.execute(
            "UPDATE work_items SET session_id = ?, session_provider = ?, updated_at = ? "
            "WHERE task_id = ?",
            (clean_session, clean_provider, time.time(), task_id),
        )
    return get(task_id) if cursor.rowcount else None

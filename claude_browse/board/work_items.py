"""Persistent user-owned work queue stored beside Agent Board session state."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import date
from pathlib import Path

from claude_browse.board import projects, store

STATUSES = ("active", "done", "archived")
PROVIDERS = ("claude", "codex")
_PROTOTYPE_TASK_ID = "0b001368-52a5-4368-8638-bf7b79670851"
_MIGRATION_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    task_id          TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    project_key      TEXT NOT NULL,
    project_name     TEXT NOT NULL,
    project_path     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    due_date         TEXT,
    session_id       TEXT UNIQUE,
    session_provider TEXT,
    notes            TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    completed_at     REAL,
    title_override   TEXT,
    title_source     TEXT NOT NULL DEFAULT 'automatic',
    session_cwd      TEXT
)
"""


def migration_backup_path() -> Path:
    return Path(f"{store._DB_PATH}.pre-work-overlay.bak")


def _backup_database(conn: sqlite3.Connection) -> None:
    backup_path = migration_backup_path()
    if backup_path.exists():
        return
    conn.commit()
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(backup_path)
    try:
        conn.backup(destination)
    finally:
        destination.close()


def _migrate_legacy_rows(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE work_items SET title_override = title, title_source = 'manual' "
        "WHERE session_id IS NOT NULL AND title_override IS NULL"
    )
    conn.execute(
        "UPDATE work_items SET status = 'active' WHERE status IN ('todo', 'waiting')"
    )
    conn.execute(
        "UPDATE work_items SET session_cwd = COALESCE("
        "(SELECT sessions.cwd FROM sessions WHERE sessions.session_id = work_items.session_id), "
        "project_path) WHERE session_id IS NOT NULL AND session_cwd IS NULL"
    )
    conn.execute("DELETE FROM work_items WHERE task_id = ?", (_PROTOTYPE_TASK_ID,))


def _migrate_overlay(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(work_items)").fetchall()
    }
    if {"title_override", "title_source", "session_cwd"} <= columns:
        return
    _backup_database(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "title_override" not in columns:
            conn.execute("ALTER TABLE work_items ADD COLUMN title_override TEXT")
        if "title_source" not in columns:
            conn.execute(
                "ALTER TABLE work_items ADD COLUMN title_source TEXT "
                "NOT NULL DEFAULT 'automatic'"
            )
        if "session_cwd" not in columns:
            conn.execute("ALTER TABLE work_items ADD COLUMN session_cwd TEXT")
        _migrate_legacy_rows(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _conn():
    conn = store.get_conn()
    with _MIGRATION_LOCK:
        conn.execute(_SCHEMA)
        _migrate_overlay(conn)
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
    result = str(value or "active").strip().lower()
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
    status: object = "active",
    due_date: object = None,
    session_id: object = None,
    provider: object = "claude",
    notes: object = "",
) -> dict:
    clean_title = _text(title, "title", limit=500, required=True)
    clean_path = _text(project_path, "project_path", limit=4096)
    project = projects.resolve_project(clean_path or None)
    clean_session = _text(session_id, "session_id", limit=500) or None
    if clean_session is None:
        raise ValueError("session_id is required")
    clean_status = _status(status)
    clean_provider = _provider(provider)
    clean_due = _due(due_date)
    clean_notes = _text(notes, "notes", limit=4000)
    now = time.time()
    task_id = str(uuid.uuid4())
    completed_at = now if clean_status in {"done", "archived"} else None
    with _conn() as conn:
        try:
            conn.execute(
                """INSERT INTO work_items
                   (task_id, title, project_key, project_name, project_path,
                    status, due_date, session_id, session_provider, notes,
                    created_at, updated_at, completed_at, title_override,
                    title_source, session_cwd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    clean_title,
                    "manual",
                    clean_path,
                ),
            )
        except Exception as exc:
            if "UNIQUE constraint failed: work_items.session_id" in str(exc):
                raise ValueError("this thread is already in the work queue") from exc
            raise
    return get(task_id)  # type: ignore[return-value]


def _folder_project(cwd: str) -> dict[str, str]:
    """Return a no-subprocess folder identity for the hook hot path."""
    if not cwd:
        return {"key": "inbox", "name": "Inbox", "path": ""}
    canonical = os.path.realpath(os.path.abspath(os.path.expanduser(cwd)))
    return {
        "key": f"path:{canonical}",
        "name": Path(canonical).name or canonical,
        "path": canonical,
    }


def ensure_for_session(row: dict, *, reactivate_done: bool = False) -> dict:
    """Create the user-owned overlay for a terminal session exactly once.

    Hook-owned runtime fields keep changing; title/due/status must not. This
    sparse overlay is therefore initialized from the first observed session
    snapshot and subsequently changed only by explicit user edits.
    """
    session_id = _text(row.get("session_id"), "session_id", limit=500, required=True)
    cwd = str(row.get("cwd") or "")
    title = str(row.get("name") or os.path.basename(cwd) or session_id)
    provider = _provider(row.get("provider") or store.DEFAULT_PROVIDER)
    project = _folder_project(cwd)
    now = time.time()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO work_items
               (task_id, title, project_key, project_name, project_path, status,
                due_date, session_id, session_provider, notes, created_at,
                updated_at, completed_at, title_override, title_source, session_cwd)
               VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?, '', ?, ?, NULL,
                       NULL, 'automatic', ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 title = CASE WHEN work_items.title_source != 'manual'
                              THEN excluded.title ELSE work_items.title END,
                 session_provider = excluded.session_provider,
                 session_cwd = CASE WHEN excluded.session_cwd != ''
                                    THEN excluded.session_cwd ELSE work_items.session_cwd END,
                 status = CASE WHEN ? AND work_items.status = 'done'
                               THEN 'active' ELSE work_items.status END,
                 completed_at = CASE WHEN ? AND work_items.status = 'done'
                                     THEN NULL ELSE work_items.completed_at END,
                 updated_at = CASE WHEN work_items.title_source != 'manual'
                                        OR (? AND work_items.status = 'done')
                                   THEN excluded.updated_at ELSE work_items.updated_at END""",
            (
                str(uuid.uuid4()), title, project["key"], project["name"],
                project["path"], session_id, provider, now, now, cwd,
                reactivate_done, reactivate_done, reactivate_done,
            ),
        )
        result = conn.execute(
            "SELECT * FROM work_items WHERE session_id = ?", (session_id,)
        ).fetchone()
    return dict(result)


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
    clauses = ["session_id IS NOT NULL"]
    if not include_done:
        clauses.append("status = 'active'")
    where = "WHERE " + " AND ".join(clauses)
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
        values["title_override"] = values["title"]
        values["title_source"] = "manual"
    if "status" in changes:
        values["status"] = _status(changes["status"])
        values["completed_at"] = (
            time.time() if values["status"] in {"done", "archived"} else None
        )
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
        if cursor.rowcount and "title" in changes and existing.get("session_id"):
            conn.execute(
                "UPDATE sessions SET name = ?, name_source = 'manual', updated_at = ? "
                "WHERE session_id = ?",
                (values["title"], values["updated_at"], existing["session_id"]),
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

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
PRIORITIES = ("urgent", "high", "normal", "low")
_PROTOTYPE_TASK_ID = "0b001368-52a5-4368-8638-bf7b79670851"
_MIGRATION_LOCK = threading.Lock()
_RECONCILE_LIMIT = 5000
_REORDER_LIMIT = 500

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
    session_cwd      TEXT,
    priority         TEXT NOT NULL DEFAULT 'normal',
    position         INTEGER NOT NULL
)
"""

_PROJECT_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_settings (
    project_key TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL,
    updated_at  REAL NOT NULL
)
"""


def migration_backup_path() -> Path:
    return Path(f"{store._DB_PATH}.pre-work-overlay.bak")


def planning_migration_backup_path() -> Path:
    return Path(f"{store._DB_PATH}.pre-priority-ordering.bak")


def _backup_database_to(conn: sqlite3.Connection, backup_path: Path) -> None:
    if backup_path.exists():
        return
    conn.commit()
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(backup_path)
    try:
        conn.backup(destination)
    finally:
        destination.close()


def _backup_database(conn: sqlite3.Connection) -> None:
    _backup_database_to(conn, migration_backup_path())


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


def _migrate_planning(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(work_items)").fetchall()
    }
    has_settings = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'project_settings'"
    ).fetchone()
    if {"priority", "position"} <= columns and has_settings:
        return
    _backup_database_to(conn, planning_migration_backup_path())
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "priority" not in columns:
            conn.execute(
                "ALTER TABLE work_items ADD COLUMN priority TEXT "
                "NOT NULL DEFAULT 'normal'"
            )
        if "position" not in columns:
            conn.execute(
                "ALTER TABLE work_items ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
            )
            rows = conn.execute(
                "SELECT task_id FROM work_items ORDER BY created_at, task_id"
            ).fetchall()
            for index, row in enumerate(rows, start=1):
                conn.execute(
                    "UPDATE work_items SET position = ? WHERE task_id = ?",
                    (index * 1_000_000, row[0]),
                )
        conn.execute(_PROJECT_SETTINGS_SCHEMA)
        now = time.time()
        project_rows = conn.execute(
            """SELECT project_key, MIN(position) AS first_position
               FROM work_items WHERE session_id IS NOT NULL
               GROUP BY project_key ORDER BY first_position, project_key"""
        ).fetchall()
        for index, row in enumerate(project_rows, start=1):
            conn.execute(
                """INSERT OR IGNORE INTO project_settings
                   (project_key, description, position, updated_at)
                   VALUES (?, '', ?, ?)""",
                (row[0], index * 1_000_000, now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _ensure_planning_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_PROJECT_SETTINGS_SCHEMA)
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_work_items_project_priority_position
           ON work_items(project_key, priority, position, task_id)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_work_items_status_position
           ON work_items(status, position, task_id)"""
    )


def _conn():
    conn = store.get_conn()
    with _MIGRATION_LOCK:
        conn.execute(_SCHEMA)
        _migrate_overlay(conn)
        _migrate_planning(conn)
        _ensure_planning_schema(conn)
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


def _priority(value: object) -> str:
    result = str(value or "normal").strip().lower()
    if result not in PRIORITIES:
        raise ValueError(f"priority must be one of: {', '.join(PRIORITIES)}")
    return result


def _bounded_unique_strings(values: object, field: str) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ValueError(f"{field} must be a non-empty list")
    if len(values) > _REORDER_LIMIT:
        raise ValueError(f"{field} may contain at most {_REORDER_LIMIT} items")
    normalized = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must contain non-empty strings")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} must be unique")
    return normalized


def _provider(value: object) -> str:
    result = str(value or "claude").strip().lower()
    if result not in PROVIDERS:
        raise ValueError(f"provider must be one of: {', '.join(PROVIDERS)}")
    return result


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
                updated_at, completed_at, title_override, title_source, session_cwd,
                priority, position)
               VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?, '', ?, ?, NULL,
                       NULL, 'automatic', ?, 'normal', ?)
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
                project["path"], session_id, provider, now, now, cwd, time.time_ns(),
                reactivate_done, reactivate_done, reactivate_done,
            ),
        )
        result = conn.execute(
            "SELECT * FROM work_items WHERE session_id = ?", (session_id,)
        ).fetchone()
    return dict(result)


def reconcile_sessions(*, limit: int = _RECONCILE_LIMIT) -> int:
    """Reconcile existing runtime rows once, outside the hook and GET paths.

    Git discovery happens before the write transaction. The bounded set is
    then committed as one idempotent batch, so startup either publishes the
    whole reconciliation or none of it.
    """
    if limit <= 0:
        return 0
    conn = _conn()
    rows = conn.execute(
        """SELECT sessions.* FROM sessions
           LEFT JOIN work_items ON work_items.session_id = sessions.session_id
           WHERE COALESCE(sessions.provider, 'claude') IN ('claude', 'codex')
             AND (work_items.session_id IS NULL
                  OR work_items.project_key LIKE 'path:%')
           ORDER BY sessions.updated_at DESC, sessions.session_id
           LIMIT ?""",
        (limit,),
    ).fetchall()
    prepared = []
    for raw in rows:
        row = dict(raw)
        session_id = str(row.get("session_id") or "")
        cwd = str(row.get("cwd") or "")
        provider = _provider(row.get("provider") or store.DEFAULT_PROVIDER)
        title = str(row.get("name") or os.path.basename(cwd) or session_id)
        prepared.append((row, projects.resolve_project(cwd or None), title, provider))

    changed = 0
    now = time.time()
    with conn:
        for row, project, title, provider in prepared:
            session_id = str(row["session_id"])
            cwd = str(row.get("cwd") or "")
            timestamp = float(row.get("updated_at") or now)
            existing = conn.execute(
                "SELECT * FROM work_items WHERE session_id = ?", (session_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO work_items
                       (task_id, title, project_key, project_name, project_path,
                        status, due_date, session_id, session_provider, notes,
                        created_at, updated_at, completed_at, title_override,
                        title_source, session_cwd, priority, position)
                       VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?, '', ?, ?,
                               NULL, NULL, 'automatic', ?, 'normal', ?)""",
                    (
                        str(uuid.uuid4()), title, project["key"], project["name"],
                        project["path"], session_id, provider, timestamp, timestamp, cwd,
                        time.time_ns(),
                    ),
                )
                changed += 1
                continue
            updates = {
                "project_key": project["key"],
                "project_name": project["name"],
                "project_path": project["path"],
                "session_provider": provider,
                "session_cwd": cwd or existing["session_cwd"],
            }
            if existing["title_source"] != "manual":
                updates["title"] = title
            if any(existing[field] != value for field, value in updates.items()):
                assignments = ", ".join(f"{field} = ?" for field in updates)
                conn.execute(
                    f"UPDATE work_items SET {assignments} WHERE session_id = ?",
                    (*updates.values(), session_id),
                )
                changed += 1
    return changed


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
                  CASE priority
                    WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                    WHEN 'normal' THEN 2 ELSE 3 END,
                  position,
                  task_id"""
        ).fetchall()
    return [dict(row) for row in rows]


def mutate(task_id: str, **changes: object) -> tuple[dict | None, str | None]:
    """Atomically mutate user work fields and related runtime projection.

    The returned session id is publication work for the caller to start only
    after this function's transaction has committed.
    """
    allowed = {"title", "status", "due_date", "priority"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    if not changes:
        return get(task_id), None
    existing = get(task_id)
    if existing is None:
        return None, None
    if not existing.get("session_id"):
        return None, None
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
    if "priority" in changes:
        values["priority"] = _priority(changes["priority"])
    values["updated_at"] = time.time()

    assignments = ", ".join(f"{field} = ?" for field in values)
    publish_session: str | None = None
    with _conn() as conn:
        cursor = conn.execute(
            f"UPDATE work_items SET {assignments} WHERE task_id = ?",
            (*values.values(), task_id),
        )
        session_id = str(existing.get("session_id") or "")
        closes = (
            cursor.rowcount
            and "status" in changes
            and values["status"] in {"done", "archived"}
            and existing.get("status") != values["status"]
        )
        updates_runtime = bool(cursor.rowcount and session_id and ("title" in changes or closes))
        if cursor.rowcount and "title" in changes and session_id:
            conn.execute(
                "UPDATE sessions SET name = ?, name_source = 'manual', updated_at = ? "
                "WHERE session_id = ?",
                (values["title"], values["updated_at"], session_id),
            )
        if closes and session_id:
            conn.execute(
                "UPDATE sessions SET acked_at = ?, pending_alert = NULL, "
                "pending_alert_revision = NULL WHERE session_id = ?",
                (values["updated_at"], session_id),
            )
        if updates_runtime:
            runtime_cursor = conn.execute(
                "UPDATE sessions SET sync_revision = COALESCE(sync_revision, 0) + 1 "
                "WHERE session_id = ?",
                (session_id,),
            )
            if runtime_cursor.rowcount:
                publish_session = session_id
        result = conn.execute(
            "SELECT * FROM work_items WHERE task_id = ?", (task_id,)
        ).fetchone()
    return (dict(result) if result is not None else None), publish_session


def reorder_tasks(
    project_key: object, task_ids: object, *, priority: object | None = None
) -> list[dict]:
    """Reassign existing ordered slots after validating the whole visible group."""
    key = _text(project_key, "project_key", limit=1000, required=True)
    ids = _bounded_unique_strings(task_ids, "task_ids")
    destination_priority = _priority(priority) if priority is not None else None
    placeholders = ",".join("?" for _ in ids)
    conn = _conn()
    with conn:
        rows = conn.execute(
            f"SELECT * FROM work_items WHERE task_id IN ({placeholders})",
            ids,
        ).fetchall()
        if len(rows) != len(ids):
            raise ValueError("task_ids must identify existing tasks")
        by_id = {row["task_id"]: row for row in rows}
        ordered = [by_id[task_id] for task_id in ids]
        if any(not row["session_id"] for row in ordered):
            raise ValueError("task_ids must be session-backed")
        if any(row["project_key"] != key for row in ordered):
            raise ValueError("task_ids must belong to the same project")
        statuses = {row["status"] for row in ordered}
        priorities = {row["priority"] for row in ordered}
        if len(statuses) != 1:
            raise ValueError("task_ids must belong to the same work-status group")
        status = next(iter(statuses))
        if status in {"done", "archived"}:
            if destination_priority is not None:
                raise ValueError("closed tasks cannot change priority")
            if len(priorities) != 1:
                raise ValueError("closed tasks may reorder only within their group")
        elif destination_priority is None and len(priorities) != 1:
            raise ValueError("active tasks may reorder only within one priority group")

        slots = sorted(int(row["position"]) for row in ordered)
        now = time.time()
        for task_id, slot in zip(ids, slots, strict=True):
            if destination_priority is None:
                conn.execute(
                    "UPDATE work_items SET position = ?, updated_at = ? WHERE task_id = ?",
                    (slot, now, task_id),
                )
            else:
                conn.execute(
                    """UPDATE work_items SET position = ?, priority = ?, updated_at = ?
                       WHERE task_id = ?""",
                    (slot, destination_priority, now, task_id),
                )
        result = conn.execute(
            f"SELECT * FROM work_items WHERE task_id IN ({placeholders})", ids
        ).fetchall()
    result_by_id = {row["task_id"]: dict(row) for row in result}
    return [result_by_id[task_id] for task_id in ids]


def _ensure_project_settings(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """SELECT project_key FROM work_items WHERE session_id IS NOT NULL
           GROUP BY project_key ORDER BY MIN(position), project_key"""
    ).fetchall()
    existing_max = conn.execute(
        "SELECT COALESCE(MAX(position), 0) FROM project_settings"
    ).fetchone()[0]
    next_position = max(int(existing_max) + 1, time.time_ns())
    now = time.time()
    for row in rows:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO project_settings
               (project_key, description, position, updated_at)
               VALUES (?, '', ?, ?)""",
            (row[0], next_position, now),
        )
        if cursor.rowcount:
            next_position += 1


def list_projects() -> list[dict]:
    with _conn() as conn:
        _ensure_project_settings(conn)
        rows = conn.execute(
            """SELECT work_items.project_key,
                      MIN(work_items.project_name) AS name,
                      MIN(work_items.project_path) AS path,
                      project_settings.description,
                      project_settings.position
               FROM work_items
               JOIN project_settings USING (project_key)
               WHERE work_items.session_id IS NOT NULL
               GROUP BY work_items.project_key
               ORDER BY project_settings.position, work_items.project_key"""
        ).fetchall()
    return [dict(row) for row in rows]


def set_project_description(project_key: object, description: object) -> dict:
    key = _text(project_key, "project_key", limit=1000, required=True)
    value = _text(description, "description", limit=1000)
    conn = _conn()
    with conn:
        exists = conn.execute(
            """SELECT 1 FROM work_items
               WHERE project_key = ? AND session_id IS NOT NULL LIMIT 1""",
            (key,),
        ).fetchone()
        if not exists:
            raise ValueError("project not found")
        _ensure_project_settings(conn)
        conn.execute(
            "UPDATE project_settings SET description = ?, updated_at = ? "
            "WHERE project_key = ?",
            (value, time.time(), key),
        )
    return next(project for project in list_projects() if project["project_key"] == key)


def reorder_projects(project_keys: object) -> list[dict]:
    keys = _bounded_unique_strings(project_keys, "project_keys")
    conn = _conn()
    with conn:
        _ensure_project_settings(conn)
        existing = {
            row[0]
            for row in conn.execute(
                """SELECT DISTINCT project_key FROM work_items
                   WHERE session_id IS NOT NULL"""
            ).fetchall()
        }
        if set(keys) != existing:
            raise ValueError("project_keys must contain every existing project exactly once")
        slots = sorted(
            row[0]
            for row in conn.execute(
                f"SELECT position FROM project_settings WHERE project_key IN "
                f"({','.join('?' for _ in keys)})",
                keys,
            ).fetchall()
        )
        now = time.time()
        for key, slot in zip(keys, slots, strict=True):
            conn.execute(
                "UPDATE project_settings SET position = ?, updated_at = ? "
                "WHERE project_key = ?",
                (slot, now, key),
            )
    return list_projects()

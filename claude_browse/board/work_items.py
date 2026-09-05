"""Persistent user-owned work queue stored beside Agent Board session state."""

from __future__ import annotations

import os
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
    position         INTEGER NOT NULL,
    project_resolved INTEGER NOT NULL DEFAULT 0
)
"""

_PROJECT_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_settings (
    project_key TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL,
    updated_at  REAL NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    folder_id    TEXT
)
"""

_FOLDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    position   INTEGER NOT NULL,
    updated_at REAL NOT NULL
)
"""

_PROJECT_DESCRIPTION_FRAGMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_description_fragments (
    target_key  TEXT NOT NULL,
    source_key  TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (target_key, source_key)
)
"""

_TASK_SESSION_LINKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_session_links (
    session_id TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    provider   TEXT,
    cwd        TEXT,
    created_at REAL NOT NULL
)
"""


def migration_backup_path() -> Path:
    return Path(f"{store._DB_PATH}.pre-work-overlay.bak")


def planning_migration_backup_path() -> Path:
    return Path(f"{store._DB_PATH}.pre-priority-ordering.bak")


def project_resolution_migration_backup_path() -> Path:
    return Path(f"{store._DB_PATH}.pre-project-resolution.bak")


def sidebar_migration_backup_path() -> Path:
    return Path(f"{store._DB_PATH}.pre-sidebar-organization.bak")


def linked_workspace_migration_backup_path() -> Path:
    return Path(f"{store._DB_PATH}.pre-linked-workspace.bak")


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
    if {"priority", "position", "project_resolved"} <= columns and has_settings:
        return
    backup_path = (
        project_resolution_migration_backup_path()
        if {"priority", "position"} <= columns and "project_resolved" not in columns
        else planning_migration_backup_path()
    )
    _backup_database_to(conn, backup_path)
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
        if "project_resolved" not in columns:
            conn.execute(
                "ALTER TABLE work_items ADD COLUMN project_resolved INTEGER "
                "NOT NULL DEFAULT 0"
            )
        conn.execute(_PROJECT_SETTINGS_SCHEMA)
        conn.execute(_FOLDERS_SCHEMA)
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


def _migrate_sidebar(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(project_settings)").fetchall()
    }
    has_folders = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'folders'"
    ).fetchone()
    if {"display_name", "folder_id"} <= columns and has_folders:
        return
    _backup_database_to(conn, sidebar_migration_backup_path())
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "display_name" not in columns:
            conn.execute(
                "ALTER TABLE project_settings ADD COLUMN display_name "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "folder_id" not in columns:
            conn.execute("ALTER TABLE project_settings ADD COLUMN folder_id TEXT")
        conn.execute(_FOLDERS_SCHEMA)
        conn.execute(
            "UPDATE project_settings SET folder_id = NULL "
            "WHERE folder_id IS NOT NULL AND folder_id NOT IN (SELECT id FROM folders)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _ensure_planning_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_PROJECT_SETTINGS_SCHEMA)
    conn.execute(_FOLDERS_SCHEMA)
    conn.execute(_PROJECT_DESCRIPTION_FRAGMENTS_SCHEMA)
    has_session_links = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_session_links'"
    ).fetchone()
    if has_session_links is None:
        _backup_database_to(conn, linked_workspace_migration_backup_path())
        conn.execute(_TASK_SESSION_LINKS_SCHEMA)
        conn.execute(
            """INSERT OR IGNORE INTO task_session_links
               (session_id, task_id, provider, cwd, created_at)
               SELECT session_id, task_id, session_provider, session_cwd, created_at
               FROM work_items WHERE session_id IS NOT NULL"""
        )
        # The INSERT above opens a SQLite transaction.  _conn() is also used
        # immediately before callers reserve BEGIN IMMEDIATE, so finish this
        # additive migration before returning the shared connection.
        conn.commit()
    else:
        conn.execute(_TASK_SESSION_LINKS_SCHEMA)
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
        _migrate_sidebar(conn)
        _ensure_planning_schema(conn)
    return conn


def _text(value: object, field: str, *, limit: int, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return result


def _strict_text(value: object, field: str, *, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return _text(value, field, limit=limit, required=required)


def _project_key(value: object) -> str:
    return _strict_text(value, "project_key", limit=1000, required=True)


def _folder_id(value: object) -> str | None:
    if value is None:
        return None
    return _strict_text(value, "folder_id", limit=200, required=True)


def _due(value: object) -> str | None:
    result = str(value or "").strip()
    if not result:
        return None
    try:
        parsed = date.fromisoformat(result)
    except ValueError as exc:
        raise ValueError("due_date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != result:
        raise ValueError("due_date must be YYYY-MM-DD")
    return parsed.isoformat()


def _status(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    result = value.strip().lower()
    if result not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    return result


def _priority(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"priority must be one of: {', '.join(PRIORITIES)}")
    result = value.strip().lower()
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
    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        return _ensure_for_session(conn, row, reactivate_done=reactivate_done)


def _ensure_for_session(
    conn: sqlite3.Connection, row: dict, *, reactivate_done: bool = False
) -> dict:
    """Initialize or refresh an overlay under the caller's writer transaction."""
    session_id = _text(row.get("session_id"), "session_id", limit=500, required=True)
    cwd = str(row.get("cwd") or "")
    title = str(row.get("name") or os.path.basename(cwd) or session_id)
    provider = _provider(row.get("provider") or store.DEFAULT_PROVIDER)
    project = _folder_project(cwd)
    now = time.time()
    # A delayed hook for a historical conversation must keep addressing its
    # canonical card. The writer lock excludes adoption between this check
    # and the current-session upsert below.
    linked = conn.execute(
        """SELECT work_items.* FROM task_session_links
           JOIN work_items USING (task_id)
           WHERE task_session_links.session_id = ?
             AND work_items.session_id != ?""",
        (session_id, session_id),
    ).fetchone()
    if linked is not None:
        return dict(linked)
    conn.execute(
        """INSERT INTO work_items
           (task_id, title, project_key, project_name, project_path, status,
            due_date, session_id, session_provider, notes, created_at,
            updated_at, completed_at, title_override, title_source, session_cwd,
            priority, position, project_resolved)
           VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?, '', ?, ?, NULL,
                   NULL, 'automatic', ?, 'normal', ?, 0)
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
    conn.execute(
        """INSERT OR IGNORE INTO task_session_links
           (session_id, task_id, provider, cwd, created_at) VALUES (?, ?, ?, ?, ?)""",
        (session_id, result["task_id"], provider, cwd, now),
    )
    _ensure_project_setting(conn, result["project_key"])
    return dict(result)


def capture_discovered_session(session_id: str) -> bool:
    """Create only an unowned overlay that cannot race token-based adoption."""
    from claude_browse.board import launches

    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        runtime = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if runtime is None or conn.execute(
            """SELECT 1 FROM work_items WHERE session_id = ?
               UNION ALL SELECT 1 FROM task_session_links WHERE session_id = ?""",
            (session_id, session_id),
        ).fetchone() is not None:
            return False
        provider = _provider(runtime["provider"] or store.DEFAULT_PROVIDER)
        if launches.awaiting_task_adoption(conn, provider, runtime["cwd"] or ""):
            return False
        _ensure_for_session(conn, dict(runtime))
        return True


def reconcile_sessions(*, limit: int = _RECONCILE_LIMIT) -> int:
    """Reconcile existing runtime rows once, outside the hook and GET paths.

    Git discovery happens before the write transaction. The bounded set is
    then committed as one idempotent batch, so startup either publishes the
    whole reconciliation or none of it.
    """
    from claude_browse.board import launches

    if limit <= 0:
        return 0
    conn = _conn()
    rows = conn.execute(
        """SELECT sessions.* FROM sessions
           LEFT JOIN work_items ON work_items.session_id = sessions.session_id
           LEFT JOIN task_session_links
             ON task_session_links.session_id = sessions.session_id
           WHERE COALESCE(sessions.provider, 'claude') IN ('claude', 'codex')
             AND (task_session_links.session_id IS NULL
                  OR (work_items.session_id = sessions.session_id
                      AND work_items.project_resolved = 0))
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
        conn.execute("BEGIN IMMEDIATE")
        for row, project, title, provider in prepared:
            session_id = str(row["session_id"])
            cwd = str(row.get("cwd") or "")
            current_runtime = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            # Project discovery deliberately happens outside the writer lock.
            # If a hook changed this session meanwhile, leave it unresolved so
            # the next bounded pass resolves the new cwd instead of blessing a
            # stale project as final.
            if current_runtime is None or any(
                current_runtime[field] != row[field]
                for field in ("updated_at", "cwd", "name", "provider")
            ):
                continue
            timestamp = float(row.get("updated_at") or now)
            existing = conn.execute(
                "SELECT * FROM work_items WHERE session_id = ?", (session_id,)
            ).fetchone()
            if existing is None:
                # Recheck links after out-of-transaction project discovery:
                # this session may now be historical after hook adoption.
                if conn.execute(
                    "SELECT 1 FROM task_session_links WHERE session_id = ?", (session_id,)
                ).fetchone() is not None or launches.awaiting_task_adoption(conn, provider, cwd):
                    continue
                task_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO work_items
                       (task_id, title, project_key, project_name, project_path,
                        status, due_date, session_id, session_provider, notes,
                        created_at, updated_at, completed_at, title_override,
                        title_source, session_cwd, priority, position, project_resolved)
                       VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?, '', ?, ?,
                               NULL, NULL, 'automatic', ?, 'normal', ?, 1)""",
                    (
                        task_id, title, project["key"], project["name"],
                        project["path"], session_id, provider, timestamp, timestamp, cwd,
                        time.time_ns(),
                    ),
                )
                _ensure_project_setting(conn, project["key"])
                conn.execute(
                    """INSERT OR IGNORE INTO task_session_links
                       (session_id, task_id, provider, cwd, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (session_id, task_id, provider, cwd, timestamp),
                )
                changed += 1
                continue
            previous_project_key = str(existing["project_key"])
            updates = {
                "project_key": project["key"],
                "project_name": project["name"],
                "project_path": project["path"],
                "session_provider": provider,
                "session_cwd": cwd or existing["session_cwd"],
                "project_resolved": 1,
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
            _ensure_project_setting(
                conn, project["key"], previous_key=previous_project_key
            )
        conn.execute(
            "DELETE FROM project_settings WHERE project_key NOT IN "
            "(SELECT DISTINCT project_key FROM work_items WHERE session_id IS NOT NULL)"
        )
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
            """SELECT work_items.* FROM work_items
               LEFT JOIN task_session_links USING (task_id)
               WHERE work_items.session_id = ? OR task_session_links.session_id = ?
               LIMIT 1""",
            (session_id, session_id),
        ).fetchone()
    return dict(row) if row is not None else None


def attach_continuation(
    task_id: str, session: dict, expected_session_id: str
) -> dict:
    """Atomically attach a new runtime session to an existing visible task."""
    key = _strict_text(task_id, "task_id", limit=200, required=True)
    expected = _strict_text(
        expected_session_id, "expected_session_id", limit=500, required=True
    )
    if not isinstance(session, dict):
        raise ValueError("session must be a dictionary")
    new_session_id = _strict_text(
        session.get("session_id"), "session_id", limit=500, required=True
    )
    provider = _provider(session.get("provider") or store.DEFAULT_PROVIDER)
    cwd = str(session.get("cwd") or "")
    now = time.time()
    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute(
            "SELECT * FROM work_items WHERE task_id = ?", (key,)
        ).fetchone()
        if task is None or not task["session_id"]:
            raise ValueError("task not found")
        if task["session_id"] != expected:
            raise ValueError("stale expected session id")
        linked = conn.execute(
            "SELECT task_id FROM task_session_links WHERE session_id = ?", (new_session_id,)
        ).fetchone()
        current_owner = conn.execute(
            "SELECT task_id FROM work_items WHERE session_id = ?", (new_session_id,)
        ).fetchone()
        owner = linked or current_owner
        if owner is not None and owner["task_id"] != key:
            raise ValueError("new session belongs to another task")
        if linked is not None:
            raise ValueError("new session is already linked to this task")
        # This supports adoption immediately after SessionStart intent matching,
        # before ordinary hook capture has had a chance to create the runtime row.
        conn.execute(
            """INSERT INTO sessions (session_id, cwd, provider, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET cwd = excluded.cwd,
                   provider = excluded.provider, updated_at = excluded.updated_at""",
            (new_session_id, cwd, provider, now),
        )
        conn.execute(
            """INSERT OR IGNORE INTO task_session_links
               (session_id, task_id, provider, cwd, created_at) VALUES (?, ?, ?, ?, ?)""",
            (expected, key, task["session_provider"], task["session_cwd"], task["created_at"]),
        )
        conn.execute(
            """INSERT INTO task_session_links (session_id, task_id, provider, cwd, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (new_session_id, key, provider, cwd, now),
        )
        conn.execute(
            """UPDATE work_items SET session_id = ?, session_provider = ?, session_cwd = ?,
               title_override = title, title_source = 'manual', project_resolved = 1,
               updated_at = ? WHERE task_id = ?""",
            (new_session_id, provider, cwd, now, key),
        )
        result = conn.execute("SELECT * FROM work_items WHERE task_id = ?", (key,)).fetchone()
    return dict(result)


def get_session_history(task_id: str) -> list[dict]:
    key = _strict_text(task_id, "task_id", limit=200, required=True)
    with _conn() as conn:
        rows = conn.execute(
            """SELECT session_id, provider, cwd, created_at FROM task_session_links
               WHERE task_id = ? ORDER BY created_at, session_id""",
            (key,),
        ).fetchall()
    return [dict(row) for row in rows]


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
    conn = _conn()
    with conn:
        # Reserve the writer before reading the row. Otherwise a hook or a
        # second UI request can change its status between this read and the
        # related work/runtime updates below.
        conn.execute("BEGIN IMMEDIATE")
        existing_row = conn.execute(
            "SELECT * FROM work_items WHERE task_id = ?", (task_id,)
        ).fetchone()
        if existing_row is None or not existing_row["session_id"]:
            return None, None
        existing = dict(existing_row)
        cursor = conn.execute(
            f"UPDATE work_items SET {assignments} WHERE task_id = ?",
            (*values.values(), task_id),
        )
        session_id = str(existing["session_id"])
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


def finish_turn(
    session_id: str,
    working_since: float,
    turn_s: float,
    *,
    cwd: str | None,
    host: str,
    model_label: str | None = None,
    mark_unattended: bool = True,
) -> tuple[bool, bool]:
    """Finish one turn and decide alerts against the serialized work status."""

    def alert_allowed(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT status FROM work_items WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row is None or row["status"] not in {"done", "archived"}

    return store.finish_turn_with_decision(
        session_id,
        working_since,
        turn_s,
        cwd=cwd,
        host=host,
        model_label=model_label,
        mark_unattended=mark_unattended,
        alert_allowed=alert_allowed,
    )


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
        conn.execute("BEGIN IMMEDIATE")
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
        if len(statuses) != 1:
            raise ValueError("task_ids must belong to the same work-status group")
        status = next(iter(statuses))
        if status in {"done", "archived"}:
            if destination_priority is not None:
                raise ValueError("closed tasks cannot change priority")

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


def _ensure_project_setting(
    conn: sqlite3.Connection, project_key: str, *, previous_key: str | None = None
) -> None:
    if previous_key and previous_key != project_key:
        previous = conn.execute(
            """SELECT description, position, display_name, folder_id
               FROM project_settings WHERE project_key = ?""",
            (previous_key,),
        ).fetchone()
        conn.execute(
            """INSERT OR IGNORE INTO project_settings
               (project_key, description, position, updated_at, display_name, folder_id)
               SELECT ?, description, position, ?, display_name,
                      CASE WHEN folder_id IS NULL
                                 OR folder_id IN (SELECT id FROM folders)
                           THEN folder_id ELSE NULL END
               FROM project_settings
               WHERE project_key = ?""",
            (project_key, time.time(), previous_key),
        )
        current = conn.execute(
            """SELECT description, position, display_name, folder_id
               FROM project_settings WHERE project_key = ?""",
            (project_key,),
        ).fetchone()
        if previous is not None and current is not None:
            old_description = str(previous["description"] or "").strip()
            current_description = str(current["description"] or "").strip()
            if old_description and old_description not in current_description:
                merged = (
                    f"{current_description}\n\n{old_description}"
                    if current_description
                    else old_description
                )
                if len(merged) <= 10_000:
                    conn.execute(
                        "UPDATE project_settings SET description = ?, position = ?, "
                        "updated_at = ? WHERE project_key = ?",
                        (
                            merged,
                            min(int(previous["position"]), int(current["position"])),
                            time.time(),
                            project_key,
                        ),
                    )
                else:
                    conn.execute(
                        """INSERT INTO project_description_fragments
                           (target_key, source_key, description, created_at)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(target_key, source_key) DO UPDATE SET
                             description = excluded.description""",
                        (project_key, previous_key, old_description, time.time()),
                    )
            old_display_name = str(previous["display_name"] or "").strip()
            current_display_name = str(current["display_name"] or "").strip()
            old_folder_id = previous["folder_id"]
            current_folder_id = current["folder_id"]
            fields: dict[str, object] = {}
            if old_display_name and not current_display_name:
                fields["display_name"] = old_display_name
            if (
                old_folder_id is not None
                and current_folder_id is None
                and conn.execute(
                    "SELECT 1 FROM folders WHERE id = ?", (old_folder_id,)
                ).fetchone()
            ):
                fields["folder_id"] = old_folder_id
            if fields:
                fields["updated_at"] = time.time()
                assignments = ", ".join(f"{field} = ?" for field in fields)
                conn.execute(
                    f"UPDATE project_settings SET {assignments} WHERE project_key = ?",
                    (*fields.values(), project_key),
                )
    if conn.execute(
        "SELECT 1 FROM project_settings WHERE project_key = ?", (project_key,)
    ).fetchone():
        return
    existing_max = conn.execute(
        "SELECT COALESCE(MAX(position), 0) FROM project_settings"
    ).fetchone()[0]
    conn.execute(
        """INSERT OR IGNORE INTO project_settings
           (project_key, description, position, updated_at, display_name, folder_id)
           VALUES (?, '', ?, ?, '', NULL)""",
        (project_key, max(int(existing_max) + 1, time.time_ns()), time.time()),
    )


def list_projects() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT work_items.project_key,
                      COALESCE(NULLIF(project_settings.display_name, ''),
                               MIN(work_items.project_name)) AS name,
                      MIN(work_items.project_path) AS path,
                      project_settings.description,
                      project_settings.position,
                      project_settings.display_name,
                      project_settings.folder_id
               FROM work_items
               JOIN project_settings USING (project_key)
               WHERE work_items.session_id IS NOT NULL
               GROUP BY work_items.project_key
               ORDER BY project_settings.position, work_items.project_key"""
        ).fetchall()
        projects = [dict(row) for row in rows]
        fragments = conn.execute(
            "SELECT target_key, source_key, description "
            "FROM project_description_fragments ORDER BY created_at, source_key"
        ).fetchall()
    by_project: dict[str, list[dict]] = {}
    for fragment in fragments:
        by_project.setdefault(str(fragment["target_key"]), []).append(
            {
                "source_key": fragment["source_key"],
                "description": fragment["description"],
            }
        )
    for project in projects:
        project["inherited_descriptions"] = by_project.get(project["project_key"], [])
    return projects


def update_project(project_key: object, **changes: object) -> dict:
    allowed = {"description", "display_name", "folder_id"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    key = _project_key(project_key)
    values: dict[str, object] = {}
    if "description" in changes:
        values["description"] = _text(changes["description"], "description", limit=10_000)
    if "display_name" in changes:
        values["display_name"] = _strict_text(
            changes["display_name"], "display_name", limit=120
        )
    if "folder_id" in changes:
        values["folder_id"] = _folder_id(changes["folder_id"])
    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            """SELECT 1 FROM work_items
               WHERE project_key = ? AND session_id IS NOT NULL LIMIT 1""",
            (key,),
        ).fetchone()
        if not exists:
            raise ValueError("project not found")
        _ensure_project_setting(conn, key)
        if values.get("folder_id") is not None and not conn.execute(
            "SELECT 1 FROM folders WHERE id = ?", (values["folder_id"],)
        ).fetchone():
            raise ValueError("folder not found")
        if values:
            values["updated_at"] = time.time()
            assignments = ", ".join(f"{field} = ?" for field in values)
            conn.execute(
                f"UPDATE project_settings SET {assignments} WHERE project_key = ?",
                (*values.values(), key),
            )
    return next(project for project in list_projects() if project["project_key"] == key)


def set_project_description(project_key: object, description: object) -> dict:
    return update_project(project_key, description=description)


def list_folders() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id AS folder_id, name, position FROM folders ORDER BY position, id"
        ).fetchall()
    return [dict(row) for row in rows]


def create_folder(name: object) -> dict:
    value = _strict_text(name, "name", limit=120, required=True)
    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_max = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM folders"
        ).fetchone()[0]
        folder_id = str(uuid.uuid4())
        position = max(int(existing_max) + 1, time.time_ns())
        now = time.time()
        conn.execute(
            "INSERT INTO folders (id, name, position, updated_at) VALUES (?, ?, ?, ?)",
            (folder_id, value, position, now),
        )
    return {"folder_id": folder_id, "name": value, "position": position}


def update_folder(folder_id: object, name: object) -> dict:
    key = _folder_id(folder_id)
    if key is None:
        raise ValueError("folder_id must be a non-empty string")
    value = _strict_text(name, "name", limit=120, required=True)
    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE folders SET name = ?, updated_at = ? WHERE id = ?",
            (value, time.time(), key),
        )
        if not cursor.rowcount:
            raise ValueError("folder not found")
        row = conn.execute(
            "SELECT id AS folder_id, name, position FROM folders WHERE id = ?", (key,)
        ).fetchone()
    return dict(row)


def reorder_folders(folder_ids: object) -> list[dict]:
    if not isinstance(folder_ids, list):
        raise ValueError("folder_ids must be a list")
    if len(folder_ids) > _REORDER_LIMIT:
        raise ValueError(f"folder_ids may contain at most {_REORDER_LIMIT} items")
    ids = [
        _strict_text(folder_id, "folder_ids", limit=200, required=True)
        for folder_id in folder_ids
    ]
    if len(set(ids)) != len(ids):
        raise ValueError("folder_ids must be unique")
    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT id, position FROM folders").fetchall()
        existing = {row["id"] for row in rows}
        if set(ids) != existing:
            raise ValueError("folder_ids must contain every existing folder exactly once")
        slots = sorted(int(row["position"]) for row in rows)
        now = time.time()
        for folder_id, slot in zip(ids, slots, strict=True):
            conn.execute(
                "UPDATE folders SET position = ?, updated_at = ? WHERE id = ?",
                (slot, now, folder_id),
            )
    return list_folders()


def reorder_projects(project_keys: object) -> list[dict]:
    keys = _bounded_unique_strings(project_keys, "project_keys")
    conn = _conn()
    with conn:
        for key in keys:
            _ensure_project_setting(conn, key)
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

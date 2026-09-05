"""User-owned workspace hierarchy and task placement storage.

The board's project fields remain a description of where a session came from.
This module deliberately keeps organization and launch destinations in separate
tables, so moving a card never changes a repository identity or a live cwd.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from claude_browse.board import store, work_items

_GENERAL_SPACE_ID = "space:general"
_REORDER_LIMIT = 500
_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_spaces (
    space_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_folders (
    folder_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_lists (
    list_key TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    folder_id TEXT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    working_directory TEXT,
    position INTEGER NOT NULL,
    source_project_key TEXT,
    inherit_session_cwd INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_placements (
    task_id TEXT PRIMARY KEY,
    list_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspace_folders_space_position
    ON workspace_folders(space_id, position, folder_id);
CREATE INDEX IF NOT EXISTS idx_workspace_lists_parent_position
    ON workspace_lists(space_id, folder_id, position, list_key);
CREATE INDEX IF NOT EXISTS idx_task_placements_list_position
    ON task_placements(list_key, position, task_id);
CREATE TABLE IF NOT EXISTS workspace_seed_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    project_count INTEGER NOT NULL,
    folder_count INTEGER NOT NULL
);
"""

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_PATH: Path | None = None


def migration_backup_path() -> Path:
    """The one-time backup shared by linked-workspace additive migrations."""
    return work_items.linked_workspace_migration_backup_path()


def _backup(conn: sqlite3.Connection) -> None:
    work_items._backup_database_to(conn, migration_backup_path())


def _conn() -> sqlite3.Connection:
    global _SCHEMA_PATH
    conn = work_items._conn()
    # Workspace calls are on the board poll path.  Do schema work only once
    # per database path, not on every context lookup or placement mutation.
    with _SCHEMA_LOCK:
        if _SCHEMA_PATH != store._DB_PATH:
            existing = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workspace_spaces'"
            ).fetchone()
            if existing is None:
                _backup(conn)
            conn.executescript(_SCHEMA)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(workspace_lists)")
            }
            if "inherit_session_cwd" not in columns:
                conn.execute(
                    "ALTER TABLE workspace_lists ADD COLUMN inherit_session_cwd "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            conn.commit()
            _SCHEMA_PATH = store._DB_PATH
    return conn


def _strict_text(value: object, field: str, *, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return result


def _optional_id(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _strict_text(value, field, limit=200, required=True)


def _position(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("position must be an integer")
    return value


def _workspace_path(value: object, *, must_exist: bool) -> str | None:
    if value is None:
        return None
    raw = _strict_text(value, "working_directory", limit=10_000, required=True)
    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
    if must_exist and not os.path.isdir(resolved):
        raise ValueError("working_directory must exist and be a directory")
    return resolved


def _folder_status(path: str | None) -> str:
    if path is None:
        return "unlinked"
    return "ready" if os.path.isdir(path) else "missing"


def _next_position(conn: sqlite3.Connection, table: str, clause: str = "", args=()) -> int:
    row = conn.execute(f"SELECT COALESCE(MAX(position), 0) FROM {table} {clause}", args).fetchone()
    return max(int(row[0]) + 1, time.time_ns())


def _seed(conn: sqlite3.Connection) -> None:
    """Copy legacy sidebar preferences once, while admitting later projects."""
    project_count = conn.execute(
        "SELECT COUNT(DISTINCT project_key) FROM work_items WHERE session_id IS NOT NULL"
    ).fetchone()[0]
    folder_count = conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
    state = conn.execute(
        "SELECT project_count, folder_count FROM workspace_seed_state WHERE id = 1"
    ).fetchone()
    if state is not None and state["project_count"] == project_count and state["folder_count"] == folder_count:
        return
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO workspace_spaces (space_id, name, position, updated_at) VALUES (?, ?, ?, ?)",
        (_GENERAL_SPACE_ID, "General", 1, now),
    )
    legacy_folders = conn.execute(
        "SELECT id, name, position FROM folders ORDER BY position, id"
    ).fetchall()
    for folder in legacy_folders:
        conn.execute(
            """INSERT OR IGNORE INTO workspace_folders
               (folder_id, space_id, name, position, updated_at) VALUES (?, ?, ?, ?, ?)""",
            (folder["id"], _GENERAL_SPACE_ID, folder["name"], folder["position"], now),
        )
    rows = conn.execute(
        """SELECT work_items.project_key,
                  COALESCE(NULLIF(project_settings.display_name, ''), MIN(work_items.project_name)) AS name,
                  MIN(work_items.project_path) AS path, project_settings.description, project_settings.position,
                  project_settings.folder_id
           FROM work_items JOIN project_settings USING (project_key)
           WHERE work_items.session_id IS NOT NULL
           GROUP BY work_items.project_key
           ORDER BY project_settings.position, work_items.project_key"""
    ).fetchall()
    for project in rows:
        folder_id = project["folder_id"]
        if folder_id is not None and not conn.execute(
            "SELECT 1 FROM workspace_folders WHERE folder_id = ?", (folder_id,)
        ).fetchone():
            folder_id = None
        conn.execute(
            """INSERT OR IGNORE INTO workspace_lists
               (list_key, space_id, folder_id, name, description, working_directory,
                position, source_project_key, inherit_session_cwd, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                project["project_key"], _GENERAL_SPACE_ID, folder_id, project["name"],
                project["description"] or "", project["path"], project["position"],
                project["project_key"], now,
            ),
        )
    conn.execute(
        """INSERT INTO workspace_seed_state (id, project_count, folder_count)
           VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET project_count = excluded.project_count,
               folder_count = excluded.folder_count""",
        (project_count, folder_count),
    )


def _space(conn: sqlite3.Connection, space_id: str) -> dict:
    row = conn.execute(
        "SELECT space_id, name, position FROM workspace_spaces WHERE space_id = ?", (space_id,)
    ).fetchone()
    if row is None:
        raise ValueError("space not found")
    return dict(row)


def _folder(conn: sqlite3.Connection, folder_id: str) -> dict:
    row = conn.execute(
        "SELECT folder_id, space_id, name, position FROM workspace_folders WHERE folder_id = ?",
        (folder_id,),
    ).fetchone()
    if row is None:
        raise ValueError("folder not found")
    return dict(row)


def _list(conn: sqlite3.Connection, list_key: str) -> dict:
    row = conn.execute(
        """SELECT list_key, space_id, folder_id, name, description, working_directory,
                  position, source_project_key, inherit_session_cwd
           FROM workspace_lists WHERE list_key = ?""",
        (list_key,),
    ).fetchone()
    if row is None:
        raise ValueError("list not found")
    result = dict(row)
    result["folder_status"] = _folder_status(result["working_directory"])
    return result


def _public_list(listed: dict) -> dict:
    result = dict(listed)
    result.pop("inherit_session_cwd", None)
    return result


def _validate_parent(conn: sqlite3.Connection, space_id: str, folder_id: str | None) -> None:
    _space(conn, space_id)
    if folder_id is not None and _folder(conn, folder_id)["space_id"] != space_id:
        raise ValueError("folder must belong to the list space")


def _snapshot(conn: sqlite3.Connection) -> dict:
    spaces = [dict(row) for row in conn.execute(
        "SELECT space_id, name, position FROM workspace_spaces ORDER BY position, space_id"
    )]
    folders = [dict(row) for row in conn.execute(
        "SELECT folder_id, space_id, name, position FROM workspace_folders ORDER BY position, folder_id"
    )]
    lists = []
    for row in conn.execute(
        """SELECT list_key, space_id, folder_id, name, description, working_directory,
                  position, source_project_key, inherit_session_cwd FROM workspace_lists
           ORDER BY position, list_key"""
    ):
        item = dict(row)
        item["folder_status"] = _folder_status(item["working_directory"])
        lists.append(_public_list(item))
    return {"spaces": spaces, "folders": folders, "lists": lists}


def snapshot() -> dict:
    conn = _conn()
    with conn:
        _seed(conn)
    return _snapshot(conn)


def create_space(name: str) -> dict:
    value = _strict_text(name, "name", limit=120, required=True)
    conn = _conn()
    with conn:
        _seed(conn)
        space_id = str(uuid.uuid4())
        position = _next_position(conn, "workspace_spaces")
        conn.execute(
            "INSERT INTO workspace_spaces (space_id, name, position, updated_at) VALUES (?, ?, ?, ?)",
            (space_id, value, position, time.time()),
        )
    return _space(conn, space_id)


def update_space(space_id: str, **changes: object) -> dict:
    unknown = set(changes) - {"name", "position"}
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    key = _strict_text(space_id, "space_id", limit=200, required=True)
    values: dict[str, object] = {}
    if "name" in changes:
        values["name"] = _strict_text(changes["name"], "name", limit=120, required=True)
    if "position" in changes:
        values["position"] = _position(changes["position"])
    conn = _conn()
    with conn:
        _seed(conn)
        _space(conn, key)
        if values:
            values["updated_at"] = time.time()
            conn.execute(
                f"UPDATE workspace_spaces SET {', '.join(f'{field} = ?' for field in values)} WHERE space_id = ?",
                (*values.values(), key),
            )
    return _space(conn, key)


def create_folder(name: str, space_id: str) -> dict:
    value = _strict_text(name, "name", limit=120, required=True)
    parent = _strict_text(space_id, "space_id", limit=200, required=True)
    conn = _conn()
    with conn:
        _seed(conn)
        _space(conn, parent)
        folder_id = str(uuid.uuid4())
        position = _next_position(conn, "workspace_folders", "WHERE space_id = ?", (parent,))
        conn.execute(
            """INSERT INTO workspace_folders (folder_id, space_id, name, position, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (folder_id, parent, value, position, time.time()),
        )
    return _folder(conn, folder_id)


def update_folder(folder_id: str, **changes: object) -> dict:
    unknown = set(changes) - {"name", "space_id", "position"}
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    key = _strict_text(folder_id, "folder_id", limit=200, required=True)
    conn = _conn()
    with conn:
        _seed(conn)
        existing = _folder(conn, key)
        values: dict[str, object] = {}
        if "name" in changes:
            values["name"] = _strict_text(changes["name"], "name", limit=120, required=True)
        if "space_id" in changes:
            values["space_id"] = _strict_text(changes["space_id"], "space_id", limit=200, required=True)
            _space(conn, str(values["space_id"]))
        if "position" in changes:
            values["position"] = _position(changes["position"])
        if values:
            values["updated_at"] = time.time()
            conn.execute(
                f"UPDATE workspace_folders SET {', '.join(f'{field} = ?' for field in values)} WHERE folder_id = ?",
                (*values.values(), key),
            )
            if "space_id" in values and values["space_id"] != existing["space_id"]:
                conn.execute(
                    "UPDATE workspace_lists SET space_id = ?, updated_at = ? WHERE folder_id = ?",
                    (values["space_id"], values["updated_at"], key),
                )
    return _folder(conn, key)


def create_list(
    name: str, space_id: str, folder_id: str | None = None, working_directory: str | None = None
) -> dict:
    value = _strict_text(name, "name", limit=120, required=True)
    parent = _strict_text(space_id, "space_id", limit=200, required=True)
    folder = _optional_id(folder_id, "folder_id")
    directory = _workspace_path(working_directory, must_exist=True)
    conn = _conn()
    with conn:
        _seed(conn)
        _validate_parent(conn, parent, folder)
        list_key = f"list:{uuid.uuid4()}"
        position = _next_position(conn, "workspace_lists", "WHERE space_id = ? AND folder_id IS ?", (parent, folder))
        conn.execute(
            """INSERT INTO workspace_lists
               (list_key, space_id, folder_id, name, description, working_directory,
                position, source_project_key, inherit_session_cwd, updated_at)
               VALUES (?, ?, ?, ?, '', ?, ?, NULL, 0, ?)""",
            (list_key, parent, folder, value, directory, position, time.time()),
        )
    return _public_list(_list(conn, list_key))


def update_list(list_key: str, **changes: object) -> dict:
    allowed = {"name", "description", "space_id", "folder_id", "working_directory", "position"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    key = _strict_text(list_key, "list_key", limit=1000, required=True)
    conn = _conn()
    with conn:
        _seed(conn)
        existing = _list(conn, key)
        values: dict[str, object] = {}
        if "name" in changes:
            values["name"] = _strict_text(changes["name"], "name", limit=120, required=True)
        if "description" in changes:
            values["description"] = _strict_text(changes["description"], "description", limit=10_000)
        target_space = _strict_text(changes["space_id"], "space_id", limit=200, required=True) if "space_id" in changes else existing["space_id"]
        target_folder = _optional_id(changes["folder_id"], "folder_id") if "folder_id" in changes else existing["folder_id"]
        _validate_parent(conn, target_space, target_folder)
        if "space_id" in changes:
            values["space_id"] = target_space
        if "folder_id" in changes:
            values["folder_id"] = target_folder
        if "working_directory" in changes:
            values["working_directory"] = _workspace_path(changes["working_directory"], must_exist=True)
            # NULL here is an explicit unlink, distinct from a source List
            # that has never been relinked and may retain a task's exact cwd.
            values["inherit_session_cwd"] = 0
        if "position" in changes:
            values["position"] = _position(changes["position"])
        if values:
            values["updated_at"] = time.time()
            conn.execute(
                f"UPDATE workspace_lists SET {', '.join(f'{field} = ?' for field in values)} WHERE list_key = ?",
                (*values.values(), key),
            )
    return _public_list(_list(conn, key))


def _node(conn: sqlite3.Connection, kind: str, key: str) -> dict:
    if kind == "space":
        return _space(conn, key)
    if kind == "folder":
        return _folder(conn, key)
    return _list(conn, key)


def _sibling_nodes(conn: sqlite3.Connection, kind: str, node: dict) -> list[dict]:
    if kind == "space":
        query, args = "SELECT space_id AS node_id, position FROM workspace_spaces", ()
    elif kind == "folder":
        query, args = (
            "SELECT folder_id AS node_id, position FROM workspace_folders WHERE space_id = ?",
            (node["space_id"],),
        )
    else:
        query, args = (
            """SELECT list_key AS node_id, position FROM workspace_lists
               WHERE space_id = ? AND folder_id IS ?""",
            (node["space_id"], node["folder_id"]),
        )
    siblings = [dict(row) for row in conn.execute(query, args)]
    siblings.sort(key=lambda sibling: (int(sibling["position"]), sibling["node_id"]))
    return siblings


def _same_parent(kind: str, node: dict, target: dict) -> bool:
    if kind == "space":
        return True
    if kind == "folder":
        return node["space_id"] == target["space_id"]
    return (node["space_id"], node["folder_id"]) == (
        target["space_id"], target["folder_id"]
    )


def _rewrite_sibling_positions(
    conn: sqlite3.Connection, kind: str, sibling_ids: list[str]
) -> None:
    table, column = {
        "space": ("workspace_spaces", "space_id"),
        "folder": ("workspace_folders", "folder_id"),
        "list": ("workspace_lists", "list_key"),
    }[kind]
    now = time.time()
    for position, sibling_id in enumerate(sibling_ids, start=1):
        conn.execute(
            f"UPDATE {table} SET position = ?, updated_at = ? WHERE {column} = ?",
            (position, now, sibling_id),
        )


def _public_node(conn: sqlite3.Connection, kind: str, key: str) -> dict:
    node = _node(conn, kind, key)
    return _public_list(node) if kind == "list" else node


def move_node(kind: str, node_id: str, direction: int) -> dict:
    """Move a workspace node one place among its actual siblings.

    Positions from the legacy PATCH endpoints may contain ties.  A successful
    move therefore rewrites the whole sibling group into a dense, stable order
    instead of copying a neighbor's position onto just the requested node.
    """
    if not isinstance(kind, str) or kind not in {"space", "folder", "list"}:
        raise ValueError("kind must be space, folder, or list")
    if isinstance(direction, bool) or not isinstance(direction, int) or direction not in {-1, 1}:
        raise ValueError("direction must be -1 or 1")
    key = _strict_text(node_id, "node_id", limit=1000 if kind == "list" else 200, required=True)

    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _seed(conn)
        node = _node(conn, kind, key)
        siblings = _sibling_nodes(conn, kind, node)
        index = next(index for index, sibling in enumerate(siblings) if sibling["node_id"] == key)
        neighbor_index = index + direction
        if not 0 <= neighbor_index < len(siblings):
            return _public_node(conn, kind, key)

        siblings[index], siblings[neighbor_index] = siblings[neighbor_index], siblings[index]
        _rewrite_sibling_positions(conn, kind, [sibling["node_id"] for sibling in siblings])
        return _public_node(conn, kind, key)


def place_node(kind: str, node_id: str, target_id: str, placement: str) -> dict:
    """Place a node before or after a same-parent sibling atomically.

    This is intentionally distinct from changing a folder/list parent: drag
    ordering must never silently relocate a node across the workspace tree.
    Rewriting all sibling ranks fixes legacy ties and makes the persisted
    order deterministic after every successful drop.
    """
    if not isinstance(kind, str) or kind not in {"space", "folder", "list"}:
        raise ValueError("kind must be space, folder, or list")
    if not isinstance(placement, str) or placement not in {"before", "after"}:
        raise ValueError("placement must be before or after")
    limit = 1000 if kind == "list" else 200
    key = _strict_text(node_id, "node_id", limit=limit, required=True)
    target = _strict_text(target_id, "target_id", limit=limit, required=True)

    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _seed(conn)
        node = _node(conn, kind, key)
        target_node = _node(conn, kind, target)
        if not _same_parent(kind, node, target_node):
            raise ValueError("target must share the node parent")
        siblings = _sibling_nodes(conn, kind, node)
        ordered_ids = [sibling["node_id"] for sibling in siblings]
        if key != target:
            ordered_ids.remove(key)
            target_index = ordered_ids.index(target)
            ordered_ids.insert(target_index + (placement == "after"), key)
        _rewrite_sibling_positions(conn, kind, ordered_ids)
        return _public_node(conn, kind, key)


def create_working_directory(list_key: str, path: str) -> dict:
    key = _strict_text(list_key, "list_key", limit=1000, required=True)
    raw = _strict_text(path, "path", limit=10_000, required=True)
    candidate = os.path.abspath(os.path.expanduser(raw))
    # Check the spelled path before realpath: a dangling symlink otherwise
    # resolves to a nonexistent target and would be mistaken for a safe mkdir.
    if os.path.lexists(candidate):
        raise ValueError("working directory already exists")
    target = os.path.realpath(candidate)
    parent = os.path.dirname(target)
    conn = _conn()
    with conn:
        _seed(conn)
        _list(conn, key)
        if os.path.lexists(target):
            raise ValueError("working directory already exists")
        if not os.path.isdir(parent):
            raise ValueError("working directory parent must exist")
        # The filesystem step is intentionally after every validation.  If the
        # database commit fails afterwards, leave this one explicit directory
        # for the user to recover rather than trying recursive cleanup.
        os.mkdir(target)
        conn.execute(
            """UPDATE workspace_lists
               SET working_directory = ?, inherit_session_cwd = 0, updated_at = ?
               WHERE list_key = ?""",
            (target, time.time(), key),
        )
    return _public_list(_list(conn, key))


def _revision(*parts: object) -> str:
    encoded = json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _context_for_list(conn: sqlite3.Connection, listed: dict, *, session_id: str | None = None, effective_directory: str | None = None) -> dict:
    directory = listed["working_directory"] if effective_directory is None else effective_directory
    return {
        "list_key": listed["list_key"],
        "list_name": listed["name"],
        "space_id": listed["space_id"],
        "folder_id": listed["folder_id"],
        "working_directory": directory,
        "folder_status": _folder_status(directory),
        "launch_revision": _revision(session_id, listed["list_key"], directory),
    }


def context_for_task(task: dict, *, seeded: bool = False) -> dict:
    task_id = _strict_text(task.get("task_id") if isinstance(task, dict) else None, "task_id", limit=200, required=True)
    if not seeded:
        snapshot()
    conn = _conn()
    row = conn.execute("SELECT * FROM work_items WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError("task not found")
    current = dict(row)
    placement = conn.execute(
        "SELECT list_key, position FROM task_placements WHERE task_id = ?", (task_id,)
    ).fetchone()
    effective_key = placement["list_key"] if placement is not None else current["project_key"]
    listed = _list(conn, effective_key)
    inherited = (
        placement is None
        and listed["source_project_key"] == current["project_key"]
        and listed["inherit_session_cwd"]
    )
    directory = current["session_cwd"] if inherited else listed["working_directory"]
    context = _context_for_list(
        conn, listed, session_id=current["session_id"], effective_directory=directory
    )
    context["order"] = int(placement["position"] if placement is not None else current["position"])
    return context


def context_for_list(list_key: str, *, seeded: bool = False) -> dict:
    key = _strict_text(list_key, "list_key", limit=1000, required=True)
    if not seeded:
        snapshot()
    return _context_for_list(_conn(), _list(_conn(), key))


def _effective_key(conn: sqlite3.Connection, task: sqlite3.Row) -> str:
    if "placement_list_key" in task.keys():
        return str(task["placement_list_key"] or task["project_key"])
    placement = conn.execute(
        "SELECT list_key FROM task_placements WHERE task_id = ?", (task["task_id"],)
    ).fetchone()
    return str(placement["list_key"] if placement is not None else task["project_key"])


def move_task(task_id: str, list_key: str, expected_list_key: str) -> dict:
    task_key = _strict_text(task_id, "task_id", limit=200, required=True)
    destination = _strict_text(list_key, "list_key", limit=1000, required=True)
    expected = _strict_text(expected_list_key, "expected_list_key", limit=1000, required=True)
    snapshot()
    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _list(conn, destination)
        task = conn.execute("SELECT * FROM work_items WHERE task_id = ?", (task_key,)).fetchone()
        if task is None or not task["session_id"]:
            raise ValueError("task not found")
        current = _effective_key(conn, task)
        if current != expected:
            raise ValueError("task is no longer in the expected current list")
        if destination != current:
            if destination == task["project_key"]:
                conn.execute("DELETE FROM task_placements WHERE task_id = ?", (task_key,))
            else:
                position = _next_position(conn, "task_placements", "WHERE list_key = ?", (destination,))
                conn.execute(
                    """INSERT INTO task_placements (task_id, list_key, position, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(task_id) DO UPDATE SET list_key = excluded.list_key,
                           position = excluded.position, updated_at = excluded.updated_at""",
                    (task_key, destination, position, time.time()),
                )
    return context_for_task({"task_id": task_key}, seeded=True)


def _ids(values: object) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ValueError("task_ids must be a non-empty list")
    if len(values) > _REORDER_LIMIT:
        raise ValueError(f"task_ids may contain at most {_REORDER_LIMIT} items")
    ids = [_strict_text(value, "task_ids", limit=200, required=True) for value in values]
    if len(ids) != len(set(ids)):
        raise ValueError("task_ids must be unique")
    return ids


def reorder_tasks(list_key: str, task_ids: list[str], priority: str | None = None) -> list[dict]:
    key = _strict_text(list_key, "list_key", limit=1000, required=True)
    ids = _ids(task_ids)
    destination_priority = work_items._priority(priority) if priority is not None else None
    snapshot()
    placeholders = ",".join("?" for _ in ids)
    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _list(conn, key)
        rows = conn.execute(
            f"""SELECT work_items.*, task_placements.list_key AS placement_list_key,
                       task_placements.position AS placement_position
                FROM work_items LEFT JOIN task_placements USING (task_id)
                WHERE work_items.task_id IN ({placeholders})""",
            ids,
        ).fetchall()
        if len(rows) != len(ids):
            raise ValueError("task_ids must identify existing tasks")
        by_id = {row["task_id"]: row for row in rows}
        ordered = [by_id[item] for item in ids]
        if any(not item["session_id"] for item in ordered):
            raise ValueError("task_ids must be session-backed")
        if any(_effective_key(conn, item) != key for item in ordered):
            raise ValueError("task_ids must belong to the same list")
        statuses = {item["status"] for item in ordered}
        if len(statuses) != 1:
            raise ValueError("task_ids must belong to the same work-status group")
        if next(iter(statuses)) in {"done", "archived"} and destination_priority is not None:
            raise ValueError("closed tasks cannot change priority")
        slots = [
            int(item["placement_position"] if item["placement_position"] is not None else item["position"])
            for item in ordered
        ]
        now = time.time()
        for item, slot in zip(ordered, sorted(slots), strict=True):
            if item["placement_position"] is not None:
                conn.execute("UPDATE task_placements SET position = ?, updated_at = ? WHERE task_id = ?", (slot, now, item["task_id"]))
            else:
                conn.execute("UPDATE work_items SET position = ?, updated_at = ? WHERE task_id = ?", (slot, now, item["task_id"]))
            if destination_priority is not None:
                conn.execute("UPDATE work_items SET priority = ?, updated_at = ? WHERE task_id = ?", (destination_priority, now, item["task_id"]))
    return [work_items.get(task_id) for task_id in ids]

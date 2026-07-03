"""Local SQLite store for live agent-thread session state.

This is the hot-path source of truth: hooks and the statusline write/read it
synchronously, so it must stay fast and never touch the network. Cross-laptop
sync (Firestore/Slack) reads from here out-of-band, never the other way.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

_DB_PATH = Path(os.environ["AGENT_BOARD_DB_PATH"]) if os.environ.get(
    "AGENT_BOARD_DB_PATH"
) else Path.home() / ".claude" / "agent-board" / "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id         TEXT PRIMARY KEY,
    host               TEXT,
    cwd                TEXT,
    name               TEXT,
    name_source        TEXT,
    state              TEXT,
    working_since      REAL,
    heartbeat_at       REAL,
    updated_at         REAL,
    msg_count          INTEGER,
    named_at_msg_count INTEGER,
    pending_alert      TEXT
)
"""

_COLUMNS = (
    "session_id",
    "host",
    "cwd",
    "name",
    "name_source",
    "state",
    "working_since",
    "heartbeat_at",
    "updated_at",
    "msg_count",
    "named_at_msg_count",
    "pending_alert",
)

_COLUMN_TYPES = {
    "host": "TEXT",
    "cwd": "TEXT",
    "name": "TEXT",
    "name_source": "TEXT",
    "state": "TEXT",
    "working_since": "REAL",
    "heartbeat_at": "REAL",
    "updated_at": "REAL",
    "msg_count": "INTEGER",
    "named_at_msg_count": "INTEGER",
    "pending_alert": "TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any column in _COLUMNS missing from an already-existing table.

    CREATE TABLE IF NOT EXISTS only creates the table on first use -- it
    never adds columns to a table that already exists with an older schema.
    Without this, a new column added here would silently not exist on any
    machine that already ran an earlier version.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    for col in _COLUMNS:
        if col not in existing and col in _COLUMN_TYPES:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {_COLUMN_TYPES[col]}")


_conn_cache: sqlite3.Connection | None = None
_conn_cache_path: Path | None = None


def get_conn() -> sqlite3.Connection:
    """Return a connection, cached per-process (keyed by _DB_PATH).

    A hook invocation is a short-lived process, but several of its own
    calls often open a connection each (e.g. hook.py's Stop handler does
    get() then set_state() then heartbeat()) -- caching within that one
    process avoids re-running WAL/busy_timeout/migration setup 2-3x for a
    single logical operation. Keyed by _DB_PATH (not just cached globally)
    so tests that monkeypatch _DB_PATH to a fresh tmp_path per test still
    get an isolated connection rather than reusing a stale one.
    """
    global _conn_cache, _conn_cache_path
    if _conn_cache is not None and _conn_cache_path == _DB_PATH:
        return _conn_cache

    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute(_SCHEMA)
    _migrate(conn)
    _conn_cache = conn
    _conn_cache_path = _DB_PATH
    return conn


def upsert(session_id: str, **fields: object) -> None:
    """Insert or update a session row. Always bumps updated_at."""
    fields = dict(fields)
    fields["updated_at"] = time.time()
    unknown = set(fields) - set(_COLUMNS)
    if unknown:
        raise ValueError(f"Unknown session field(s): {sorted(unknown)}")

    cols = ["session_id", *fields.keys()]
    placeholders = ", ".join("?" for _ in cols)
    values = [session_id, *fields.values()]
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in fields)

    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(session_id) DO UPDATE SET {update_clause}",
            values,
        )


def get(session_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def active(max_age_hours: float = 24) -> list[dict]:
    """Rows not ended, or ended but updated within the window. Newest first."""
    cutoff = time.time() - (max_age_hours * 3600)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions "
            "WHERE state != 'ended' OR updated_at >= ? "
            "ORDER BY updated_at DESC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_state(
    session_id: str,
    state: str,
    *,
    cwd: str | None = None,
    working_since: float | None = None,
    host: str | None = None,
) -> None:
    fields: dict[str, object] = {"state": state}
    if cwd is not None:
        fields["cwd"] = cwd
    if working_since is not None:
        fields["working_since"] = working_since
    if host is not None:
        fields["host"] = host
    upsert(session_id, **fields)


def heartbeat(session_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET heartbeat_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )


def set_pending_alert(session_id: str, kind: str) -> None:
    """Mark that hook.py just fired a local notification of this kind
    ("done" or "needs-input") -- the single source of truth for "this
    transition matters enough to alert on", read by sync.py to decide
    whether to post a fresh Slack message (not just update the board)."""
    upsert(session_id, pending_alert=kind)


def clear_pending_alert(session_id: str) -> None:
    upsert(session_id, pending_alert=None)


#: Canonical state -> (sort rank, icon), shared by every renderer (cli.py's
#: `jobs`/`aj` board and sync.py's Slack board) so a new state only needs to
#: be added here once, not kept in sync across files.
STATE_ORDER = {"needs-input": 0, "working": 1, "idle": 2, "gone": 3, "ended": 4}
STATE_ICON = {
    "needs-input": "⏸️",
    "working": "◇",
    "idle": "✓",
    "gone": "☠",
    "ended": "·",
}


def display_state(row: dict, *, stale_after_s: int = 600) -> str:
    """Derive the state a renderer should show: 'gone' overrides a stale non-ended row.

    Uses .get() throughout, not row["state"] -- local SQLite rows always have
    a state column, but callers (sync.py) also feed this Firestore-derived
    dicts with no schema guarantee, where a missing/legacy doc could lack
    the key entirely.
    """
    state = row.get("state")
    if state == "ended":
        return "ended"
    last_seen = row.get("heartbeat_at") or row.get("updated_at")
    if last_seen is None or (time.time() - last_seen) > stale_after_s:
        return "gone"
    return state or "gone"


def _raw_set_updated_at(session_id: str, updated_at: float) -> None:
    """Test-only helper: force updated_at without bumping it to now()."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (updated_at, session_id),
        )

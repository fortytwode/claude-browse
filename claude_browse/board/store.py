"""Local SQLite store for live agent-thread session state.

This is the hot-path source of truth: hooks and the statusline write/read it
synchronously, so it must stay fast and never touch the network. Cross-laptop
sync (Firestore/Slack) reads from here out-of-band, never the other way.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_DB_PATH = Path.home() / ".claude" / "agent-board" / "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    host          TEXT,
    cwd           TEXT,
    name          TEXT,
    name_source   TEXT,
    state         TEXT,
    working_since REAL,
    heartbeat_at  REAL,
    updated_at    REAL,
    msg_count     INTEGER
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
)


def get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute(_SCHEMA)
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
) -> None:
    fields: dict[str, object] = {"state": state}
    if cwd is not None:
        fields["cwd"] = cwd
    if working_since is not None:
        fields["working_since"] = working_since
    upsert(session_id, **fields)


def heartbeat(session_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET heartbeat_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )


def display_state(row: dict, *, stale_after_s: int = 600) -> str:
    """Derive the state a renderer should show: 'gone' overrides a stale non-ended row."""
    if row["state"] == "ended":
        return "ended"
    last_seen = row.get("heartbeat_at") or row.get("updated_at")
    if last_seen is None or (time.time() - last_seen) > stale_after_s:
        return "gone"
    return row["state"]


def _raw_set_updated_at(session_id: str, updated_at: float) -> None:
    """Test-only helper: force updated_at without bumping it to now()."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (updated_at, session_id),
        )

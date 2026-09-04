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
    model_label        TEXT,
    pending_alert      TEXT,
    provider           TEXT,
    done_at            REAL,
    done_turn_s        REAL,
    acked_at           REAL
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
    "model_label",
    "pending_alert",
    "provider",
    "done_at",
    "done_turn_s",
    "acked_at",
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
    "model_label": "TEXT",
    "pending_alert": "TEXT",
    # Which CLI owns the session ("claude" / "codex"). Drives the resume
    # command on every surface; NULL on rows written before this column
    # existed, which every reader treats as "claude" (the only provider the
    # board tracked back then).
    "provider": "TEXT",
    # Unattended-completion tracking. done_at is set when a Stop closes a
    # turn long enough to count as "a run you may have walked away from"
    # (hook.py's _UNATTENDED_MIN_TURN_S); it's cleared by the next
    # UserPromptSubmit (you came back = implicit ack) and superseded by
    # acked_at (explicit ack: `agent-board ack`, or a Slack reaction read by
    # the team-operations sweep). The sweep and every board surface derive
    # "finished, not picked up" from these three fields -- see is_unattended.
    "done_at": "REAL",
    "done_turn_s": "REAL",
    "acked_at": "REAL",
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
    """Rows still active, recent, or awaiting completion acknowledgement."""
    cutoff = time.time() - (max_age_hours * 3600)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions "
            "WHERE state != 'ended' OR updated_at >= ? "
            "OR (done_at IS NOT NULL AND (acked_at IS NULL OR acked_at < done_at)) "
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
    model_label: str | None = None,
) -> None:
    fields: dict[str, object] = {"state": state}
    if cwd is not None:
        fields["cwd"] = cwd
    if working_since is not None:
        fields["working_since"] = working_since
    if host is not None:
        fields["host"] = host
    if model_label is not None:
        fields["model_label"] = model_label
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


DEFAULT_PROVIDER = "claude"


def provider_of(row: dict | None) -> str:
    """Provider id for a row; legacy rows (no column / NULL) are Claude."""
    return str((row or {}).get("provider") or DEFAULT_PROVIDER)


def mark_done(session_id: str, turn_s: float) -> None:
    """Record that a long turn just finished and nobody has come back yet."""
    upsert(session_id, done_at=time.time(), done_turn_s=float(turn_s), acked_at=None)


def clear_done(session_id: str) -> None:
    """The user returned to the session (new prompt): nothing is waiting."""
    upsert(session_id, done_at=None, done_turn_s=None)


def ack(session_id: str) -> None:
    """Explicit acknowledgement: the user saw the completion, stop nagging."""
    upsert(session_id, acked_at=time.time())


def is_unattended(row: dict | None, *, now: float | None = None) -> bool:
    """True when a long turn finished and the user has neither prompted
    again nor acknowledged it. Ended sessions still count: a run that
    finished and whose terminal was closed is exactly the thread the user
    is most likely to forget. 'working' never counts (a new turn is in
    flight, so someone is there)."""
    del now  # reserved for callers that want an age cutoff later
    if not row:
        return False
    done_at = row.get("done_at")
    if not done_at:
        return False
    if row.get("state") == "working":
        return False
    acked_at = row.get("acked_at")
    if acked_at and acked_at >= done_at:
        return False
    return True


def unattended(max_age_hours: float = 24) -> list[dict]:
    """Active rows (per active()) that are unattended, oldest completion first."""
    rows = [r for r in active(max_age_hours=max_age_hours) if is_unattended(r)]
    rows.sort(key=lambda r: r.get("done_at") or 0)
    return rows


def find(query: str, max_age_hours: float = 24 * 7) -> list[dict]:
    """Rows whose session_id starts with `query` or whose name contains it
    (case-insensitive). Used by `agent-board ack <id-or-name>`."""
    q = query.strip().lower()
    if not q:
        return []
    matches = []
    for row in active(max_age_hours=max_age_hours):
        sid = str(row.get("session_id") or "").lower()
        name = str(row.get("name") or "").lower()
        if sid.startswith(q) or q in name:
            matches.append(row)
    return matches


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

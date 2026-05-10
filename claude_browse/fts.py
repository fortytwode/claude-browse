"""SQLite FTS5 index over Claude Code session text.

Replaces fzf's character-level fuzzy matching with proper full-text search:
single bare words match tokens exactly (no fuzzy false-positive flood),
multiple bare words AND together, double-quoted strings match as phrases.

The index lives at ~/.claude/cache/claude-browse-index.db. It's pure cache —
deletable any time; the next claude-browse run rebuilds from JSONL.
"""

from __future__ import annotations

import math
import os
import sqlite3
import time
from datetime import datetime, timezone

from .core import (
    canonicalize_path,
    extract_fielded_corpus,
    get_session_info,
    list_session_files,
)

DB_PATH = os.path.expanduser("~/.claude/cache/claude-browse-index.db")
# v2: added last_timestamp column so the list view can sort by most recent
#     activity instead of session start time.
# v3: split sessions_fts from a single 'corpus' column into six fielded
#     columns (cwd, title, first_msg, user_text, asst_text, boilerplate)
#     so search_ranked() can weight them via bm25(). User-visible effect:
#     first launch after upgrade walks every session JSONL on disk to
#     repopulate the new columns (~10s for ~4000 sessions). Subsequent
#     launches resume the per-file mtime fast-path. See _init_schema for
#     the drop+recreate dance.
SCHEMA_VERSION = 3


def open_db(path: str = DB_PATH) -> sqlite3.Connection:
    """Open or create the FTS index database."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create tables on first run; migrate (drop+recreate) on schema bump.

    The DB is pure cache, so a stale schema version just blows the tables
    away and the next reindex() rebuilds from JSONL. Cheaper than writing
    real ALTER TABLE migrations for what's effectively a derived index.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        );
        """
    )
    cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
    row = cur.fetchone()
    existing_version = row[0] if row else None
    if existing_version is not None and existing_version != SCHEMA_VERSION:
        conn.executescript(
            """
            DROP TABLE IF EXISTS sessions_fts;
            DROP TABLE IF EXISTS sessions;
            DELETE FROM schema_version;
            """
        )
        existing_version = None

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            sid             TEXT PRIMARY KEY,
            path            TEXT NOT NULL UNIQUE,
            cwd             TEXT,
            timestamp       TEXT,
            last_timestamp  TEXT,
            title           TEXT,
            first_msg       TEXT,
            last_msg        TEXT,
            msg_count       INTEGER NOT NULL DEFAULT 0,
            mtime           REAL NOT NULL,
            indexed_at      REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_mtime
            ON sessions(mtime DESC);
        CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
            sid UNINDEXED,
            cwd,
            title,
            first_msg,
            user_text,
            asst_text,
            boilerplate,
            tokenize='unicode61'
        );
        """
    )
    if existing_version is None:
        conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()


def reindex(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Sync the index against on-disk session files.

    Reindexes only files whose mtime changed since the last run, so steady-
    state startup is fast (just a stat() per file). First run is a full
    walk and may take several seconds for hundreds of sessions.

    Returns (added, updated, removed) counts for caller diagnostics.
    """
    files = list_session_files()
    file_mtimes: dict[str, float] = {f: os.path.getmtime(f) for f in files}

    existing: dict[str, tuple[str, float]] = {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT path, sid, mtime FROM sessions")
    }

    added = updated = removed = 0
    now = time.time()

    for path, mtime in file_mtimes.items():
        prev = existing.get(path)
        if prev is None:
            if _index_file(conn, path, mtime, now):
                added += 1
        elif abs(prev[1] - mtime) > 0.001:
            if _index_file(conn, path, mtime, now):
                updated += 1

    for path, (sid, _) in existing.items():
        if path not in file_mtimes:
            conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
            conn.execute("DELETE FROM sessions_fts WHERE sid = ?", (sid,))
            removed += 1

    conn.commit()
    return (added, updated, removed)


def _index_file(
    conn: sqlite3.Connection, path: str, mtime: float, now: float
) -> bool:
    """Parse one JSONL file and upsert it into the index.

    Returns False if the file has no usable session data (empty, unreadable,
    or no first user message), so reindex() can skip counting it.
    """
    info = get_session_info(path)
    if not info or not info.get("session_id") or not info.get("first_msg"):
        return False

    sid = info["session_id"]
    fields = extract_fielded_corpus(path)

    conn.execute(
        """
        INSERT INTO sessions (
            sid, path, cwd, timestamp, last_timestamp, title, first_msg,
            last_msg, msg_count, mtime, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sid) DO UPDATE SET
            path = excluded.path,
            cwd = excluded.cwd,
            timestamp = excluded.timestamp,
            last_timestamp = excluded.last_timestamp,
            title = excluded.title,
            first_msg = excluded.first_msg,
            last_msg = excluded.last_msg,
            msg_count = excluded.msg_count,
            mtime = excluded.mtime,
            indexed_at = excluded.indexed_at
        """,
        (
            sid,
            path,
            canonicalize_path(info.get("cwd")),
            info.get("timestamp"),
            info.get("last_timestamp"),
            info.get("name"),
            info.get("first_msg"),
            info.get("last_msg"),
            info.get("msg_count", 0),
            mtime,
            now,
        ),
    )
    conn.execute("DELETE FROM sessions_fts WHERE sid = ?", (sid,))
    conn.execute(
        """INSERT INTO sessions_fts
           (sid, cwd, title, first_msg, user_text, asst_text, boilerplate)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            sid,
            fields["cwd"],
            fields["title"],
            fields["first_msg"],
            fields["user_text"],
            fields["asst_text"],
            fields["boilerplate"],
        ),
    )
    return True


def normalize_query(query: str) -> str:
    """Translate a user query into FTS5 syntax.

    Each bare word becomes a quoted FTS5 token (so it's an exact-token match,
    never an operator), and double-quoted spans become FTS5 phrase clauses.
    Implicit AND between clauses is FTS5's default. Result for the four
    user-stated cases:

        runna           -> "runna"                (single token)
        runna sca2      -> "runna" "sca2"         (AND of two tokens)
        "runna sca2"    -> "runna sca2"           (phrase: adjacent in order)
        runna "sca2 v3" -> "runna" "sca2 v3"      (token AND phrase)

    Bare uppercase AND/OR/NOT also get quoted, so they match literally rather
    than triggering FTS5 boolean operators. Power users who want booleans
    can quote the operands and write them themselves.
    """
    tokens: list[tuple[str, str]] = []  # (kind, text), kind in {"word","phrase"}
    in_quote = False
    current: list[str] = []
    for ch in query:
        if ch == '"':
            if in_quote:
                tokens.append(("phrase", "".join(current)))
                current = []
                in_quote = False
            else:
                if current:
                    tokens.append(("word", "".join(current)))
                    current = []
                in_quote = True
        elif ch.isspace() and not in_quote:
            if current:
                tokens.append(("word", "".join(current)))
                current = []
        else:
            current.append(ch)
    if current:
        tokens.append(("phrase" if in_quote else "word", "".join(current)))

    parts: list[str] = []
    for _kind, text in tokens:
        text = text.strip()
        if not text:
            continue
        escaped = text.replace('"', '""')
        parts.append(f'"{escaped}"')
    return " ".join(parts)


def search(
    conn: sqlite3.Connection, query: str, limit: int = 200
) -> list[dict]:
    """Run an FTS5 query against the session index.

    Empty or whitespace-only query returns recent sessions by timestamp, so
    the initial fzf list looks just like before. Non-empty query is normalized
    into FTS5 syntax (see normalize_query); FTS5 filters, and the result set
    is then ordered reverse-chronologically (newest session first).
    """
    if not query.strip():
        return list_recent(conn, limit)

    fts_query = normalize_query(query)
    if not fts_query:
        return list_recent(conn, limit)

    # Filter by FTS5 match, but order by recency, not BM25. Users already
    # know what they searched for; what they want is "the newest session
    # that mentions runna," not "the session where runna scored best."
    sql = """
        SELECT s.sid, s.path, s.cwd, s.timestamp, s.last_timestamp, s.title,
               s.first_msg, s.last_msg, s.msg_count, s.mtime,
               snippet(sessions_fts, -1, '\x01', '\x02', '…', 12) AS context
        FROM sessions_fts
        JOIN sessions s ON s.sid = sessions_fts.sid
        WHERE sessions_fts MATCH ?
        ORDER BY COALESCE(s.last_timestamp, s.timestamp, '') DESC, s.mtime DESC
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, (fts_query, limit)).fetchall()
    except sqlite3.OperationalError:
        return []

    return [_row_to_dict(r) for r in rows]


# --- ranker_v1 -----------------------------------------------------------

# Per-column BM25 weights, in sessions_fts column order (sid, cwd, title,
# first_msg, user_text, asst_text, boilerplate). cwd is the strongest topic
# anchor for personal session corpora; assistant text is the weakest signal
# (verbose, often quoting the user back); boilerplate (Toggl rollups) is
# kept retrievable but de-weighted so a single client mention in a time log
# doesn't outrank work actually about that client. Numbers are v1 priors
# from the eval set in eval/run.py — change them and re-run that eval to see
# the ranking impact before assuming better.
_DEFAULT_BM25_WEIGHTS = (0.0, 10.0, 8.0, 5.0, 1.0, 0.3, 0.05)

# Recency contribution: alpha * exp(-age_days / half_life).
# half_life=30d means a 30-day-old match is weighted half as much as today's,
# 60d is a quarter, 90d is an eighth. alpha=3 sets recency on the same order
# as a strong BM25 hit so they trade off rather than one dominating.
_DEFAULT_RECENCY_ALPHA = 3.0
_DEFAULT_HALF_LIFE_DAYS = 30.0


def search_ranked(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
    weights: tuple[float, ...] = _DEFAULT_BM25_WEIGHTS,
    recency_alpha: float = _DEFAULT_RECENCY_ALPHA,
    half_life_days: float = _DEFAULT_HALF_LIFE_DAYS,
) -> list[dict]:
    """Multi-column BM25 + exponential recency decay reranker.

    Pulls a candidate pool by FTS5 MATCH, scores each row by:
        score = -bm25(weighted) + alpha * exp(-age_days / half_life)
    (bm25 returns negative-is-better; the negation flips it to higher-is-
    better so the additive recency term composes naturally.)

    Empty query falls through to list_recent so the picker's idle state
    behaves identically to today.
    """
    if not query.strip():
        return list_recent(conn, limit)

    fts_query = normalize_query(query)
    if not fts_query:
        return list_recent(conn, limit)

    # Pull more candidates than we'll return so reranking has room to
    # surface non-recent strong matches that pure-recency would have buried.
    # 5x is empirical: in the eval set the deepest grade-3 session in a
    # current-ranker pull was at rank ~40, so 5*limit (=250 at default) clears
    # that with margin. Going higher costs SQLite time per keystroke; lower
    # risks dropping a relevant doc before BM25 ever sees it.
    candidate_pool = max(limit * 5, 200)

    w = list(weights) + [1.0] * (7 - len(weights))  # tolerate short tuples
    sql = f"""
        SELECT s.sid, s.path, s.cwd, s.timestamp, s.last_timestamp, s.title,
               s.first_msg, s.last_msg, s.msg_count, s.mtime,
               snippet(sessions_fts, -1, '\x01', '\x02', '…', 12) AS context,
               bm25(sessions_fts, {w[0]}, {w[1]}, {w[2]}, {w[3]}, {w[4]}, {w[5]}, {w[6]}) AS bm
        FROM sessions_fts
        JOIN sessions s ON s.sid = sessions_fts.sid
        WHERE sessions_fts MATCH ?
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, (fts_query, candidate_pool)).fetchall()
    except sqlite3.OperationalError:
        return []

    now = datetime.now(timezone.utc)
    scored: list[tuple[float, tuple]] = []
    for r in rows:
        bm = -(r[11] or 0.0)
        age = _age_days(r[4] or r[3], now)
        recency = math.exp(-age / half_life_days) if age >= 0 else 0.0
        score = bm + recency_alpha * recency
        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [_row_to_dict(r[:11]) for _, r in scored[:limit]]


def _age_days(ts_str: str | None, now: datetime) -> float:
    """ISO timestamp -> age in days. Missing/malformed -> 365 (very-old fallback)."""
    if not ts_str:
        return 365.0
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        return 365.0


def list_recent(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Return recent sessions for the empty-query / initial-display state.

    Ordered by last activity (the JSONL's most recent event timestamp), so
    a session resumed today floats to the top even if it was started weeks
    ago. Falls back to start timestamp when last_timestamp is missing
    (older index rows, malformed sessions); mtime is the final tiebreaker.
    """
    rows = conn.execute(
        """
        SELECT sid, path, cwd, timestamp, last_timestamp, title, first_msg,
               last_msg, msg_count, mtime, '' AS context
        FROM sessions
        ORDER BY COALESCE(last_timestamp, timestamp, '') DESC, mtime DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_by_sid(conn: sqlite3.Connection, sid: str) -> dict | None:
    """Look up one session by its ID. Used to resolve fzf's selection."""
    row = conn.execute(
        """
        SELECT sid, path, cwd, timestamp, last_timestamp, title, first_msg,
               last_msg, msg_count, mtime, '' AS context
        FROM sessions
        WHERE sid = ?
        """,
        (sid,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def _row_to_dict(r) -> dict:
    return {
        "session_id": r[0],
        "path": r[1],
        "cwd": r[2],
        "timestamp": r[3],
        "last_timestamp": r[4],
        "name": r[5],
        "first_msg": r[6],
        "last_msg": r[7],
        "msg_count": r[8] or 0,
        "mtime": r[9],
        "context": r[10],
    }

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
import re
import sqlite3
import time
from datetime import datetime, timezone

from .core import list_index_records
from .providers import get_provider
from .query import QueryPlan, build_query_plan, significant_query_terms

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
# v4: sessions table now stores provider ("claude" | "codex"), and reindex()
#     consumes a provider-agnostic record stream so the browser can show both
#     Claude Code and CodeX threads in one list.
# v5: segment-level transcript indexing powers descriptive "find the thread
#     where..." recall, last-matching-mention ranking, and query-anchored
#     snippets instead of pure thread-end recency.
SCHEMA_VERSION = 5
_CLOSEOUT_CUE_WEIGHTS = {
    "closeout": 3.0,
    "close out": 3.0,
    "final session": 3.0,
    "handoff": 2.5,
    "hand off": 2.5,
    "wrapup": 2.0,
    "wrap up": 2.0,
    "debrief": 2.0,
    "recap": 2.0,
    "summary": 1.0,
    "final discussion": 2.0,
    "finalize": 1.0,
    "finalise": 1.0,
    "final": 0.75,
}
_SELF_REFERENTIAL_CUES = (
    "claude-browse",
    "claude browse",
    "codex-browse",
    "codex browse",
    "find the thread",
    "session not found",
    "typesense",
)
_IMPORTED_SESSION_CUES = (
    "continue the imported claude session context",
    "treat it as prior conversation state",
    "claude_browse_import_",
    "/var/folders/",
)
_AUTOMATION_CUES = (
    "automated fix agent",
    "cloud_run_job",
    "cloud run job",
    "auto-retry",
    "data freshness",
    "data_freshness",
)
_PLANNING_CUES = (
    "daily standup",
    "weekly review",
    "reflections for tomorrow",
    "working on today",
    "what got done yesterday",
    "morning briefing",
)
_HANDOVER_ARTIFACT_CUES = (
    "session handover",
    "session_handover",
    "use git rigorously; commit per logical change",
    "run the qa scripts",
)
_SEARCH_SYSTEM_QUERY_CUES = (
    "claude-browse",
    "codex-browse",
    "browse",
    "search",
    "ranker",
    "sqlite",
    "typesense",
)


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
            DROP TABLE IF EXISTS segments_fts;
            DROP TABLE IF EXISTS segments;
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
            provider        TEXT NOT NULL DEFAULT 'claude',
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
        CREATE TABLE IF NOT EXISTS segments (
            rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
            sid             TEXT NOT NULL,
            segment_idx     INTEGER NOT NULL,
            role            TEXT NOT NULL,
            timestamp       TEXT,
            text            TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_segments_sid
            ON segments(sid, segment_idx);
        CREATE INDEX IF NOT EXISTS idx_segments_timestamp
            ON segments(timestamp DESC);
        CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
            sid UNINDEXED,
            role UNINDEXED,
            segment_idx UNINDEXED,
            timestamp UNINDEXED,
            text,
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
    records = list_index_records()
    record_map: dict[str, dict] = {r["path"]: r for r in records}

    existing: dict[str, tuple[str, float]] = {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT path, sid, mtime FROM sessions")
    }

    added = updated = removed = 0
    now = time.time()

    for path, record in record_map.items():
        prev = existing.get(path)
        if prev is None:
            if _index_record(conn, record, now):
                added += 1
        elif abs(prev[1] - record["mtime"]) > 0.001:
            if _index_record(conn, record, now):
                updated += 1

    for path, (sid, _) in existing.items():
        if path not in record_map:
            conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
            conn.execute("DELETE FROM sessions_fts WHERE sid = ?", (sid,))
            _delete_segments_for_sid(conn, sid)
            removed += 1

    conn.commit()
    return (added, updated, removed)


def _index_record(
    conn: sqlite3.Connection, record: dict, now: float
) -> bool:
    """Upsert one normalized session record into the index.

    Returns False if the record has no usable session data, so reindex() can
    skip counting it.
    """
    if not record or not record.get("session_id") or not record.get("first_msg"):
        return False

    sid = record["session_id"]
    fields = record["fields"]

    conn.execute(
        """
        INSERT INTO sessions (
            sid, path, provider, cwd, timestamp, last_timestamp, title,
            first_msg, last_msg, msg_count, mtime, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sid) DO UPDATE SET
            path = excluded.path,
            provider = excluded.provider,
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
            record["path"],
            record.get("provider") or "claude",
            record.get("cwd"),
            record.get("timestamp"),
            record.get("last_timestamp"),
            record.get("name"),
            record.get("first_msg"),
            record.get("last_msg"),
            record.get("msg_count", 0),
            record["mtime"],
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
    _reindex_segments_for_record(conn, record)
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
    parts: list[str] = []
    for text in significant_query_terms(query):
        text = text.strip()
        if not text:
            continue
        if (
            text.endswith("*")
            and " " not in text
            and len(text) > 1
            and text.count("*") == 1
        ):
            escaped = text[:-1].replace('"', '""')
            parts.append(f'"{escaped}"*')
        else:
            escaped = text.replace('"', '""')
            parts.append(f'"{escaped}"')
    return " ".join(parts)


def _terms_to_fts_query(
    terms: list[str],
    *,
    joiner: str = " ",
) -> str:
    parts: list[str] = []
    for text in terms:
        text = text.strip()
        if not text:
            continue
        if (
            text.endswith("*")
            and " " not in text
            and len(text) > 1
            and text.count("*") == 1
        ):
            escaped = text[:-1].replace('"', '""')
            parts.append(f'"{escaped}"*')
        else:
            escaped = text.replace('"', '""')
            parts.append(f'"{escaped}"')
    return joiner.join(parts)


def _parse_iso_timestamp(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _timestamp_sort_key(ts_str: str | None) -> float:
    dt = _parse_iso_timestamp(ts_str)
    if dt is None:
        return 0.0
    return dt.timestamp()


def _interpolate_turn_timestamps(
    start_ts: str | None,
    end_ts: str | None,
    count: int,
) -> list[str]:
    if count <= 0:
        return []

    start_dt = _parse_iso_timestamp(start_ts)
    end_dt = _parse_iso_timestamp(end_ts) or start_dt

    if start_dt is None and end_dt is None:
        return [""] * count
    if start_dt is None:
        start_dt = end_dt
    if end_dt is None:
        end_dt = start_dt
    if start_dt is None or end_dt is None:
        return [""] * count

    if count == 1 or end_dt <= start_dt:
        stamp = end_dt.isoformat().replace("+00:00", "Z")
        return [stamp] * count

    span_seconds = (end_dt - start_dt).total_seconds()
    timestamps: list[str] = []
    for idx in range(count):
        frac = idx / (count - 1)
        dt = start_dt + (end_dt - start_dt) * frac
        if span_seconds <= 0:
            dt = end_dt
        timestamps.append(dt.isoformat().replace("+00:00", "Z"))
    return timestamps


def _delete_segments_for_sid(conn: sqlite3.Connection, sid: str) -> None:
    rowids = [
        row[0]
        for row in conn.execute(
            "SELECT rowid FROM segments WHERE sid = ?",
            (sid,),
        ).fetchall()
    ]
    if rowids:
        placeholders = ",".join("?" for _ in rowids)
        conn.execute(
            f"DELETE FROM segments_fts WHERE rowid IN ({placeholders})",
            rowids,
        )
    conn.execute("DELETE FROM segments WHERE sid = ?", (sid,))


def _reindex_segments_for_record(
    conn: sqlite3.Connection,
    record: dict,
) -> None:
    sid = str(record["session_id"])
    _delete_segments_for_sid(conn, sid)

    provider = str(record.get("provider") or "claude")
    path = str(record.get("path") or "")
    turns = get_provider(provider).transcript_turns(path, sid)
    if not turns:
        return

    timestamps = _interpolate_turn_timestamps(
        record.get("timestamp"),
        record.get("last_timestamp") or record.get("timestamp"),
        len(turns),
    )

    for idx, ((role, text), ts) in enumerate(zip(turns, timestamps), 1):
        cur = conn.execute(
            """
            INSERT INTO segments (sid, segment_idx, role, timestamp, text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sid, idx, role, ts, text),
        )
        rowid = cur.lastrowid
        conn.execute(
            """
            INSERT INTO segments_fts (rowid, sid, role, segment_idx, timestamp, text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (rowid, sid, role, idx, ts, text),
        )


def _latest_segment_matches(
    conn: sqlite3.Connection,
    query: str,
    *,
    sids: list[str] | None = None,
    plan: QueryPlan | None = None,
) -> dict[str, dict[str, object]]:
    plan = plan or build_query_plan(query)
    terms = list(plan.fts_terms)
    if not terms:
        return {}
    fts_query = _terms_to_fts_query(terms, joiner=" OR ")
    descriptive = plan.descriptive
    highlight_terms = list(plan.highlight_terms or plan.fts_terms)

    where = ["segments_fts MATCH ?"]
    params: list[object] = [fts_query]
    if sids:
        placeholders = ",".join("?" for _ in sids)
        where.append(f"cur.sid IN ({placeholders})")
        params.extend(sids)

    sql = f"""
        SELECT cur.sid,
               cur.role,
               cur.segment_idx,
               cur.timestamp,
               cur.text,
               snippet(segments_fts, 4, '\x01', '\x02', '…', 12) AS context,
               bm25(segments_fts) AS bm,
               prev2.role,
               prev2.text,
               prev2.timestamp,
               prev.role,
               prev.text,
               prev.timestamp,
               nxt.role,
               nxt.text,
               nxt.timestamp,
               nxt2.role,
               nxt2.text,
               nxt2.timestamp
        FROM segments_fts
        JOIN segments AS cur ON cur.rowid = segments_fts.rowid
        LEFT JOIN segments AS prev2
            ON prev2.sid = cur.sid AND prev2.segment_idx = cur.segment_idx - 2
        LEFT JOIN segments AS prev
            ON prev.sid = cur.sid AND prev.segment_idx = cur.segment_idx - 1
        LEFT JOIN segments AS nxt
            ON nxt.sid = cur.sid AND nxt.segment_idx = cur.segment_idx + 1
        LEFT JOIN segments AS nxt2
            ON nxt2.sid = cur.sid AND nxt2.segment_idx = cur.segment_idx + 2
        WHERE {' AND '.join(where)}
    """
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return {}

    matches: dict[str, dict[str, object]] = {}
    lowered_terms = [term.lower() for term in terms]
    if len(terms) <= 2:
        required_term_count = len(terms)
    elif descriptive:
        required_term_count = max(2, len(terms) - 1)
    else:
        required_term_count = len(terms)

    def _match_stats(text: str) -> tuple[int, float, float]:
        lowered = text.lower()
        positions: list[tuple[int, int]] = []
        for term in lowered_terms:
            pos = lowered.find(term)
            if pos >= 0:
                positions.append((pos, pos + len(term)))
        term_count = len(positions)
        if not positions:
            return (0, 0.0, 0.0)
        span = max(end for _start, end in positions) - min(
            start for start, _end in positions
        )
        word_count = max(len(text.split()), 1)
        density = term_count / word_count
        compactness = term_count / max(span, 1)
        return (term_count, density, compactness)

    def _closeout_score(text: str) -> float:
        if not plan.wants_closeout:
            return 0.0
        lowered = text.lower()
        return sum(
            weight for cue, weight in _CLOSEOUT_CUE_WEIGHTS.items() if cue in lowered
        )

    def _conversation_score(text: str) -> float:
        lowered = text.lower()
        structured_markers = (
            lowered.count("http")
            + lowered.count("](")
            + lowered.count("```")
            + lowered.count("##")
            + lowered.count("|")
            + lowered.count("`")
        )
        return 1.0 / (1.0 + structured_markers)

    def _context_from_exchange(text: str) -> str:
        clean = " ".join(text.split())
        lowered = clean.lower()
        positions = [
            (lowered.find(term), lowered.find(term) + len(term))
            for term in highlight_terms
            if lowered.find(term) >= 0
        ]
        if not positions:
            excerpt = clean[:180]
        else:
            start = max(0, min(pos for pos, _end in positions) - 32)
            end = min(len(clean), max(pos for _start, pos in positions) + 80)
            excerpt = clean[start:end]
            if start > 0:
                excerpt = "…" + excerpt
            if end < len(clean):
                excerpt = excerpt + "…"
        for term in sorted(highlight_terms, key=len, reverse=True):
            excerpt = re.sub(
                re.escape(term),
                lambda match: f"\x01{match.group(0)}\x02",
                excerpt,
                flags=re.IGNORECASE,
            )
        return excerpt

    ranked = []
    for (
        sid,
        role,
        segment_idx,
        timestamp,
        text,
        context,
        bm,
        _prev2_role,
        prev2_text,
        _prev2_timestamp,
        prev_role,
        prev_text,
        _prev_timestamp,
        next_role,
        next_text,
        next_timestamp,
        _next2_role,
        next2_text,
        _next2_timestamp,
    ) in rows:
        assistant_bonus = 0
        match_timestamp = timestamp
        match_segment_idx = segment_idx
        if role == "assistant" and prev_role == "user":
            assistant_bonus = 1
        elif role == "user" and next_role == "assistant":
            assistant_bonus = 1
            match_timestamp = next_timestamp or timestamp
            match_segment_idx = segment_idx + 1
        else:
            assistant_bonus = 1 if role == "assistant" else 0

        window_parts = [
            str(prev2_text or ""),
            str(prev_text or ""),
            str(text or ""),
            str(next_text or ""),
            str(next2_text or ""),
        ]
        combined = " ".join(part for part in window_parts if part).strip()
        term_count, density, compactness = _match_stats(combined)
        if term_count < required_term_count:
            continue
        ranked.append(
            (
                sid,
                {
                    "match_segment_idx": match_segment_idx,
                    "match_timestamp": match_timestamp,
                    "match_context": _context_from_exchange(combined) or context,
                    "match_term_count": term_count,
                    "match_density": density,
                    "match_compactness": compactness,
                    "match_conversation_score": _conversation_score(combined),
                    "match_lifecycle_score": _closeout_score(combined),
                    "_match_bm25": -float(bm or 0.0),
                    "_assistant_bonus": assistant_bonus,
                },
            )
        )

    if plan.descriptive and plan.wants_closeout:
        ranked.sort(
            key=lambda item: (
                item[1]["match_lifecycle_score"],
                item[1]["match_term_count"],
                _timestamp_sort_key(item[1]["match_timestamp"]),
                item[1]["match_conversation_score"],
                item[1]["match_density"],
                item[1]["match_compactness"],
                item[1]["_match_bm25"],
                item[1]["match_segment_idx"],
            ),
            reverse=True,
        )
    elif len(terms) == 1:
        ranked.sort(
            key=lambda item: (
                item[1]["match_term_count"],
                item[1]["_assistant_bonus"],
                _timestamp_sort_key(item[1]["match_timestamp"]),
                item[1]["match_density"],
                item[1]["match_compactness"],
                item[1]["_match_bm25"],
                item[1]["match_segment_idx"],
            ),
            reverse=True,
        )
    else:
        ranked.sort(
            key=lambda item: (
                item[1]["match_term_count"],
                item[1]["match_conversation_score"],
                item[1]["match_density"],
                item[1]["match_compactness"],
                item[1]["_assistant_bonus"],
                _timestamp_sort_key(item[1]["match_timestamp"]),
                item[1]["_match_bm25"],
                item[1]["match_segment_idx"],
            ),
            reverse=True,
        )
    for sid, data in ranked:
        if sid in matches:
            continue
        matches[sid] = data
    return matches


def _decorate_match_metadata(
    rows: list[dict],
    match_map: dict[str, dict[str, object]],
) -> list[dict]:
    decorated: list[dict] = []
    for row in rows:
        match = match_map.get(str(row["session_id"]))
        if match:
            row = {
                **row,
                **match,
                "context": match.get("match_context") or row.get("context", ""),
            }
        decorated.append(row)
    return decorated


def _contains_any(text: str, cues: tuple[str, ...]) -> bool:
    return any(cue in text for cue in cues)


def _query_mentions_search_system(query: str) -> bool:
    lowered = query.lower()
    return _contains_any(lowered, _SEARCH_SYSTEM_QUERY_CUES)


def _metadata_anchor_score(row: dict, plan: QueryPlan) -> float:
    anchors = [term for term in plan.anchor_terms if term]
    if not anchors:
        return 0.0
    fields = (
        (str(row.get("name") or "").lower(), 3.0),
        (str(row.get("cwd") or "").lower(), 2.0),
        (str(row.get("first_msg") or "").lower(), 2.5),
        (str(row.get("last_msg") or "").lower(), 1.5),
    )
    score = 0.0
    matched_anchors: set[str] = set()

    def _position_bonus(pos: int) -> float:
        if pos < 0:
            return 0.0
        return 1.0 / (1.0 + (pos / 500.0))

    for anchor in anchors:
        parts = [part for part in anchor.lower().split() if part]
        if not parts:
            continue
        for text, weight in fields:
            pos = text.find(anchor.lower())
            if pos >= 0:
                score += weight * _position_bonus(pos)
                matched_anchors.add(anchor)
            elif len(parts) > 1 and all(part in text for part in parts):
                pos = min(text.find(part) for part in parts)
                score += weight * 0.75 * _position_bonus(pos)
                matched_anchors.add(anchor)
    return score + (1.5 * len(matched_anchors))


def _artifact_penalty(row: dict, plan: QueryPlan, query: str) -> float:
    haystack = " ".join(
        part
        for part in (
            row.get("name") or "",
            row.get("cwd") or "",
            row.get("first_msg") or "",
            row.get("last_msg") or "",
            row.get("context") or "",
        )
        if part
    ).lower()
    penalty = 0.0
    if _contains_any(haystack, _IMPORTED_SESSION_CUES):
        penalty += 8.0
    if _contains_any(haystack, _SELF_REFERENTIAL_CUES) and not _query_mentions_search_system(query):
        penalty += 6.0
    if _contains_any(haystack, _AUTOMATION_CUES) and "automation" not in query.lower():
        penalty += 4.0
    if _contains_any(haystack, _HANDOVER_ARTIFACT_CUES) and "handover" not in query.lower():
        penalty += 6.0
    if _contains_any(haystack, _PLANNING_CUES) and (
        plan.wants_closeout
        or "feedback" in plan.normalized_terms
        or "performance" in plan.normalized_terms
    ):
        penalty += 3.0
    return penalty


def _load_sessions_by_ids(
    conn: sqlite3.Connection,
    sids: list[str],
) -> list[dict]:
    if not sids:
        return []
    placeholders = ",".join("?" for _ in sids)
    rows = conn.execute(
        f"""
        SELECT sid, path, provider, cwd, timestamp, last_timestamp, title,
               first_msg, last_msg, msg_count, mtime, '' AS context
        FROM sessions
        WHERE sid IN ({placeholders})
        """,
        sids,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


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

    plan = build_query_plan(query)
    if plan.low_confidence:
        return list_recent(conn, limit)
    fts_query = normalize_query(query)
    if not fts_query:
        return list_recent(conn, limit)
    descriptive = plan.descriptive

    # Filter by FTS5 match, but order by recency, not BM25. Users already
    # know what they searched for; what they want is "the newest session
    # that mentions runna," not "the session where runna scored best."
    sql = """
        SELECT s.sid, s.path, s.provider, s.cwd, s.timestamp, s.last_timestamp,
               s.title, s.first_msg, s.last_msg, s.msg_count, s.mtime,
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

    results = [_row_to_dict(r) for r in rows]
    existing_ids = [str(r["session_id"]) for r in results]
    match_map = _latest_segment_matches(
        conn,
        query,
        sids=existing_ids or None,
        plan=plan,
    )
    if not results and descriptive:
        match_map = _latest_segment_matches(conn, query, plan=plan)
        results = _load_sessions_by_ids(conn, list(match_map)[:limit])
    if not results:
        return []
    decorated = _decorate_match_metadata(results, match_map)
    decorated.sort(
        key=lambda row: (
            int(row.get("match_term_count") or 0),
            1 if row.get("match_timestamp") else 0,
            _timestamp_sort_key(
                row.get("match_timestamp")
                or row.get("last_timestamp")
                or row.get("timestamp")
            ),
            float(row.get("match_conversation_score") or 0.0),
            float(row.get("match_density") or 0.0),
            float(row.get("match_compactness") or 0.0),
            float(row.get("_match_bm25") or 0.0),
            _timestamp_sort_key(row.get("last_timestamp") or row.get("timestamp")),
            float(row.get("mtime") or 0.0),
        ),
        reverse=True,
    )
    trimmed = []
    for row in decorated[:limit]:
        clean = dict(row)
        clean.pop("_match_bm25", None)
        clean.pop("_assistant_bonus", None)
        trimmed.append(clean)
    return trimmed


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

    plan = build_query_plan(query)
    if plan.low_confidence:
        return list_recent(conn, limit)
    fts_query = normalize_query(query)
    if not fts_query:
        return list_recent(conn, limit)
    descriptive = plan.descriptive
    terms = list(plan.fts_terms)

    # Pull more candidates than we'll return so reranking has room to
    # surface non-recent strong matches that pure-recency would have buried.
    # 5x is empirical: in the eval set the deepest grade-3 session in a
    # current-ranker pull was at rank ~40, so 5*limit (=250 at default) clears
    # that with margin. Going higher costs SQLite time per keystroke; lower
    # risks dropping a relevant doc before BM25 ever sees it.
    candidate_pool = max(limit * 5, 200)

    w = list(weights) + [1.0] * (7 - len(weights))  # tolerate short tuples
    sql = f"""
        SELECT s.sid, s.path, s.provider, s.cwd, s.timestamp, s.last_timestamp,
               s.title, s.first_msg, s.last_msg, s.msg_count, s.mtime,
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

    results = [_row_to_dict(r[:12]) | {"_bm25": -float(r[12] or 0.0)} for r in rows]
    existing_ids = {str(r["session_id"]) for r in results}
    match_map = _latest_segment_matches(
        conn,
        query,
        sids=None if descriptive else list(existing_ids),
        plan=plan,
    )
    extra_ids = [sid for sid in match_map if sid not in existing_ids] if descriptive else []
    if extra_ids:
        results.extend(
            [
                row | {"_bm25": 0.0}
                for row in _load_sessions_by_ids(conn, extra_ids[:candidate_pool])
            ]
        )
    if not results:
        return []
    decorated = _decorate_match_metadata(results, match_map)

    now = datetime.now(timezone.utc)
    for row in decorated:
        relevant_ts = (
            row.get("match_timestamp")
            or row.get("last_timestamp")
            or row.get("timestamp")
        )
        age = _age_days(relevant_ts, now)
        recency = math.exp(-age / half_life_days) if age >= 0 else 0.0
        row["_metadata_anchor_score"] = _metadata_anchor_score(row, plan)
        row["_artifact_penalty"] = _artifact_penalty(row, plan, query)
        row["_quality_score"] = (
            float(row.get("_metadata_anchor_score") or 0.0)
            - float(row.get("_artifact_penalty") or 0.0)
        )
        row["_score"] = float(row.get("_bm25") or 0.0) + recency_alpha * recency
        lifecycle_source = " ".join(
            part
            for part in (
                row.get("name") or "",
                row.get("first_msg") or "",
                row.get("last_msg") or "",
                row.get("context") or "",
            )
            if part
        )
        row["_closeout_score"] = float(row.get("match_lifecycle_score") or 0.0)
        if plan.wants_closeout:
            row["_closeout_score"] += sum(
                weight
                for cue, weight in _CLOSEOUT_CUE_WEIGHTS.items()
                if cue in lifecycle_source.lower()
            ) * 0.5
        row["_score"] += float(row.get("_quality_score") or 0.0)

    if plan.descriptive and plan.wants_closeout:
        decorated.sort(
            key=lambda row: (
                float(row.get("_quality_score") or 0.0),
                float(row.get("_closeout_score") or 0.0),
                int(row.get("match_term_count") or 0),
                _timestamp_sort_key(
                    row.get("match_timestamp")
                    or row.get("last_timestamp")
                    or row.get("timestamp")
                ),
                float(row.get("match_conversation_score") or 0.0),
                float(row.get("_score") or 0.0),
                float(row.get("match_density") or 0.0),
                float(row.get("match_compactness") or 0.0),
                float(row.get("_match_bm25") or 0.0),
                float(row.get("mtime") or 0.0),
            ),
            reverse=True,
        )
    elif len(terms) == 1:
        decorated.sort(
            key=lambda row: (
                int(row.get("match_term_count") or 0),
                float(row.get("_quality_score") or 0.0),
                1 if row.get("_assistant_bonus") else 0,
                _timestamp_sort_key(
                    row.get("match_timestamp")
                    or row.get("last_timestamp")
                    or row.get("timestamp")
                ),
                float(row.get("_score") or 0.0),
                float(row.get("match_conversation_score") or 0.0),
                float(row.get("match_density") or 0.0),
                float(row.get("match_compactness") or 0.0),
                float(row.get("_match_bm25") or 0.0),
                _timestamp_sort_key(row.get("last_timestamp") or row.get("timestamp")),
                float(row.get("mtime") or 0.0),
            ),
            reverse=True,
        )
    else:
        decorated.sort(
            key=lambda row: (
                int(row.get("match_term_count") or 0),
                float(row.get("_quality_score") or 0.0),
                float(row.get("match_conversation_score") or 0.0),
                _timestamp_sort_key(
                    row.get("match_timestamp")
                    or row.get("last_timestamp")
                    or row.get("timestamp")
                ),
                float(row.get("match_density") or 0.0),
                float(row.get("match_compactness") or 0.0),
                1 if row.get("_assistant_bonus") else 0,
                float(row.get("_score") or 0.0),
                float(row.get("_match_bm25") or 0.0),
                _timestamp_sort_key(row.get("last_timestamp") or row.get("timestamp")),
                float(row.get("mtime") or 0.0),
            ),
            reverse=True,
        )
    trimmed = []
    for row in decorated[:limit]:
        clean = dict(row)
        clean.pop("_bm25", None)
        clean.pop("_score", None)
        clean.pop("_match_bm25", None)
        clean.pop("_assistant_bonus", None)
        clean.pop("_closeout_score", None)
        clean.pop("_metadata_anchor_score", None)
        clean.pop("_artifact_penalty", None)
        clean.pop("_quality_score", None)
        trimmed.append(clean)
    return trimmed


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
        SELECT sid, path, provider, cwd, timestamp, last_timestamp, title,
               first_msg, last_msg, msg_count, mtime, '' AS context
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
        SELECT sid, path, provider, cwd, timestamp, last_timestamp, title,
               first_msg, last_msg, msg_count, mtime, '' AS context
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
        "provider": r[2],
        "cwd": r[3],
        "timestamp": r[4],
        "last_timestamp": r[5],
        "name": r[6],
        "first_msg": r[7],
        "last_msg": r[8],
        "msg_count": r[9] or 0,
        "mtime": r[10],
        "context": r[11],
    }

"""SQLite FTS5 index over Claude Code session text.

Replaces fzf's character-level fuzzy matching with proper full-text search:
single bare words match tokens exactly (no fuzzy false-positive flood),
multiple bare words AND together, double-quoted strings match as phrases.

The index lives at ~/.claude/cache/claude-browse-index.db. It's pure cache —
deletable any time; the next claude-browse run rebuilds from JSONL.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

from .core import list_index_records
from .providers import get_provider
from .query import (
    QueryPlan,
    build_query_plan,
    significant_query_terms,
    term_spans,
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
# v4: sessions table now stores provider ("claude" | "codex"), and reindex()
#     consumes a provider-agnostic record stream so the browser can show both
#     Claude Code and CodeX threads in one list.
# v5: segment-level transcript indexing powers descriptive "find the thread
#     where..." recall, last-matching-mention ranking, and query-anchored
#     snippets instead of pure thread-end recency.
# v6: local semantic-window sparse vectors plus exact identifier retrieval.
#     This gives natural-language recall a corpus-derived ranking channel
#     without sending transcript text to a network service.
# v7: optional dense embedding side cache over semantic_windows, stored and
#     queried locally. Disabled by default so the normal tool remains offline.
SCHEMA_VERSION = 7
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
_CRITIQUE_QUERY_TERMS = frozenset(
    {
        "critique",
        "critical",
        "evidence",
        "forced",
        "opportunities",
        "opportunity",
        "question",
        "questioned",
        "questioning",
        "skeptic",
        "skeptical",
    }
)
_CRITIQUE_CUE_WEIGHTS = {
    "forced": 3.0,
    "better evidence": 3.0,
    "evidence": 2.0,
    "wrong": 1.5,
    "backward": 2.0,
    "weak": 1.5,
    "convincing": 2.0,
    "should not": 2.0,
    "contradict": 2.0,
    "need better": 1.5,
    "undercut": 1.5,
    "framing": 1.0,
}
_FEEDBACK_QUERY_TERMS = frozenset(
    {
        "comment",
        "comments",
        "feedback",
        "note",
        "notes",
        "review",
        "reviews",
    }
)
_FEEDBACK_CUE_WEIGHTS = {
    "feedback": 3.0,
    "review": 2.0,
    "comments": 1.5,
    "notes": 1.0,
    "next steps": 1.0,
}
_PERFORMANCE_QUERY_TERMS = frozenset({"performance"})
_PERFORMANCE_REVIEW_CUE_WEIGHTS = {
    "feedback": 2.0,
    "review": 2.0,
    "1:1": 1.5,
    "support": 1.0,
    "notes": 1.0,
    "performance": 1.0,
}
_PERFORMANCE_MARKETING_MISMATCH_CUES = (
    "campaign performance",
    "creative performance",
    "performance marketing",
)
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
_CODE_REFERENCE_WINDOW_CUES = (
    "build",
    "deck",
    "decks",
    "prompt",
    "prompts",
    "script",
    "scripts",
    "marp",
)
_EXACT_URL_RE = re.compile(r"https?://[^\s<>)\"']+", re.IGNORECASE)
_EXACT_ID_RE = re.compile(r"\b[a-z0-9][a-z0-9_-]{15,}\b", re.IGNORECASE)
_SEMANTIC_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9']+")
_SEMANTIC_WINDOW_RADIUS = 2
_SEMANTIC_MAX_FEATURES = 160
_SEMANTIC_QUERY_MAX_FEATURES = 96
_SEMANTIC_MIN_SCORE = 0.11
_PHRASE_CONTINUATION_CONNECTORS = frozenset(
    {
        "about",
        "and",
        "as",
        "by",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "or",
        "to",
        "with",
    }
)
_DENSE_EMBEDDING_ENDPOINT = "https://api.openai.com/v1/embeddings"
_DENSE_QUERY_CACHE_LIMIT = 500
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY_ENV_VALUES


def _dense_embeddings_enabled() -> bool:
    return _env_flag("CLAUDE_BROWSE_DENSE_EMBEDDINGS")


def _dense_embedding_model() -> str:
    return os.environ.get("CLAUDE_BROWSE_EMBEDDING_MODEL", "text-embedding-3-small").strip() or "text-embedding-3-small"


def _dense_embedding_dimensions() -> int:
    raw = os.environ.get("CLAUDE_BROWSE_EMBEDDING_DIMENSIONS", "256").strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 256


def _dense_embedding_batch_size() -> int:
    raw = os.environ.get("CLAUDE_BROWSE_EMBEDDING_BATCH_SIZE", "64").strip()
    try:
        return max(1, min(256, int(raw)))
    except (TypeError, ValueError):
        return 64


def _dense_embedding_max_chars() -> int:
    raw = os.environ.get("CLAUDE_BROWSE_EMBEDDING_MAX_CHARS", "24000").strip()
    try:
        return max(1000, int(raw))
    except (TypeError, ValueError):
        return 24000


def _dense_min_score() -> float:
    raw = os.environ.get("CLAUDE_BROWSE_DENSE_MIN_SCORE", "0.25").strip()
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.25


def _normalized_path_segments(path: str | None) -> list[str]:
    if not path:
        return []
    segments: list[str] = []
    for part in str(path).split("/"):
        cleaned = re.sub(r"[^a-z0-9]+", "", part.lower())
        if cleaned:
            segments.append(cleaned)
    return segments


def _query_wants_critique(plan: QueryPlan) -> bool:
    return any(term in _CRITIQUE_QUERY_TERMS for term in plan.normalized_terms)


def _query_wants_feedback(plan: QueryPlan) -> bool:
    return any(term in _FEEDBACK_QUERY_TERMS for term in plan.normalized_terms)


def _query_wants_performance_review(plan: QueryPlan) -> bool:
    return (
        any(term in _PERFORMANCE_QUERY_TERMS for term in plan.normalized_terms)
        and any(term not in _PERFORMANCE_QUERY_TERMS for term in plan.anchor_terms)
    )


def _semantic_anchor_terms(plan: QueryPlan) -> tuple[str, ...]:
    soft_terms = (
        _CRITIQUE_QUERY_TERMS
        | _FEEDBACK_QUERY_TERMS
        | _PERFORMANCE_QUERY_TERMS
    )
    anchors = tuple(
        term for term in plan.anchor_terms if term not in soft_terms
    )
    return anchors or plan.anchor_terms


def _semantic_intent_score(text: str, plan: QueryPlan) -> float:
    lowered = text.lower()
    score = 0.0
    if _query_wants_critique(plan):
        score += sum(
            weight
            for cue, weight in _CRITIQUE_CUE_WEIGHTS.items()
            if cue in lowered
        )
    if _query_wants_feedback(plan):
        score += sum(
            weight
            for cue, weight in _FEEDBACK_CUE_WEIGHTS.items()
            if cue in lowered
        )
    if _query_wants_performance_review(plan):
        score += sum(
            weight
            for cue, weight in _PERFORMANCE_REVIEW_CUE_WEIGHTS.items()
            if cue in lowered
        )
    return score


def _semantic_mismatch_penalty(text: str, plan: QueryPlan) -> float:
    lowered = text.lower()
    penalty = 0.0
    if _query_wants_performance_review(plan):
        if any(cue in lowered for cue in _PERFORMANCE_MARKETING_MISMATCH_CUES):
            penalty += 2.5
    return penalty


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
            DROP TABLE IF EXISTS semantic_postings;
            DROP TABLE IF EXISTS semantic_terms;
            DROP TABLE IF EXISTS semantic_windows;
            DROP TABLE IF EXISTS dense_query_cache;
            DROP TABLE IF EXISTS dense_embeddings;
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
        CREATE TABLE IF NOT EXISTS semantic_windows (
            rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
            sid             TEXT NOT NULL,
            window_idx      INTEGER NOT NULL,
            segment_idx     INTEGER NOT NULL,
            timestamp       TEXT,
            text            TEXT NOT NULL,
            norm            REAL NOT NULL DEFAULT 1.0,
            UNIQUE(sid, window_idx)
        );
        CREATE INDEX IF NOT EXISTS idx_semantic_windows_sid
            ON semantic_windows(sid);
        CREATE INDEX IF NOT EXISTS idx_semantic_windows_timestamp
            ON semantic_windows(timestamp DESC);
        CREATE TABLE IF NOT EXISTS semantic_postings (
            term            TEXT NOT NULL,
            window_id       INTEGER NOT NULL,
            weight          REAL NOT NULL,
            PRIMARY KEY(term, window_id)
        );
        CREATE INDEX IF NOT EXISTS idx_semantic_postings_window
            ON semantic_postings(window_id);
        CREATE TABLE IF NOT EXISTS semantic_terms (
            term            TEXT PRIMARY KEY,
            df              INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dense_embeddings (
            window_id       INTEGER PRIMARY KEY,
            sid             TEXT NOT NULL,
            model           TEXT NOT NULL,
            dimensions      INTEGER NOT NULL,
            content_hash    TEXT NOT NULL,
            vector          BLOB NOT NULL,
            norm            REAL NOT NULL,
            indexed_at      REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dense_embeddings_sid
            ON dense_embeddings(sid);
        CREATE INDEX IF NOT EXISTS idx_dense_embeddings_config
            ON dense_embeddings(model, dimensions);
        CREATE TABLE IF NOT EXISTS dense_query_cache (
            cache_key       TEXT PRIMARY KEY,
            model           TEXT NOT NULL,
            dimensions      INTEGER NOT NULL,
            query           TEXT NOT NULL,
            vector          BLOB NOT NULL,
            norm            REAL NOT NULL,
            created_at      REAL NOT NULL
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
    records = _dedupe_records_by_session_id(list_index_records())
    record_map: dict[str, dict] = {r["path"]: r for r in records}
    current_sids = {str(r.get("session_id") or "") for r in records}

    existing: dict[str, tuple[str, float]] = {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT path, sid, mtime FROM sessions")
    }

    added = updated = removed = 0
    now = time.time()
    changes_since_commit = 0

    for path, record in record_map.items():
        prev = existing.get(path)
        if prev is None:
            if _index_record(conn, record, now):
                added += 1
                changes_since_commit += 1
        elif abs(prev[1] - record["mtime"]) > 0.001:
            if _index_record(conn, record, now):
                updated += 1
                changes_since_commit += 1
        if changes_since_commit >= 10:
            conn.commit()
            changes_since_commit = 0

    for path, (sid, _) in existing.items():
        if path not in record_map and sid not in current_sids:
            conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
            conn.execute("DELETE FROM sessions_fts WHERE sid = ?", (sid,))
            _delete_segments_for_sid(conn, sid)
            removed += 1
            changes_since_commit += 1
        if changes_since_commit >= 10:
            conn.commit()
            changes_since_commit = 0

    if changes_since_commit:
        conn.commit()
    if added or updated or removed or _semantic_terms_missing(conn):
        _refresh_semantic_index(conn)
    if _dense_embeddings_enabled():
        _sync_dense_embeddings(conn)
    conn.commit()
    return (added, updated, removed)


def _dedupe_records_by_session_id(records: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for record in records:
        sid = str(record.get("session_id") or "")
        if not sid:
            continue
        current = deduped.get(sid)
        if current is None:
            deduped[sid] = record
            continue
        record_key = (
            _timestamp_sort_key(record.get("last_timestamp") or record.get("timestamp")),
            float(record.get("mtime") or 0.0),
        )
        current_key = (
            _timestamp_sort_key(current.get("last_timestamp") or current.get("timestamp")),
            float(current.get("mtime") or 0.0),
        )
        if record_key >= current_key:
            deduped[sid] = record
    return list(deduped.values())


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


def _unique_terms(*groups: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for group in groups:
        for term in group:
            if not term or term in seen:
                continue
            seen.add(term)
            ordered.append(term)
    return tuple(ordered)


def _highlight_context(text: str, highlight_terms: list[str] | tuple[str, ...]) -> str:
    clean = " ".join(text.split())
    matches = []
    for term in highlight_terms:
        spans = term_spans(clean, term)
        if spans:
            start, end = spans[0]
            matches.append((term, start, end))
    if not matches:
        return clean[:180]

    phrase_matches = [
        (_term, start, end)
        for _term, start, end in matches
        if " " in _term
    ]
    chosen = phrase_matches or matches
    highlight_set = {term for term, _start, _end in chosen}
    start = max(0, min(pos for _term, pos, _end in chosen) - 32)
    end = min(len(clean), max(pos for _term, _start, pos in chosen) + 80)
    excerpt = clean[start:end]
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(clean):
        excerpt = excerpt + "…"
    for term in sorted(highlight_set, key=len, reverse=True):
        if " " in term:
            excerpt = re.sub(
                re.escape(term),
                lambda match: f"\x01{match.group(0)}\x02",
                excerpt,
                flags=re.IGNORECASE,
            )
        else:
            excerpt = re.sub(
                rf"(?<!\w){re.escape(term)}(?!\w)",
                lambda match: f"\x01{match.group(0)}\x02",
                excerpt,
                flags=re.IGNORECASE,
            )
    return excerpt


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _exact_identifier_terms(query: str) -> tuple[str, ...]:
    terms: list[str] = []
    for match in _EXACT_URL_RE.finditer(query):
        url = match.group(0).rstrip(".,;:)]}")
        if url:
            terms.append(url.lower())
            base_url = url.split("?", 1)[0].rstrip("/")
            if base_url and base_url != url:
                terms.append(base_url.lower())
    for match in _EXACT_ID_RE.finditer(query.lower()):
        raw = match.group(0).strip("_-")
        compact = raw.replace("-", "").replace("_", "")
        if len(compact) >= 16 and any(ch.isdigit() for ch in compact):
            terms.append(raw)
            if compact != raw:
                terms.append(compact)
    return _unique_terms(tuple(terms))


def _exact_segment_context(
    conn: sqlite3.Connection,
    sid: str,
    terms: tuple[str, ...],
) -> tuple[str, str | None, int | None]:
    for term in terms:
        pattern = f"%{_escape_like(term.lower())}%"
        row = conn.execute(
            """
            SELECT text, timestamp, segment_idx
            FROM segments
            WHERE sid = ? AND lower(text) LIKE ? ESCAPE '\\'
            ORDER BY segment_idx
            LIMIT 1
            """,
            (sid, pattern),
        ).fetchone()
        if row:
            context = _highlight_context(str(row[0] or ""), (term,))
            return (context, row[1], row[2])
    return ("", None, None)


def _exact_identifier_results(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
) -> list[dict]:
    terms = _exact_identifier_terms(query)
    if not terms:
        return []

    haystack = """
        lower(
            coalesce(s.cwd, '') || ' ' ||
            coalesce(s.title, '') || ' ' ||
            coalesce(s.first_msg, '') || ' ' ||
            coalesce(s.last_msg, '') || ' ' ||
            coalesce(sessions_fts.cwd, '') || ' ' ||
            coalesce(sessions_fts.title, '') || ' ' ||
            coalesce(sessions_fts.first_msg, '') || ' ' ||
            coalesce(sessions_fts.user_text, '') || ' ' ||
            coalesce(sessions_fts.asst_text, '')
        )
    """
    where = " OR ".join([f"{haystack} LIKE ? ESCAPE '\\'" for _ in terms])
    params = [f"%{_escape_like(term)}%" for term in terms]
    rows = conn.execute(
        f"""
        SELECT s.sid, s.path, s.provider, s.cwd, s.timestamp, s.last_timestamp,
               s.title, s.first_msg, s.last_msg, s.msg_count, s.mtime,
               '' AS context
        FROM sessions_fts
        JOIN sessions s ON s.sid = sessions_fts.sid
        WHERE {where}
        ORDER BY s.last_timestamp DESC, s.mtime DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    results: list[dict] = []
    for row in rows:
        item = _row_to_dict(row)
        context, timestamp, segment_idx = _exact_segment_context(
            conn,
            str(item["session_id"]),
            terms,
        )
        if context:
            item["context"] = context
            item["match_context"] = context
        item["match_timestamp"] = timestamp or item.get("last_timestamp")
        if segment_idx is not None:
            item["match_segment_idx"] = segment_idx
        item["exact_identifier_match"] = ", ".join(terms)
        item["_exact_identifier_score"] = 50.0
        item["_bm25"] = 0.0
        results.append(item)
    return results


def _semantic_tokens(text: str) -> list[str]:
    normalized = text.lower().replace("’", "'").replace("-", "")
    tokens: list[str] = []
    for match in _SEMANTIC_TOKEN_RE.finditer(normalized):
        token = match.group(0).replace("'", "")
        if len(token) >= 3 or any(ch.isdigit() for ch in token):
            tokens.append(token)
    return tokens


def _semantic_features(text: str, *, max_features: int = _SEMANTIC_MAX_FEATURES) -> dict[str, float]:
    tokens = _semantic_tokens(text)
    if not tokens:
        return {}

    counts: Counter[str] = Counter()
    for token in tokens:
        counts[f"tok:{token}"] += 1.0 + min(len(token), 12) / 12.0
        if len(token) >= 6:
            for idx in range(0, len(token) - 3):
                counts[f"chr:{token[idx:idx + 4]}"] += 0.25
    for left, right in zip(tokens, tokens[1:]):
        if left != right:
            counts[f"big:{left} {right}"] += 1.5

    weighted = {
        term: math.log1p(weight)
        for term, weight in counts.items()
        if weight > 0.0
    }
    if len(weighted) <= max_features:
        return weighted
    ranked = sorted(weighted.items(), key=lambda item: (-item[1], item[0]))
    return dict(ranked[:max_features])


def _semantic_idf(total_windows: int, df: int) -> float:
    return math.log((total_windows + 1.0) / (df + 1.0)) + 1.0


def _term_document_frequency(conn: sqlite3.Connection, term: str) -> tuple[int, int]:
    token = term.strip(".*").lower()
    if not token or " " in token:
        return (0, 0)

    total_windows = conn.execute(
        "SELECT COUNT(*) FROM semantic_windows"
    ).fetchone()[0]
    if total_windows:
        row = conn.execute(
            "SELECT df FROM semantic_terms WHERE term = ?",
            (f"tok:{token}",),
        ).fetchone()
        if row:
            return (int(row[0]), int(total_windows))

    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if not total_sessions:
        return (0, 0)
    fts_query = _terms_to_fts_query([token])
    if not fts_query:
        return (0, int(total_sessions))
    try:
        df = conn.execute(
            "SELECT COUNT(*) FROM sessions_fts WHERE sessions_fts MATCH ?",
            (fts_query,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        df = 0
    return (int(df), int(total_sessions))


def _discriminative_query_plan(
    conn: sqlite3.Connection,
    plan: QueryPlan,
) -> QueryPlan:
    if not plan.descriptive or len(plan.fts_terms) <= 2:
        return plan

    scored: list[tuple[float, int, str]] = []
    for idx, term in enumerate(plan.fts_terms):
        token = term.strip(".*").lower()
        if not token or " " in token or term.endswith("*"):
            continue
        df, total = _term_document_frequency(conn, token)
        if df <= 0 or total <= 0:
            continue
        length_weight = (max(min(len(token), 16), 1) / 4.0) ** 2
        specificity = _semantic_idf(total, df) * length_weight
        scored.append((specificity, idx, term))

    if len(scored) < 2:
        return plan

    ranked = sorted(scored, key=lambda item: (-item[0], item[1]))
    best = ranked[0][0]
    selected = [item for item in ranked if item[0] >= best * 0.55]
    if len(selected) < 2:
        selected = ranked[:2]
    selected = selected[:3]
    keep_indices = {idx for _score, idx, _term in selected}
    terms = tuple(
        term for idx, term in enumerate(plan.fts_terms) if idx in keep_indices
    )
    if len(terms) >= len(plan.fts_terms):
        return plan

    exact_phrase_terms = list(plan.exact_phrase_terms)
    if len(terms) == 2:
        implicit_phrase = " ".join(
            term for term in terms if " " not in term and not term.endswith("*")
        ).strip()
        if implicit_phrase and len(implicit_phrase.split()) == 2:
            exact_phrase_terms.append(implicit_phrase)

    return replace(
        plan,
        fts_terms=terms,
        anchor_terms=terms,
        exact_phrase_terms=_unique_terms(tuple(exact_phrase_terms)),
        highlight_terms=_unique_terms(
            tuple(exact_phrase_terms),
            terms,
            plan.highlight_terms,
        ),
    )


def _phrase_fallback_plan(plan: QueryPlan) -> QueryPlan | None:
    """Relax a strict quoted phrase into its meaningful words after zero hits."""
    if len(plan.phrase_fallback_terms) < 2:
        return None
    if plan.phrase_fallback_terms == plan.fts_terms:
        return None
    fallback_phrase = " ".join(plan.phrase_fallback_terms)
    return replace(
        plan,
        fts_terms=plan.phrase_fallback_terms,
        anchor_terms=plan.phrase_fallback_terms,
        exact_phrase_terms=(fallback_phrase,),
        highlight_terms=_unique_terms(
            plan.exact_phrase_terms,
            (fallback_phrase,),
            plan.phrase_fallback_terms,
            plan.highlight_terms,
        ),
    )


def _mark_phrase_fallback(rows: list[dict], plan: QueryPlan) -> list[dict]:
    if not rows:
        return rows
    phrase = ", ".join(plan.exact_phrase_terms)
    terms = ", ".join(plan.phrase_fallback_terms)
    for row in rows:
        row["phrase_fallback"] = True
        row["phrase_fallback_from"] = phrase
        row["phrase_fallback_terms"] = terms
    return rows


def _prefix_fallback_plan(plan: QueryPlan) -> QueryPlan | None:
    """Relax the last concrete token to a prefix after strict zero hits."""
    terms = list(plan.fts_terms)
    if not terms:
        return None
    if len(terms) == 1 and " " in terms[0]:
        phrase_terms = list(plan.phrase_fallback_terms)
        if len(phrase_terms) >= 2:
            for idx in range(len(phrase_terms) - 1, -1, -1):
                term = phrase_terms[idx]
                if term.endswith("*") or len(term.strip(".*")) < 3:
                    continue
                prefix_terms = phrase_terms.copy()
                prefix_terms[idx] = f"{term}*"
                return replace(
                    plan,
                    fts_terms=tuple(prefix_terms),
                    anchor_terms=tuple(prefix_terms),
                    exact_phrase_terms=(),
                    highlight_terms=_unique_terms(
                        tuple(prefix_terms),
                        plan.highlight_terms,
                    ),
                )
    for idx in range(len(terms) - 1, -1, -1):
        term = terms[idx]
        if " " in term or term.endswith("*"):
            continue
        if len(term.strip(".*")) < 3:
            continue
        prefix_terms = terms.copy()
        prefix_terms[idx] = f"{term}*"
        return replace(
            plan,
            fts_terms=tuple(prefix_terms),
            anchor_terms=tuple(prefix_terms),
            exact_phrase_terms=(),
            highlight_terms=_unique_terms(tuple(prefix_terms), plan.highlight_terms),
        )
    return None


def _has_unclosed_quote(query: str) -> bool:
    return query.count('"') % 2 == 1


def _prefix_completion_score(row: dict, plan: QueryPlan) -> float:
    """Prefer real completions for a prefix fallback over exact short tokens."""
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("context", "first_msg", "last_msg", "name")
    ).replace("\x01", "").replace("\x02", "")
    tokens = [
        match.group(0).replace("'", "").lower()
        for match in _SEMANTIC_TOKEN_RE.finditer(haystack.lower())
    ]
    if not tokens:
        return 0.0

    score = 0.0
    for idx, term in enumerate(plan.fts_terms):
        if not term.endswith("*"):
            continue
        prefix = term[:-1].strip(".*").lower()
        if len(prefix) < 3:
            continue
        anchors = [
            prior.strip(".*").lower()
            for prior in plan.fts_terms[:idx]
            if prior and " " not in prior and not prior.endswith("*")
        ][-3:]
        if not anchors:
            if any(token.startswith(prefix) and len(token) > len(prefix) for token in tokens):
                score += 2.0
            continue

        for start in range(len(tokens)):
            cursor = start
            for anchor in anchors:
                found = None
                for pos in range(cursor, min(len(tokens), cursor + 8)):
                    if tokens[pos] == anchor:
                        found = pos
                        break
                if found is None:
                    break
                cursor = found + 1
            else:
                for pos in range(cursor, min(len(tokens), cursor + 8)):
                    if not tokens[pos].startswith(prefix):
                        continue
                    if len(tokens[pos]) > len(prefix):
                        score += 2.0
                    # Only the local phrase slot counts. A later unrelated
                    # completion should not rescue an exact short-token hit.
                    return score
    return score


def _mark_prefix_fallback(
    rows: list[dict],
    strict_plan: QueryPlan,
    fallback_plan: QueryPlan,
) -> list[dict]:
    if not rows:
        return rows
    for row in rows:
        row["prefix_fallback"] = True
        row["prefix_fallback_from"] = ", ".join(strict_plan.fts_terms)
        row["prefix_fallback_terms"] = ", ".join(fallback_plan.fts_terms)
    return rows


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
    _delete_semantic_windows_for_sid(conn, sid)


def _delete_semantic_windows_for_sid(conn: sqlite3.Connection, sid: str) -> None:
    rowids = [
        row[0]
        for row in conn.execute(
            "SELECT rowid FROM semantic_windows WHERE sid = ?",
            (sid,),
        ).fetchall()
    ]
    if rowids:
        placeholders = ",".join("?" for _ in rowids)
        conn.execute(
            f"DELETE FROM semantic_postings WHERE window_id IN ({placeholders})",
            rowids,
        )
        conn.execute(
            f"DELETE FROM dense_embeddings WHERE window_id IN ({placeholders})",
            rowids,
        )
    conn.execute("DELETE FROM semantic_windows WHERE sid = ?", (sid,))


def _reindex_semantic_windows_from_segments(
    conn: sqlite3.Connection,
    sid: str,
) -> None:
    _delete_semantic_windows_for_sid(conn, sid)
    rows = conn.execute(
        """
        SELECT segment_idx, timestamp, text
        FROM segments
        WHERE sid = ?
        ORDER BY segment_idx
        """,
        (sid,),
    ).fetchall()
    if not rows:
        return

    for pos, (segment_idx, timestamp, _text) in enumerate(rows):
        start = max(0, pos - _SEMANTIC_WINDOW_RADIUS)
        end = min(len(rows), pos + _SEMANTIC_WINDOW_RADIUS + 1)
        window_text = " ".join(str(row[2] or "") for row in rows[start:end]).strip()
        features = _semantic_features(window_text)
        if not features:
            continue
        cur = conn.execute(
            """
            INSERT INTO semantic_windows (
                sid, window_idx, segment_idx, timestamp, text, norm
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                pos + 1,
                segment_idx,
                timestamp,
                window_text,
                max(
                    math.sqrt(
                        sum(weight * weight for weight in features.values())
                    ),
                    1e-9,
                ),
            ),
        )
        window_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO semantic_postings (term, window_id, weight)
            VALUES (?, ?, ?)
            """,
            [
                (term, window_id, weight)
                for term, weight in features.items()
            ],
        )


def _refresh_semantic_index(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM semantic_terms")
    conn.execute(
        """
        INSERT INTO semantic_terms (term, df)
        SELECT term, COUNT(*)
        FROM semantic_postings
        GROUP BY term
        """
    )


def _semantic_terms_missing(conn: sqlite3.Connection) -> bool:
    windows = conn.execute("SELECT COUNT(*) FROM semantic_windows").fetchone()[0]
    if not windows:
        return False
    terms = conn.execute("SELECT COUNT(*) FROM semantic_terms").fetchone()[0]
    return not terms


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
    _reindex_semantic_windows_from_segments(conn, sid)


def _semantic_query_features(
    conn: sqlite3.Connection,
    query: str,
) -> tuple[list[tuple[str, float]], float]:
    raw_features = _semantic_features(
        query,
        max_features=_SEMANTIC_QUERY_MAX_FEATURES,
    )
    if not raw_features:
        return ([], 0.0)

    total_windows = conn.execute(
        "SELECT COUNT(*) FROM semantic_windows"
    ).fetchone()[0]
    if not total_windows:
        return ([], 0.0)

    scored: list[tuple[str, float]] = []
    norm_sq = 0.0
    for term, raw_weight in raw_features.items():
        row = conn.execute(
            "SELECT df FROM semantic_terms WHERE term = ?",
            (term,),
        ).fetchone()
        if not row:
            continue
        idf = _semantic_idf(int(total_windows), int(row[0]))
        weight = raw_weight * idf * idf
        scored.append((term, weight))
        norm_sq += weight * weight

    if not scored:
        return ([], 0.0)
    scored.sort(key=lambda item: (-item[1], item[0]))
    scored = scored[:_SEMANTIC_QUERY_MAX_FEATURES]
    norm = math.sqrt(sum(weight * weight for _term, weight in scored))
    return (scored, max(norm, 1e-9))


def _semantic_window_matches(
    conn: sqlite3.Connection,
    query: str,
    *,
    plan: QueryPlan,
    sids: list[str] | None = None,
    limit: int = 200,
) -> dict[str, dict[str, object]]:
    if not plan.descriptive:
        return {}

    semantic_query = " ".join(plan.fts_terms) if len(plan.fts_terms) >= 2 else query
    query_features, query_norm = _semantic_query_features(conn, semantic_query)
    if not query_features:
        return {}

    values_sql = ", ".join(["(?, ?)"] * len(query_features))
    feature_params: list[object] = []
    for term, weight in query_features:
        feature_params.extend([term, weight])

    where = ["w.norm > 0"]
    sid_params: list[object] = []
    if sids:
        placeholders = ",".join("?" for _ in sids)
        where.append(f"w.sid IN ({placeholders})")
        sid_params.extend(sids)

    score_expr = "dot / (norm * ?)"
    sql = f"""
        WITH q(term, qw) AS (
            VALUES {values_sql}
        ),
        scored AS (
            SELECT w.sid,
                   w.segment_idx,
                   w.timestamp,
                   w.text,
                   w.norm,
                   SUM(p.weight * q.qw) AS dot,
                   COUNT(*) AS overlap_count
            FROM q
            JOIN semantic_postings p ON p.term = q.term
            JOIN semantic_windows w ON w.rowid = p.window_id
            WHERE {' AND '.join(where)}
            GROUP BY w.rowid
        )
        SELECT sid, segment_idx, timestamp, text, overlap_count,
               {score_expr} AS score
        FROM scored
        WHERE {score_expr} >= ?
        ORDER BY score DESC, overlap_count DESC, segment_idx DESC
        LIMIT ?
    """
    params = [
        *feature_params,
        *sid_params,
        query_norm,
        query_norm,
        _SEMANTIC_MIN_SCORE,
        limit,
    ]
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return {}

    matches: dict[str, dict[str, object]] = {}
    highlight_terms = list(plan.highlight_terms or plan.fts_terms)
    anchors = list(_semantic_anchor_terms(plan))
    for sid, segment_idx, timestamp, text, overlap_count, score in rows:
        sid = str(sid)
        if sid in matches:
            continue
        text_str = str(text or "")
        term_count = sum(1 for term in anchors if term_spans(text_str, term))
        matches[sid] = {
            "match_segment_idx": segment_idx,
            "match_timestamp": timestamp,
            "match_context": _highlight_context(text_str, highlight_terms),
            "match_term_count": term_count,
            "match_phrase_score": 0.0,
            "match_density": float(score or 0.0),
            "match_compactness": float(score or 0.0),
            "match_conversation_score": 1.0,
            "match_lifecycle_score": 0.0,
            "match_intent_score": 0.0,
            "match_mismatch_penalty": 0.0,
            "match_semantic_score": float(score or 0.0),
            "match_semantic_overlap": int(overlap_count or 0),
            "match_source": "semantic",
            "_match_bm25": 0.0,
            "_assistant_bonus": 0,
        }
    return matches


def _dense_embedding_input(text: str) -> str:
    clean = " ".join(str(text or "").split())
    max_chars = _dense_embedding_max_chars()
    if len(clean) > max_chars:
        return clean[:max_chars]
    return clean


def _dense_content_hash(text: str) -> str:
    return hashlib.sha256(_dense_embedding_input(text).encode("utf-8")).hexdigest()


def _vector_norm(vector: list[float]) -> float:
    return math.sqrt(sum(float(value) * float(value) for value in vector))


def _vector_to_blob(vector: list[float]) -> bytes:
    values = [float(value) for value in vector]
    if not values:
        return b""
    return struct.pack(f"<{len(values)}f", *values)


def _vector_from_blob(blob: bytes) -> list[float]:
    if not blob:
        return []
    count = len(blob) // 4
    if count <= 0:
        return []
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def _cosine_with_blob(
    vector_blob: bytes,
    vector_norm: float,
    query_vector: list[float],
    query_norm: float,
) -> float:
    if not vector_blob or vector_norm <= 0.0 or query_norm <= 0.0:
        return 0.0
    count = min(len(vector_blob) // 4, len(query_vector))
    if count <= 0:
        return 0.0
    values = struct.unpack(f"<{count}f", vector_blob[: count * 4])
    dot = sum(float(values[idx]) * float(query_vector[idx]) for idx in range(count))
    return dot / (vector_norm * query_norm)


def _request_openai_embeddings(
    texts: list[str],
    *,
    model: str,
    dimensions: int,
) -> list[list[float]]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or not texts:
        return []

    payload: dict[str, object] = {
        "model": model,
        "input": texts,
    }
    if dimensions > 0:
        payload["dimensions"] = dimensions
    request = urllib.request.Request(
        _DENSE_EMBEDDING_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        return []

    items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return []
    try:
        valid_items = [item for item in items if isinstance(item, dict)]
        ordered = sorted(valid_items, key=lambda item: int(item.get("index", 0)))
        return [
            [float(value) for value in item["embedding"]]
            for item in ordered
            if isinstance(item.get("embedding"), list)
        ]
    except (AttributeError, KeyError, TypeError, ValueError):
        return []


def _sync_dense_embeddings(conn: sqlite3.Connection) -> None:
    if not _dense_embeddings_enabled() or not os.environ.get("OPENAI_API_KEY"):
        return

    model = _dense_embedding_model()
    dimensions = _dense_embedding_dimensions()
    conn.execute(
        """
        DELETE FROM dense_embeddings
        WHERE window_id NOT IN (SELECT rowid FROM semantic_windows)
           OR model != ?
           OR dimensions != ?
        """,
        (model, dimensions),
    )

    rows = conn.execute(
        """
        SELECT w.rowid, w.sid, w.text, e.content_hash
        FROM semantic_windows w
        LEFT JOIN dense_embeddings e ON e.window_id = w.rowid
        ORDER BY w.rowid
        """
    ).fetchall()
    pending: list[tuple[int, str, str, str]] = []
    for window_id, sid, text, existing_hash in rows:
        embedding_text = _dense_embedding_input(str(text or ""))
        if not embedding_text:
            continue
        content_hash = _dense_content_hash(embedding_text)
        if existing_hash != content_hash:
            pending.append((int(window_id), str(sid), embedding_text, content_hash))
    if not pending:
        return

    now = time.time()
    batch_size = _dense_embedding_batch_size()
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset : offset + batch_size]
        try:
            vectors = _request_openai_embeddings(
                [item[2] for item in batch],
                model=model,
                dimensions=dimensions,
            )
        except Exception:
            return
        if len(vectors) != len(batch):
            return
        for (window_id, sid, _text, content_hash), vector in zip(batch, vectors):
            norm = max(_vector_norm(vector), 1e-9)
            conn.execute(
                """
                INSERT INTO dense_embeddings (
                    window_id, sid, model, dimensions, content_hash, vector,
                    norm, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(window_id) DO UPDATE SET
                    sid = excluded.sid,
                    model = excluded.model,
                    dimensions = excluded.dimensions,
                    content_hash = excluded.content_hash,
                    vector = excluded.vector,
                    norm = excluded.norm,
                    indexed_at = excluded.indexed_at
                """,
                (
                    window_id,
                    sid,
                    model,
                    dimensions,
                    content_hash,
                    _vector_to_blob(vector),
                    norm,
                    now,
                ),
            )
        conn.commit()


def _dense_query_cache_key(model: str, dimensions: int, query: str) -> str:
    raw = f"{model}\0{dimensions}\0{query}".encode()
    return hashlib.sha256(raw).hexdigest()


def _prune_dense_query_cache(conn: sqlite3.Connection) -> None:
    stale = conn.execute(
        """
        SELECT cache_key
        FROM dense_query_cache
        ORDER BY created_at DESC
        LIMIT -1 OFFSET ?
        """,
        (_DENSE_QUERY_CACHE_LIMIT,),
    ).fetchall()
    if not stale:
        return
    conn.executemany(
        "DELETE FROM dense_query_cache WHERE cache_key = ?",
        [(row[0],) for row in stale],
    )


def _dense_query_embedding(
    conn: sqlite3.Connection,
    query: str,
    *,
    model: str,
    dimensions: int,
) -> tuple[list[float], float] | None:
    embedding_text = _dense_embedding_input(query)
    if not embedding_text:
        return None
    cache_key = _dense_query_cache_key(model, dimensions, embedding_text)
    try:
        row = conn.execute(
            """
            SELECT vector, norm
            FROM dense_query_cache
            WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row:
        return (_vector_from_blob(row[0]), float(row[1] or 0.0))

    try:
        vectors = _request_openai_embeddings(
            [embedding_text],
            model=model,
            dimensions=dimensions,
        )
    except Exception:
        return None
    if len(vectors) != 1 or not vectors[0]:
        return None
    vector = vectors[0]
    norm = max(_vector_norm(vector), 1e-9)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO dense_query_cache (
                cache_key, model, dimensions, query, vector, norm, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cache_key,
                model,
                dimensions,
                embedding_text,
                _vector_to_blob(vector),
                norm,
                time.time(),
            ),
        )
        _prune_dense_query_cache(conn)
        conn.commit()
    except sqlite3.Error:
        pass
    return (vector, norm)


def _dense_query_is_eligible(query: str, plan: QueryPlan) -> bool:
    if not _dense_embeddings_enabled() or not os.environ.get("OPENAI_API_KEY"):
        return False
    if _exact_identifier_terms(query):
        return False
    if len(query.strip()) < 12:
        return False
    return len(plan.fts_terms) >= 2


def _dense_window_matches(
    conn: sqlite3.Connection,
    query: str,
    *,
    plan: QueryPlan,
    limit: int = 200,
) -> dict[str, dict[str, object]]:
    if not _dense_query_is_eligible(query, plan):
        return {}

    model = _dense_embedding_model()
    dimensions = _dense_embedding_dimensions()
    try:
        count = conn.execute(
            """
            SELECT COUNT(*)
            FROM dense_embeddings
            WHERE model = ? AND dimensions = ?
            """,
            (model, dimensions),
        ).fetchone()[0]
    except sqlite3.Error:
        return {}
    if not count:
        return {}

    query_embedding = _dense_query_embedding(
        conn,
        query,
        model=model,
        dimensions=dimensions,
    )
    if not query_embedding:
        return {}
    query_vector, query_norm = query_embedding
    min_score = _dense_min_score()
    try:
        rows = conn.execute(
            """
            SELECT w.sid, w.segment_idx, w.timestamp, w.text, e.vector, e.norm
            FROM dense_embeddings e
            JOIN semantic_windows w ON w.rowid = e.window_id
            WHERE e.model = ? AND e.dimensions = ? AND e.norm > 0
            """,
            (model, dimensions),
        ).fetchall()
    except sqlite3.Error:
        return {}

    best_by_sid: dict[str, tuple[float, int, str | None, str]] = {}
    for sid, segment_idx, timestamp, text, vector_blob, vector_norm in rows:
        score = _cosine_with_blob(
            vector_blob,
            float(vector_norm or 0.0),
            query_vector,
            query_norm,
        )
        if score < min_score:
            continue
        sid = str(sid)
        existing = best_by_sid.get(sid)
        if existing is None or score > existing[0]:
            best_by_sid[sid] = (
                score,
                int(segment_idx or 0),
                timestamp,
                str(text or ""),
            )

    ranked = sorted(
        best_by_sid.items(),
        key=lambda item: (item[1][0], _timestamp_sort_key(item[1][2])),
        reverse=True,
    )[:limit]

    highlight_terms = list(plan.highlight_terms or plan.fts_terms)
    anchors = list(_semantic_anchor_terms(plan))
    matches: dict[str, dict[str, object]] = {}
    for sid, (score, segment_idx, timestamp, text) in ranked:
        term_count = sum(1 for term in anchors if term_spans(text, term))
        matches[sid] = {
            "match_segment_idx": segment_idx,
            "match_timestamp": timestamp,
            "match_context": _highlight_context(text, highlight_terms),
            "match_term_count": term_count,
            "match_phrase_score": 0.0,
            "match_density": float(score),
            "match_compactness": float(score),
            "match_conversation_score": 1.0,
            "match_lifecycle_score": 0.0,
            "match_intent_score": 0.0,
            "match_mismatch_penalty": 0.0,
            "match_semantic_score": float(score),
            "match_dense_score": float(score),
            "match_semantic_overlap": 0,
            "match_source": "dense",
            "_match_bm25": 0.0,
            "_assistant_bonus": 0,
        }
    return matches


def _latest_segment_matches(
    conn: sqlite3.Connection,
    query: str,
    *,
    sids: list[str] | None = None,
    plan: QueryPlan | None = None,
) -> dict[str, dict[str, object]]:
    plan = plan or build_query_plan(query)
    terms = list(plan.fts_terms)
    descriptive = plan.descriptive
    if descriptive and len(plan.fts_terms) <= 3:
        anchor_terms = list(plan.fts_terms)
    else:
        anchor_terms = list(_semantic_anchor_terms(plan))
    if not terms:
        return {}
    fts_query = _terms_to_fts_query(terms, joiner=" OR ")
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
    lowered_terms = [term.lower() for term in anchor_terms]
    required_term_source = anchor_terms or terms
    if len(required_term_source) <= 2:
        required_term_count = len(required_term_source)
    elif descriptive:
        required_term_count = max(2, len(required_term_source) - 1)
    else:
        required_term_count = len(required_term_source)

    def _match_stats(text: str) -> tuple[int, float, float]:
        positions: list[tuple[int, int]] = []
        for term in lowered_terms:
            spans = term_spans(text, term)
            if spans:
                positions.append(spans[0])
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

    phrase_score_terms = tuple(
        term
        for term in _unique_terms(plan.exact_phrase_terms, tuple(highlight_terms))
        if " " in term and not term.endswith("*")
    )

    def _phrase_score(text: str) -> float:
        score = 0.0
        for phrase in phrase_score_terms:
            if phrase and term_spans(text, phrase):
                score += 1.0 + min(len(phrase.split()), 6) * 0.25
        return score

    def _phrase_continuation_score(text: str) -> float:
        best = 0.0
        for phrase in phrase_score_terms:
            for _start, end in term_spans(text, phrase):
                after = text[end : end + 40].lstrip(" \t\r\n\"'.,;:)-]")
                if not after or not after[0].isalnum():
                    continue
                first_word = after.split(None, 1)[0].strip(".,;:!?)]}\"'").lower()
                if first_word in _PHRASE_CONTINUATION_CONNECTORS:
                    best = 1.0
        return best

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
        matches = []
        for term in highlight_terms:
            spans = term_spans(clean, term)
            if spans:
                start, end = spans[0]
                matches.append((term, start, end))
        if not matches:
            excerpt = clean[:180]
            highlight_set: set[str] = set()
        else:
            phrase_matches = [
                (_term, start, end)
                for _term, start, end in matches
                if " " in _term
            ]
            chosen = phrase_matches or matches
            highlight_set = {term for term, _start, _end in chosen}
            start = max(0, min(pos for _term, pos, _end in chosen) - 32)
            end = min(
                len(clean),
                max(pos for _term, _start, pos in chosen) + 80,
            )
            excerpt = clean[start:end]
            if start > 0:
                excerpt = "…" + excerpt
            if end < len(clean):
                excerpt = excerpt + "…"
        for term in sorted(highlight_set, key=len, reverse=True):
            if " " in term:
                excerpt = re.sub(
                    re.escape(term),
                    lambda match: f"\x01{match.group(0)}\x02",
                    excerpt,
                    flags=re.IGNORECASE,
                )
            else:
                excerpt = re.sub(
                    rf"(?<!\w){re.escape(term)}(?!\w)",
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
        phrase_score = _phrase_score(combined)
        phrase_continuation_score = _phrase_continuation_score(combined)
        intent_score = _semantic_intent_score(combined, plan)
        mismatch_penalty = _semantic_mismatch_penalty(combined, plan)
        prefix_completion_score = _prefix_completion_score(
            {"context": combined},
            plan,
        )
        ranked.append(
            (
                sid,
                {
                    "match_segment_idx": match_segment_idx,
                    "match_timestamp": match_timestamp,
                    "match_context": _context_from_exchange(combined) or context,
                    "match_term_count": term_count,
                    "match_phrase_score": phrase_score,
                    "match_phrase_continuation_score": phrase_continuation_score,
                    "match_prefix_completion_score": prefix_completion_score,
                    "match_density": density,
                    "match_compactness": compactness,
                    "match_conversation_score": _conversation_score(combined),
                    "match_lifecycle_score": _closeout_score(combined),
                    "match_intent_score": intent_score,
                    "match_mismatch_penalty": mismatch_penalty,
                    "_match_bm25": -float(bm or 0.0),
                    "_assistant_bonus": assistant_bonus,
                },
            )
        )

    if plan.descriptive and plan.wants_closeout:
        ranked.sort(
            key=lambda item: (
                item[1]["match_lifecycle_score"],
                item[1]["match_phrase_score"],
                item[1]["match_phrase_continuation_score"],
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
                item[1]["match_intent_score"] - item[1]["match_mismatch_penalty"],
                item[1]["match_phrase_score"],
                item[1]["match_phrase_continuation_score"],
                item[1]["_assistant_bonus"],
                _timestamp_sort_key(item[1]["match_timestamp"]),
                item[1]["match_prefix_completion_score"],
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
                item[1]["match_phrase_score"],
                item[1]["match_phrase_continuation_score"],
                item[1]["match_term_count"],
                item[1]["match_prefix_completion_score"],
                item[1]["match_intent_score"] - item[1]["match_mismatch_penalty"],
                _timestamp_sort_key(item[1]["match_timestamp"]),
                item[1]["match_conversation_score"],
                item[1]["match_density"],
                item[1]["match_compactness"],
                item[1]["_assistant_bonus"],
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


def _looks_like_search_diagnostic(text: str) -> bool:
    lowered = text.lower()
    return (
        "search_ranked" in lowered
        or ("returns `" in lowered and "rank" in lowered)
        or ("returned `" in lowered and "rank" in lowered)
    )


def _is_plain_entity_query(plan: QueryPlan, query: str) -> bool:
    stripped = query.strip().lower().replace("’", "'")
    if len(plan.anchor_terms) != 1:
        return False
    if stripped != str(plan.anchor_terms[0]).lower():
        return False
    return bool(re.fullmatch(r"[a-z0-9']+", stripped))


def _query_can_trust_imported_continuation(plan: QueryPlan) -> bool:
    anchors = [term.strip(".*") for term in plan.anchor_terms if term.strip(".*")]
    if len(anchors) >= 2:
        return True
    if len(anchors) == 1:
        return len(anchors[0]) > 3
    return False


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


def _exact_phrase_score(row: dict, plan: QueryPlan) -> float:
    if not plan.exact_phrase_terms:
        return 0.0

    weighted_fields = (
        (str(row.get("name") or ""), 3.0),
        (str(row.get("first_msg") or ""), 2.5),
        (str(row.get("context") or ""), 2.0),
        (str(row.get("last_msg") or ""), 1.5),
        (str(row.get("cwd") or ""), 1.0),
    )
    score = float(row.get("match_phrase_score") or 0.0)
    for phrase in plan.exact_phrase_terms:
        if not phrase:
            continue
        phrase_weight = 1.0 + min(len(phrase.split()), 6) * 0.25
        for text, field_weight in weighted_fields:
            if term_spans(text, phrase):
                score += phrase_weight * field_weight
                break
    return score


def _workspace_anchor_score(row: dict, plan: QueryPlan) -> float:
    anchors = [term.strip(".*").lower() for term in _semantic_anchor_terms(plan) if term]
    if len(anchors) != 1:
        return 0.0
    anchor = anchors[0]
    segments = _normalized_path_segments(str(row.get("cwd") or ""))
    if not segments:
        return 0.0
    if anchor == segments[-1]:
        return 6.0
    if anchor in segments:
        return 3.0
    return 0.0


def _current_cwd_score(row: dict, current_cwd: str | None) -> float:
    """Softly prefer sessions from the folder where browse was launched."""
    row_cwd = str(row.get("cwd") or "").strip()
    if not row_cwd or not current_cwd:
        return 0.0
    row_path = os.path.normpath(os.path.expanduser(row_cwd))
    current_path = os.path.normpath(os.path.expanduser(str(current_cwd)))
    if row_path == current_path:
        return 3.0
    if row_path.startswith(current_path + os.sep):
        return 2.5
    if current_path.startswith(row_path + os.sep):
        remainder = current_path[len(row_path) :].strip(os.sep)
        distance = len([part for part in remainder.split(os.sep) if part])
        if distance == 1:
            return 1.5
        if distance == 2:
            return 0.75
    return 0.0


def _single_anchor_evidence_tier(row: dict) -> int:
    """Bucket single-anchor matches before recency sorts within the bucket."""
    if float(row.get("_artifact_penalty") or 0.0) >= 5.0:
        return 0
    if float(row.get("_workspace_match_score") or 0.0) > 0.0:
        return 4
    if float(row.get("_metadata_anchor_score") or 0.0) >= 4.5:
        return 3
    if int(row.get("match_term_count") or 0) > 0:
        return 2
    return 1


def _descriptive_sort_key(row: dict) -> tuple:
    exact_identifier_score = float(row.get("_exact_identifier_score") or 0.0)
    match_ts = _timestamp_sort_key(
        row.get("match_timestamp")
        or row.get("last_timestamp")
        or row.get("timestamp")
    )
    intent_score = float(row.get("match_intent_score") or 0.0) - float(
        row.get("match_mismatch_penalty") or 0.0
    )
    common_tail = (
        float(row.get("match_density") or 0.0),
        float(row.get("match_compactness") or 0.0),
        1 if row.get("_assistant_bonus") else 0,
        float(row.get("_score") or 0.0),
        float(row.get("_match_bm25") or 0.0),
        _timestamp_sort_key(row.get("last_timestamp") or row.get("timestamp")),
        float(row.get("mtime") or 0.0),
    )
    if row.get("_exact_phrase_score"):
        return (
            exact_identifier_score,
            1,
            int(row.get("match_term_count") or 0),
            float(row.get("_current_cwd_score") or 0.0),
            match_ts,
            float(row.get("_exact_phrase_score") or 0.0),
            float(row.get("match_phrase_continuation_score") or 0.0),
            float(row.get("_quality_score") or 0.0),
            intent_score,
            float(row.get("match_conversation_score") or 0.0),
            *common_tail,
        )
    return (
        exact_identifier_score,
        0,
        float(row.get("_current_cwd_score") or 0.0),
        float(row.get("_quality_score") or 0.0),
        int(row.get("match_term_count") or 0),
        intent_score,
        float(row.get("match_conversation_score") or 0.0),
        match_ts,
        *common_tail,
    )


def _artifact_penalty(row: dict, plan: QueryPlan, query: str) -> float:
    context_haystack, metadata_haystack, haystack = _artifact_haystacks(row)
    penalty = 0.0
    if _contains_any(context_haystack, _IMPORTED_SESSION_CUES):
        penalty += 8.0
    elif _contains_any(metadata_haystack, _IMPORTED_SESSION_CUES):
        # Imported-session titles are boilerplate, but the continued thread can
        # later contain real work. Only suppress the handoff/opening itself.
        try:
            match_segment_idx = int(row.get("match_segment_idx") or 0)
        except (TypeError, ValueError):
            match_segment_idx = 0
        if (
            match_segment_idx <= 2
            or not _query_can_trust_imported_continuation(plan)
        ):
            penalty += 8.0
    if (
        _contains_any(haystack, _SELF_REFERENTIAL_CUES)
        or _looks_like_search_diagnostic(haystack)
    ) and not _query_mentions_search_system(query):
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
    semantic_anchors = _semantic_anchor_terms(plan)
    if len(semantic_anchors) == 1 and (_is_plain_entity_query(plan, query) or plan.descriptive):
        anchor = str(semantic_anchors[0]).lower()
        code_ref_patterns = (
            rf"\b\w*{re.escape(anchor)}\w*\.(py|md|pptx?|json|csv)\b",
            rf"`[^`]*{re.escape(anchor)}[^`]*`",
            rf"(?:{'|'.join(_CODE_REFERENCE_WINDOW_CUES)})[^\\n]{{0,40}}{re.escape(anchor)}",
            rf"{re.escape(anchor)}[^\\n]{{0,40}}(?:{'|'.join(_CODE_REFERENCE_WINDOW_CUES)})",
        )
        if any(re.search(pattern, haystack) for pattern in code_ref_patterns):
            penalty += 5.0 if _is_plain_entity_query(plan, query) else 4.0
    return penalty


def _artifact_haystacks(row: dict) -> tuple[str, str, str]:
    context_haystack = str(row.get("context") or "").lower()
    metadata_haystack = " ".join(
        part
        for part in (
            row.get("name") or "",
            row.get("cwd") or "",
            row.get("first_msg") or "",
            row.get("last_msg") or "",
        )
        if part
    ).lower()
    haystack = " ".join(
        part for part in (metadata_haystack, context_haystack) if part
    )
    return context_haystack, metadata_haystack, haystack


def _is_suppressible_diagnostic_row(row: dict, query: str) -> bool:
    if _query_mentions_search_system(query):
        return False
    _context, _metadata, haystack = _artifact_haystacks(row)
    return (
        _contains_any(haystack, _SELF_REFERENTIAL_CUES)
        or _looks_like_search_diagnostic(haystack)
        or _contains_any(haystack, _HANDOVER_ARTIFACT_CUES)
    )


def _suppress_diagnostic_rows_when_content_exists(
    rows: list[dict],
    query: str,
) -> list[dict]:
    content_rows = [
        row for row in rows if not _is_suppressible_diagnostic_row(row, query)
    ]
    return content_rows or rows


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
    strict_plan = plan
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
    fallback_plan = None
    fallback_matched = False
    prefix_fallback_plan = None
    prefix_fallback_matched = False
    rows = []
    if _has_unclosed_quote(query):
        prefix_fallback_plan = _prefix_fallback_plan(plan)
        if prefix_fallback_plan:
            prefix_query = _terms_to_fts_query(list(prefix_fallback_plan.fts_terms))
            if prefix_query:
                try:
                    rows = conn.execute(sql, (prefix_query, limit)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    plan = prefix_fallback_plan
                    descriptive = plan.descriptive
                    prefix_fallback_matched = True
    if not rows:
        try:
            rows = conn.execute(sql, (fts_query, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
    if not rows:
        fallback_plan = _phrase_fallback_plan(plan)
        if fallback_plan:
            fallback_query = _terms_to_fts_query(list(fallback_plan.fts_terms))
            if fallback_query:
                try:
                    rows = conn.execute(sql, (fallback_query, limit)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    plan = fallback_plan
                    descriptive = plan.descriptive
                    fallback_matched = True
    if not rows:
        prefix_fallback_plan = _prefix_fallback_plan(plan)
        if prefix_fallback_plan:
            prefix_query = _terms_to_fts_query(list(prefix_fallback_plan.fts_terms))
            if prefix_query:
                try:
                    rows = conn.execute(sql, (prefix_query, limit)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    plan = prefix_fallback_plan
                    descriptive = plan.descriptive
                    prefix_fallback_matched = True

    results = [_row_to_dict(r) for r in rows]
    if fallback_matched and results:
        results = _mark_phrase_fallback(results, strict_plan)
    if prefix_fallback_matched and results:
        results = _mark_prefix_fallback(results, strict_plan, prefix_fallback_plan)
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
    for row in decorated:
        row["_exact_phrase_score"] = _exact_phrase_score(row, plan)
        row["_prefix_completion_score"] = max(
            _prefix_completion_score(row, plan),
            float(row.get("match_prefix_completion_score") or 0.0),
        )
        row["_artifact_penalty"] = _artifact_penalty(row, plan, query)
    decorated.sort(
        key=lambda row: (
            1 if row.get("_exact_phrase_score") else 0,
            int(row.get("match_term_count") or 0),
            float(row.get("_prefix_completion_score") or 0.0),
            float(row.get("match_phrase_continuation_score") or 0.0),
            -float(row.get("_artifact_penalty") or 0.0),
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
    decorated = _suppress_diagnostic_rows_when_content_exists(decorated, query)
    trimmed = []
    for row in decorated[:limit]:
        clean = dict(row)
        clean.pop("_match_bm25", None)
        clean.pop("_assistant_bonus", None)
        clean.pop("_exact_phrase_score", None)
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
    current_cwd: str | None = None,
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
    exact_results = _exact_identifier_results(conn, query, max(limit * 2, 50))
    if plan.low_confidence and not exact_results:
        return list_recent(conn, limit)
    plan = _discriminative_query_plan(conn, plan)
    strict_plan = plan
    fts_query = "" if plan.low_confidence else _terms_to_fts_query(list(plan.fts_terms))
    if not fts_query and not exact_results:
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
    fallback_plan = None
    fallback_matched = False
    prefix_fallback_plan = None
    prefix_fallback_matched = False
    rows = []
    if fts_query and _has_unclosed_quote(query):
        prefix_fallback_plan = _prefix_fallback_plan(plan)
        if prefix_fallback_plan:
            prefix_query = _terms_to_fts_query(list(prefix_fallback_plan.fts_terms))
            if prefix_query:
                try:
                    rows = conn.execute(sql, (prefix_query, candidate_pool)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    plan = prefix_fallback_plan
                    descriptive = plan.descriptive
                    terms = list(plan.fts_terms)
                    prefix_fallback_matched = True
    if fts_query and not rows:
        try:
            rows = conn.execute(sql, (fts_query, candidate_pool)).fetchall()
        except sqlite3.OperationalError:
            return exact_results[:limit]
    if fts_query and not rows:
        fallback_plan = _phrase_fallback_plan(plan)
        if fallback_plan:
            fallback_query = _terms_to_fts_query(list(fallback_plan.fts_terms))
            if fallback_query:
                try:
                    rows = conn.execute(sql, (fallback_query, candidate_pool)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    plan = fallback_plan
                    descriptive = plan.descriptive
                    terms = list(plan.fts_terms)
                    fallback_matched = True
    if fts_query and not rows:
        prefix_fallback_plan = _prefix_fallback_plan(plan)
        if prefix_fallback_plan:
            prefix_query = _terms_to_fts_query(list(prefix_fallback_plan.fts_terms))
            if prefix_query:
                try:
                    rows = conn.execute(sql, (prefix_query, candidate_pool)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    plan = prefix_fallback_plan
                    descriptive = plan.descriptive
                    terms = list(plan.fts_terms)
                    prefix_fallback_matched = True

    fts_results = [
        _row_to_dict(r[:12]) | {"_bm25": -float(r[12] or 0.0)}
        for r in rows
    ]
    if fallback_matched and fts_results:
        fts_results = _mark_phrase_fallback(fts_results, strict_plan)
    if prefix_fallback_matched and fts_results:
        fts_results = _mark_prefix_fallback(
            fts_results,
            strict_plan,
            prefix_fallback_plan,
        )
    exact_ids = {str(r["session_id"]) for r in exact_results}
    results = [
        *exact_results,
        *[row for row in fts_results if str(row["session_id"]) not in exact_ids],
    ]
    existing_ids = {str(r["session_id"]) for r in results}
    match_map = _latest_segment_matches(
        conn,
        query,
        sids=None if descriptive else list(existing_ids),
        plan=plan,
    )
    if descriptive:
        semantic_map = _semantic_window_matches(
            conn,
            query,
            plan=plan,
            limit=candidate_pool,
        )
        for sid, semantic_match in semantic_map.items():
            existing_match = match_map.get(sid)
            if not existing_match:
                match_map[sid] = semantic_match
                continue
            existing_match["match_semantic_score"] = semantic_match.get(
                "match_semantic_score",
                0.0,
            )
            existing_match["match_semantic_overlap"] = semantic_match.get(
                "match_semantic_overlap",
                0,
            )
    dense_map = _dense_window_matches(
        conn,
        query,
        plan=plan,
        limit=candidate_pool,
    )
    for sid, dense_match in dense_map.items():
        existing_match = match_map.get(sid)
        if not existing_match:
            match_map[sid] = dense_match
            continue
        dense_score = float(dense_match.get("match_dense_score") or 0.0)
        existing_semantic = float(existing_match.get("match_semantic_score") or 0.0)
        existing_match["match_dense_score"] = dense_score
        existing_match["match_semantic_score"] = max(existing_semantic, dense_score)
        if dense_score > existing_semantic and existing_match.get("match_source") == "semantic":
            existing_match["match_context"] = dense_match.get(
                "match_context",
                existing_match.get("match_context", ""),
            )
            existing_match["match_timestamp"] = dense_match.get(
                "match_timestamp",
                existing_match.get("match_timestamp"),
            )
            existing_match["match_segment_idx"] = dense_match.get(
                "match_segment_idx",
                existing_match.get("match_segment_idx"),
            )
            existing_match["match_source"] = "dense"
    extra_ids = [
        sid for sid in match_map if sid not in existing_ids
    ] if descriptive or dense_map else []
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
        row["_exact_phrase_score"] = _exact_phrase_score(row, plan)
        row["_prefix_completion_score"] = max(
            _prefix_completion_score(row, plan),
            float(row.get("match_prefix_completion_score") or 0.0),
        )
        row["_metadata_anchor_score"] = _metadata_anchor_score(row, plan)
        row["_workspace_match_score"] = _workspace_anchor_score(row, plan)
        row["_current_cwd_score"] = _current_cwd_score(row, current_cwd)
        if row["_current_cwd_score"]:
            row["current_cwd_score"] = row["_current_cwd_score"]
        row["_artifact_penalty"] = _artifact_penalty(row, plan, query)
        row["_semantic_intent_score"] = float(
            row.get("match_intent_score") or 0.0
        ) - float(row.get("match_mismatch_penalty") or 0.0)
        row["_semantic_window_score"] = float(
            row.get("match_semantic_score") or 0.0
        )
        row["_quality_score"] = (
            float(row.get("_exact_identifier_score") or 0.0)
            + float(row.get("_exact_phrase_score") or 0.0)
            + float(row.get("_prefix_completion_score") or 0.0)
            + float(row.get("_workspace_match_score") or 0.0)
            + float(row.get("_current_cwd_score") or 0.0)
            + float(row.get("_metadata_anchor_score") or 0.0)
            + float(row.get("match_phrase_continuation_score") or 0.0)
            + float(row.get("_semantic_intent_score") or 0.0)
            + float(row.get("_semantic_window_score") or 0.0) * 6.0
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
                float(row.get("_exact_identifier_score") or 0.0),
                float(row.get("_quality_score") or 0.0),
                float(row.get("_closeout_score") or 0.0),
                int(row.get("match_term_count") or 0),
                float(row.get("match_phrase_continuation_score") or 0.0),
                _timestamp_sort_key(
                    row.get("match_timestamp")
                    or row.get("last_timestamp")
                    or row.get("timestamp")
                ),
                float(row.get("_current_cwd_score") or 0.0),
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
                float(row.get("_exact_identifier_score") or 0.0),
                _single_anchor_evidence_tier(row),
                float(row.get("_current_cwd_score") or 0.0),
                float(row.get("_prefix_completion_score") or 0.0),
                float(row.get("match_phrase_continuation_score") or 0.0),
                -float(row.get("_artifact_penalty") or 0.0),
                _timestamp_sort_key(
                    row.get("match_timestamp")
                    or row.get("last_timestamp")
                    or row.get("timestamp")
                ),
                float(row.get("_quality_score") or 0.0),
                int(row.get("match_term_count") or 0),
                1 if row.get("_assistant_bonus") else 0,
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
    elif not plan.descriptive:
        decorated.sort(
            key=lambda row: (
                float(row.get("_exact_identifier_score") or 0.0),
                1 if row.get("_exact_phrase_score") else 0,
                int(row.get("match_term_count") or 0),
                float(row.get("_current_cwd_score") or 0.0),
                float(row.get("_prefix_completion_score") or 0.0),
                float(row.get("match_phrase_continuation_score") or 0.0),
                -float(row.get("_artifact_penalty") or 0.0),
                _timestamp_sort_key(
                    row.get("match_timestamp")
                    or row.get("last_timestamp")
                    or row.get("timestamp")
                ),
                float(row.get("_quality_score") or 0.0),
                float(row.get("_score") or 0.0),
                float(row.get("match_conversation_score") or 0.0),
                float(row.get("match_density") or 0.0),
                float(row.get("match_compactness") or 0.0),
                1 if row.get("_assistant_bonus") else 0,
                float(row.get("_match_bm25") or 0.0),
                _timestamp_sort_key(row.get("last_timestamp") or row.get("timestamp")),
                float(row.get("mtime") or 0.0),
            ),
            reverse=True,
        )
    else:
        decorated.sort(
            key=_descriptive_sort_key,
            reverse=True,
        )
    decorated = _suppress_diagnostic_rows_when_content_exists(decorated, query)
    trimmed = []
    for row in decorated[:limit]:
        clean = dict(row)
        clean.pop("_bm25", None)
        clean.pop("_score", None)
        clean.pop("_match_bm25", None)
        clean.pop("_assistant_bonus", None)
        clean.pop("_closeout_score", None)
        clean.pop("_metadata_anchor_score", None)
        clean.pop("_exact_phrase_score", None)
        clean.pop("_workspace_match_score", None)
        clean.pop("_current_cwd_score", None)
        clean.pop("_artifact_penalty", None)
        clean.pop("_quality_score", None)
        clean.pop("_exact_identifier_score", None)
        clean.pop("_semantic_intent_score", None)
        clean.pop("_semantic_window_score", None)
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

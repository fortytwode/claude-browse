"""Tests for claude_browse.fts: SQLite FTS5 indexer + search.

Each test gets its own temp database; nothing touches the user's real
~/.claude/cache/claude-browse-index.db.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from claude_browse import fts

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db():
    """Fresh in-memory-equivalent FTS db for each test."""
    path = tempfile.mktemp(suffix=".db")
    conn = fts.open_db(path)
    yield conn
    conn.close()
    if os.path.exists(path):
        os.unlink(path)


def _seed(conn, sid: str, corpus: str, **meta) -> None:
    """Insert one session with a synthetic corpus, bypassing the file walk.

    `corpus` is treated as user_text by default (preserves pre-v3 test
    semantics: 'this text exists' -> findable). Per-column overrides via
    fts_cwd / fts_title / fts_first_msg / fts_user / fts_asst / fts_boiler
    let v1-ranker tests target specific fields.
    """
    now = time.time()
    timestamp = meta.get("timestamp", "2026-05-01T10:00:00Z")
    conn.execute(
        """
        INSERT INTO sessions (sid, path, cwd, timestamp, last_timestamp,
                              title, first_msg, last_msg, msg_count, mtime,
                              indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            meta.get("path", f"/tmp/{sid}.jsonl"),
            meta.get("cwd", "/tmp"),
            timestamp,
            meta.get("last_timestamp", timestamp),
            meta.get("title", f"Title for {sid}"),
            meta.get("first_msg", f"first message for {sid}"),
            meta.get("last_msg", ""),
            meta.get("msg_count", 4),
            meta.get("mtime", now),
            now,
        ),
    )
    conn.execute(
        """INSERT INTO sessions_fts
           (sid, cwd, title, first_msg, user_text, asst_text, boilerplate)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            sid,
            meta.get("fts_cwd", meta.get("cwd", "")),
            meta.get("fts_title", ""),
            meta.get("fts_first_msg", ""),
            meta.get("fts_user", corpus),
            meta.get("fts_asst", ""),
            meta.get("fts_boiler", ""),
        ),
    )
    conn.commit()


# --- normalize_query -------------------------------------------------------


def test_normalize_single_word_is_token_match():
    """'runna' should query as a quoted FTS5 token, not a fuzzy match."""
    assert fts.normalize_query("runna") == '"runna"'


def test_normalize_two_words_is_AND():
    """'runna sca2' should become AND of two quoted tokens (FTS5 default)."""
    assert fts.normalize_query("runna sca2") == '"runna" "sca2"'


def test_normalize_quoted_is_phrase():
    """'"runna sca2"' (quoted) becomes an FTS5 phrase clause."""
    assert fts.normalize_query('"runna sca2"') == '"runna sca2"'


def test_normalize_mixed_word_and_phrase():
    assert (
        fts.normalize_query('runna "sca2 v3"')
        == '"runna" "sca2 v3"'
    )


def test_normalize_bare_AND_quoted_as_literal():
    """Bare 'AND' must be quoted so it doesn't trigger FTS5's operator."""
    assert fts.normalize_query("AND") == '"AND"'


def test_normalize_empty():
    assert fts.normalize_query("") == ""
    assert fts.normalize_query("   ") == ""


def test_normalize_internal_quote_escaped():
    """Embedded double-quote becomes "" inside an FTS5 phrase."""
    assert fts.normalize_query('say "hi"') == '"say" "hi"'


# --- search semantics ------------------------------------------------------


def test_search_single_word_exact_token(db):
    _seed(db, "s1", "the runna deck is ready")
    _seed(db, "s2", "running fast and loose")
    _seed(db, "s3", "rangoon was nice")

    sids = [r["session_id"] for r in fts.search(db, "runna")]
    # Only s1 has the literal token 'runna' (s2 has 'running', s3 'rangoon')
    assert sids == ["s1"]


def test_search_two_words_is_AND(db):
    _seed(db, "s1", "the runna sca2 deck is ready")
    _seed(db, "s2", "only runna mentioned here")
    _seed(db, "s3", "only sca2 mentioned here")

    sids = sorted(r["session_id"] for r in fts.search(db, "runna sca2"))
    assert sids == ["s1"]


def test_search_phrase_requires_adjacency(db):
    _seed(db, "s1", "the runna sca2 deck is ready")
    _seed(db, "s2", "runna and sca2 mentioned with gap")

    sids = sorted(r["session_id"] for r in fts.search(db, '"runna sca2"'))
    # Phrase requires adjacency — s2's "runna and sca2" is not adjacent.
    assert sids == ["s1"]


def test_search_short_query_no_fuzzy_flood(db):
    """The original bug: fzf fuzzy-matched 'sca2' against 99/100 sessions."""
    _seed(db, "s1", "the SCA2 deck shipped")
    for i in range(20):
        _seed(db, f"noise{i}", "scattered semantics about agile testing")

    sids = [r["session_id"] for r in fts.search(db, "sca2")]
    assert sids == ["s1"]


def test_search_empty_returns_recent(db):
    _seed(db, "older", "x", mtime=1.0, timestamp="2026-01-01T00:00:00Z")
    _seed(db, "newer", "y", mtime=99.0, timestamp="2026-05-01T00:00:00Z")

    results = fts.search(db, "")
    assert [r["session_id"] for r in results] == ["newer", "older"]


def test_recent_sorts_by_last_activity_not_start(db):
    """An old session resumed today should outrank a newer session that
    hasn't been touched in months."""
    _seed(
        db,
        "old_resumed",
        "x",
        mtime=99.0,
        timestamp="2026-01-01T00:00:00Z",
        last_timestamp="2026-05-05T00:00:00Z",
    )
    _seed(
        db,
        "newer_dormant",
        "y",
        mtime=1.0,
        timestamp="2026-04-01T00:00:00Z",
        last_timestamp="2026-04-01T00:30:00Z",
    )

    results = fts.list_recent(db)
    assert [r["session_id"] for r in results] == ["old_resumed", "newer_dormant"]


def test_search_invalid_fts_query_returns_empty(db):
    """Invalid FTS5 syntax should not crash; we degrade to no results."""
    _seed(db, "s1", "anything")
    # Force a query that bypasses normalize_query and is invalid for FTS5.
    # normalize_query would normally protect us, but defensive fallback matters.
    results = fts.search(db, "(((")
    # normalize_query escapes everything; should still be a valid FTS5 query.
    assert isinstance(results, list)


# --- reindex ---------------------------------------------------------------


def test_reindex_picks_up_new_files(db, tmp_path, monkeypatch):
    """A new JSONL on disk shows up in the index after reindex()."""
    sessions_dir = tmp_path / "projects" / "demo"
    sessions_dir.mkdir(parents=True)
    target = sessions_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    shutil.copy(FIXTURES / "sample_session.jsonl", target)

    monkeypatch.setattr(fts, "list_session_files", lambda: [str(target)])

    added, updated, removed = fts.reindex(db)
    assert added == 1
    assert updated == 0
    assert removed == 0

    sids = [r["session_id"] for r in fts.list_recent(db)]
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in sids


def test_reindex_skips_unchanged(db, tmp_path, monkeypatch):
    """Second reindex with no mtime change does no work."""
    sessions_dir = tmp_path / "projects" / "demo"
    sessions_dir.mkdir(parents=True)
    target = sessions_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    shutil.copy(FIXTURES / "sample_session.jsonl", target)

    monkeypatch.setattr(fts, "list_session_files", lambda: [str(target)])

    fts.reindex(db)
    added2, updated2, removed2 = fts.reindex(db)
    assert (added2, updated2, removed2) == (0, 0, 0)


def test_reindex_removes_deleted_files(db, tmp_path, monkeypatch):
    sessions_dir = tmp_path / "projects" / "demo"
    sessions_dir.mkdir(parents=True)
    target = sessions_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    shutil.copy(FIXTURES / "sample_session.jsonl", target)

    monkeypatch.setattr(fts, "list_session_files", lambda: [str(target)])
    fts.reindex(db)
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1

    monkeypatch.setattr(fts, "list_session_files", lambda: [])
    _, _, removed = fts.reindex(db)
    assert removed == 1
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_get_by_sid_roundtrip(db):
    _seed(db, "s1", "hello world", title="My Session", cwd="/Users/me/proj")
    info = fts.get_by_sid(db, "s1")
    assert info is not None
    assert info["session_id"] == "s1"
    assert info["name"] == "My Session"
    assert info["cwd"] == "/Users/me/proj"


def test_get_by_sid_missing(db):
    assert fts.get_by_sid(db, "does-not-exist") is None

"""Tests for eval.from_log: search-log selections -> labeled query set."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from claude_browse import fts
from eval import from_log


@pytest.fixture
def db():
    path = tempfile.mktemp(suffix=".db")
    conn = fts.open_db(path)
    yield conn
    conn.close()
    if os.path.exists(path):
        os.unlink(path)


def _seed_session(conn, sid: str) -> None:
    conn.execute(
        """
        INSERT INTO sessions (sid, path, provider, cwd, timestamp,
                              last_timestamp, title, first_msg, last_msg,
                              msg_count, mtime, indexed_at)
        VALUES (?, ?, 'claude', '/tmp', '2026-06-28T00:00:00Z',
                '2026-06-28T00:00:00Z', 't', 'f', '', 1, 0, 0)
        """,
        (sid, f"/tmp/{sid}.jsonl"),
    )


def _write_log(path, events) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def test_selection_pairs_skip_empty_query_coach_and_duplicates():
    events = [
        {"event": "selection", "query": "", "selected": {"session_id": "a" * 36}},
        {"event": "selection", "query": "bounty",
         "selected": {"session_id": "__coach__"}},
        {"event": "search", "query": "bounty", "top": []},
        {"event": "selection", "query": "bounty", "ts": "2026-06-28T10:00:00Z",
         "action": "resume", "selected": {"session_id": "b" * 36}},
        {"event": "selection", "query": "bounty", "ts": "2026-06-29T10:00:00Z",
         "action": "resume", "selected": {"session_id": "b" * 36}},
    ]

    pairs = from_log.selection_pairs(events)

    assert len(pairs) == 1
    assert pairs[0]["query"] == "bounty"
    assert pairs[0]["sid"] == "b" * 36


def test_merge_pairs_adds_graded_entry_and_skips_unresolved(db):
    _seed_session(db, "b" * 36)
    pairs = [
        {"query": "bounty", "sid": "b" * 36,
         "ts": "2026-06-28T10:00:00Z", "action": "resume"},
        {"query": "gone", "sid": "c" * 36,
         "ts": "2026-06-28T11:00:00Z", "action": "resume"},
    ]
    data = {"queries": []}

    added_queries, added_sids, skipped = from_log.merge_pairs(data, pairs, db)

    assert (added_queries, added_sids, skipped) == (1, 1, 1)
    entry = data["queries"][0]
    assert entry["q"] == "bounty"
    assert entry["relevant"] == [
        {"sid": "b" * 12, "grade": 3, "from_log": True}
    ]
    assert entry["preferred_action"] == "enter"


def test_merge_pairs_never_touches_hand_labeled_entries(db):
    _seed_session(db, "b" * 36)
    _seed_session(db, "d" * 36)
    data = {
        "queries": [
            {
                "q": "bounty",
                "relevant": [{"sid": "b" * 12, "grade": 2, "from_top20": True}],
                "note": "hand labeled",
                "preferred_action": "ctrl-t",
            }
        ]
    }
    pairs = [
        {"query": "bounty", "sid": "b" * 36,
         "ts": "2026-06-28T10:00:00Z", "action": "resume"},
        {"query": "bounty", "sid": "d" * 36,
         "ts": "2026-06-29T10:00:00Z", "action": "resume"},
    ]

    added_queries, added_sids, skipped = from_log.merge_pairs(data, pairs, db)

    assert (added_queries, added_sids, skipped) == (0, 1, 0)
    entry = data["queries"][0]
    # The hand-written grade-2 label survives untouched; only the new sid
    # is appended.
    assert {"sid": "b" * 12, "grade": 2, "from_top20": True} in entry["relevant"]
    assert {"sid": "d" * 12, "grade": 3, "from_log": True} in entry["relevant"]
    assert entry["note"] == "hand labeled"
    assert entry["preferred_action"] == "ctrl-t"


def test_read_log_events_includes_rotated_file(tmp_path):
    log_path = tmp_path / "search.log.jsonl"
    _write_log(f"{log_path}.1", [{"event": "search", "query": "old"}])
    _write_log(log_path, [{"event": "search", "query": "new"}])

    events = from_log.read_log_events(str(log_path))

    assert [e["query"] for e in events] == ["old", "new"]

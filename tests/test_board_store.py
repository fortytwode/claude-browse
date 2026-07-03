"""Tests for the local SQLite session-state store (board/store.py)."""

from __future__ import annotations

import time

from claude_browse.board import store


def _fresh_store(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(store, "_DB_PATH", db_path)
    return db_path


def test_upsert_then_get_roundtrips_state(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    store.upsert("sess-1", host="air", cwd="/tmp/proj", state="idle", name="foo")
    row = store.get("sess-1")

    assert row is not None
    assert row["state"] == "idle"
    assert row["name"] == "foo"
    assert row["updated_at"] is not None


def test_set_state_working_then_idle_preserves_working_since(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    store.upsert("sess-2", host="air", cwd="/tmp/proj", state="idle", name="bar")
    t0 = time.time()
    store.set_state("sess-2", "working", working_since=t0)
    store.set_state("sess-2", "idle")

    row = store.get("sess-2")
    assert row["state"] == "idle"
    assert row["working_since"] == t0


def test_active_excludes_stale_ended_includes_recent_idle_newest_first(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    old_ended_ts = time.time() - (48 * 3600)
    store.upsert("sess-old-ended", host="air", cwd="/tmp/a", state="ended", name="old")
    store._raw_set_updated_at("sess-old-ended", old_ended_ts)

    store.upsert("sess-recent", host="air", cwd="/tmp/b", state="idle", name="recent")

    rows = store.active(max_age_hours=24)
    ids = [r["session_id"] for r in rows]

    assert "sess-old-ended" not in ids
    assert "sess-recent" in ids
    assert ids[0] == "sess-recent"


def test_concurrent_upserts_different_sessions_do_not_lock(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    for i in range(20):
        store.upsert(f"sess-{i}", host="air", cwd="/tmp", state="working", name=f"n{i}")

    rows = {r["session_id"] for r in store.active(max_age_hours=24)}
    assert rows == {f"sess-{i}" for i in range(20)}


def test_get_unknown_session_returns_none(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    assert store.get("does-not-exist") is None


def test_display_state_gone_after_stale_heartbeat():
    now = time.time()
    stale_row = {"state": "working", "heartbeat_at": now - 700, "updated_at": now - 700}
    fresh_row = {"state": "working", "heartbeat_at": now - 5, "updated_at": now - 5}
    ended_row = {"state": "ended", "heartbeat_at": now - 999999, "updated_at": now - 999999}

    assert store.display_state(stale_row, stale_after_s=600) == "gone"
    assert store.display_state(fresh_row, stale_after_s=600) == "working"
    assert store.display_state(ended_row, stale_after_s=600) == "ended"


def test_display_state_falls_back_to_updated_at_when_no_heartbeat():
    now = time.time()
    row = {"state": "idle", "heartbeat_at": None, "updated_at": now - 700}
    assert store.display_state(row, stale_after_s=600) == "gone"


def test_heartbeat_updates_heartbeat_at(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    store.upsert("sess-hb", host="air", cwd="/tmp", state="working", name="hb")
    before = store.get("sess-hb")["heartbeat_at"]

    time.sleep(0.01)
    store.heartbeat("sess-hb")

    after = store.get("sess-hb")["heartbeat_at"]
    assert after is not None
    assert before is None or after > before

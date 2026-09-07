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


def test_set_state_backfills_host_when_provided_on_a_hostless_row(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    store.set_state("sess-hostless", "working")  # no host arg -- row starts with host=None
    assert store.get("sess-hostless")["host"] is None

    store.set_state("sess-hostless", "idle", host="real-hostname", model_label="Codex")
    row = store.get("sess-hostless")
    assert row["host"] == "real-hostname"
    assert row["model_label"] == "Codex"


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


def test_stale_ended_unattended_completion_remains_visible_and_findable_until_ack(
    tmp_path, monkeypatch
):
    _fresh_store(tmp_path, monkeypatch)

    store.upsert(
        "sess-old-unattended",
        host="air",
        cwd="/tmp/a",
        state="idle",
        name="old unattended completion",
    )
    store.mark_done("sess-old-unattended", 900)
    store.set_state("sess-old-unattended", "ended")
    old_ts = time.time() - (30 * 24 * 3600)
    store._raw_set_updated_at("sess-old-unattended", old_ts)

    assert [r["session_id"] for r in store.active(max_age_hours=24)] == [
        "sess-old-unattended"
    ]
    assert [r["session_id"] for r in store.unattended(max_age_hours=24)] == [
        "sess-old-unattended"
    ]
    assert [r["session_id"] for r in store.find("old unattended")] == [
        "sess-old-unattended"
    ]

    store.ack("sess-old-unattended")
    store._raw_set_updated_at("sess-old-unattended", old_ts)

    assert store.active(max_age_hours=24) == []
    assert store.unattended(max_age_hours=24) == []
    assert store.find("old unattended") == []


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


def test_display_state_handles_row_missing_state_key_without_raising():
    """Regression: display_state used row["state"] (unguarded) elsewhere it
    used .get() -- safe for local SQLite rows (schema-guaranteed) but not
    for Firestore-sourced dicts sync.py feeds it, which have no schema
    guarantee (e.g. a doc written before a field existed)."""
    now = time.time()
    row_no_state_recent = {"heartbeat_at": now - 5, "updated_at": now - 5}
    row_no_state_stale = {"heartbeat_at": now - 700, "updated_at": now - 700}

    assert store.display_state(row_no_state_recent) == "gone"
    assert store.display_state(row_no_state_stale) == "gone"


def test_heartbeat_updates_heartbeat_at(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    store.upsert("sess-hb", host="air", cwd="/tmp", state="working", name="hb")
    before = store.get("sess-hb")["heartbeat_at"]

    time.sleep(0.01)
    store.heartbeat("sess-hb")

    after = store.get("sess-hb")["heartbeat_at"]
    assert after is not None
    assert before is None or after > before


def test_pending_alert_set_and_clear(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("sess-alert", host="air", cwd="/tmp", state="idle", name="foo")

    store.set_pending_alert("sess-alert", "needs-input")
    assert store.get("sess-alert")["pending_alert"] == "needs-input"

    store.clear_pending_alert("sess-alert")
    assert store.get("sess-alert")["pending_alert"] is None


def test_sync_revisions_track_only_unpublished_transitions(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("dirty", host="air", state="idle")
    store.upsert("clean", host="air", state="idle")

    first = store.mark_sync_pending("dirty")
    second = store.mark_sync_pending("dirty")

    assert (first, second) == (1, 2)
    assert [row["session_id"] for row in store.pending_sync()] == ["dirty"]
    assert store.mark_sync_published("dirty", first) is True
    assert [row["session_id"] for row in store.pending_sync()] == ["dirty"]
    assert store.mark_sync_published("dirty", second) is True
    assert store.pending_sync() == []


def test_pending_alert_consume_is_revision_guarded(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert(
        "alert", host="air", state="needs-input", pending_alert="needs-input"
    )
    old_revision = store.mark_sync_pending("alert")
    store.set_pending_alert("alert", "needs-input")
    new_revision = store.mark_sync_pending("alert")

    assert store.consume_pending_alert("alert", "needs-input", old_revision) is None
    assert store.get("alert")["pending_alert_revision"] == new_revision

    consumed = store.consume_pending_alert("alert", "needs-input", new_revision)
    assert consumed is not None and consumed["state"] == "needs-input"
    assert store.get("alert")["pending_alert"] is None


def test_get_conn_is_cached_but_invalidates_on_db_path_change(tmp_path, monkeypatch):
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"

    monkeypatch.setattr(store, "_DB_PATH", path_a)
    conn1 = store.get_conn()
    conn2 = store.get_conn()
    assert conn1 is conn2  # cached within the same _DB_PATH

    monkeypatch.setattr(store, "_DB_PATH", path_b)
    conn3 = store.get_conn()
    assert conn3 is not conn1  # invalidated when _DB_PATH changes (test isolation)


def test_upsert_and_get_named_at_msg_count(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("sess-named", host="air", cwd="/tmp", state="idle",
                 name="foo", name_source="haiku", named_at_msg_count=42)

    row = store.get("sess-named")
    assert row["named_at_msg_count"] == 42


def test_migration_adds_new_columns_to_a_pre_existing_older_schema_db(tmp_path, monkeypatch):
    """Regression: adding a column to _SCHEMA does nothing for a database
    that already exists with an older schema -- CREATE TABLE IF NOT EXISTS
    only fires on first creation. Simulates a real machine's existing db
    predating this column."""
    import sqlite3

    db_path = tmp_path / "old_state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, host TEXT, cwd TEXT, name TEXT,
            name_source TEXT, state TEXT, working_since REAL,
            heartbeat_at REAL, updated_at REAL, msg_count INTEGER
        )"""
    )
    conn.execute(
        "INSERT INTO sessions (session_id, state, name) VALUES (?, ?, ?)",
        ("pre-existing-row", "idle", "pre-migration-name"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(store, "_DB_PATH", db_path)

    # get_conn() (called by any store function) must migrate in the missing column
    row = store.get("pre-existing-row")
    assert row["name"] == "pre-migration-name"  # old data preserved
    assert row["named_at_msg_count"] is None  # new column present, defaults NULL
    assert row["model_label"] is None
    assert row["sync_revision"] == 0
    assert row["published_revision"] == 0
    assert row["pending_alert_revision"] is None

    store.upsert("pre-existing-row", named_at_msg_count=7, model_label="Opus")
    row = store.get("pre-existing-row")
    assert row["named_at_msg_count"] == 7
    assert row["model_label"] == "Opus"


# ---------------------------------------------------------------------------
# Unattended-completion helpers (2026-09 redesign)
# ---------------------------------------------------------------------------

def test_new_columns_migrate_onto_an_old_schema_db(tmp_path, monkeypatch):
    """A machine that ran the pre-redesign board has a sessions table without
    provider/done_at/done_turn_s/acked_at; _migrate must add them."""
    import sqlite3

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, host TEXT, cwd TEXT, name TEXT, "
        "name_source TEXT, state TEXT, working_since REAL, heartbeat_at REAL, updated_at REAL, "
        "msg_count INTEGER, named_at_msg_count INTEGER, model_label TEXT, pending_alert TEXT)"
    )
    conn.execute("INSERT INTO sessions (session_id, state) VALUES ('legacy', 'idle')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "_DB_PATH", db)
    monkeypatch.setattr(store, "_conn_cache", None)

    row = store.get("legacy")
    assert row["provider"] is None and row["done_at"] is None
    assert store.provider_of(row) == "claude"  # legacy rows are Claude


def test_mark_done_then_prompt_clears_and_ack_supersedes(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    store.upsert("s", host="air", cwd="/tmp/p", state="idle")
    assert store.is_unattended(store.get("s")) is False

    store.mark_done("s", 720.0)
    row = store.get("s")
    assert row["done_turn_s"] == 720.0
    assert store.is_unattended(row) is True
    assert [r["session_id"] for r in store.unattended()] == ["s"]

    store.ack("s")
    assert store.is_unattended(store.get("s")) is False

    store.mark_done("s", 800.0)  # a later completion re-opens it despite the old ack
    assert store.is_unattended(store.get("s")) is True

    store.clear_done("s")
    assert store.is_unattended(store.get("s")) is False


def test_is_unattended_ignores_working_and_counts_ended(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    store.upsert("w", host="air", cwd="/tmp/p", state="working")
    store.mark_done("w", 400.0)
    store.upsert("w", state="working")
    assert store.is_unattended(store.get("w")) is False

    store.upsert("e", host="air", cwd="/tmp/p", state="idle")
    store.mark_done("e", 400.0)
    store.upsert("e", state="ended")
    assert store.is_unattended(store.get("e")) is True  # closed window != picked up


def test_find_matches_id_prefix_and_name_substring(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    store.upsert("53a5d575-aaaa", host="air", cwd="/tmp/p", state="idle", name="Review beat structure")
    store.upsert("372809ec-bbbb", host="air", cwd="/tmp/p", state="idle", name="cash reporting fixes")

    assert [r["session_id"] for r in store.find("53a5")] == ["53a5d575-aaaa"]
    assert [r["session_id"] for r in store.find("CASH report")] == ["372809ec-bbbb"]
    assert store.find("") == []

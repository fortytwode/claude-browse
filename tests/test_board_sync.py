"""Tests for Firestore cross-laptop sync (board/sync.py)."""

from __future__ import annotations

import os

from claude_browse.board import store, sync


def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")


class _FakeDocRef:
    def __init__(self, sink, doc_id):
        self.sink = sink
        self.doc_id = doc_id

    def set(self, data):
        self.sink[self.doc_id] = data


class _FakeCollection:
    def __init__(self, sink):
        self.sink = sink

    def document(self, doc_id):
        return _FakeDocRef(self.sink, doc_id)


class _FakeClient:
    def __init__(self):
        self.sink = {}

    def collection(self, name):
        assert name == sync.COLLECTION
        return _FakeCollection(self.sink)


def test_push_writes_doc_keyed_by_host_and_session_id(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s1", host="air", cwd="/tmp/proj", state="idle", name="foo")
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "post_or_update_slack", lambda body: None)  # U7 concern; isolated here

    fake_client = _FakeClient()
    monkeypatch.setattr(sync, "_firestore_client", lambda: fake_client)

    sync.push("s1")

    assert "air:s1" in fake_client.sink
    assert fake_client.sink["air:s1"]["state"] == "idle"
    assert fake_client.sink["air:s1"]["name"] == "foo"


def test_push_still_writes_ended_state(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s2", host="air", cwd="/tmp/proj", state="ended", name="done-thread")
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "post_or_update_slack", lambda body: None)  # U7 concern; isolated here

    fake_client = _FakeClient()
    monkeypatch.setattr(sync, "_firestore_client", lambda: fake_client)

    sync.push("s2")

    assert fake_client.sink["air:s2"]["state"] == "ended"


def test_push_no_op_when_row_missing(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    calls = []
    monkeypatch.setattr(sync, "_firestore_client", lambda: calls.append("called"))

    sync.push("does-not-exist")

    assert calls == []


def test_push_never_raises_when_client_construction_fails(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s3", host="air", cwd="/tmp/proj", state="idle", name="foo")
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "post_or_update_slack", lambda body: None)  # U7 concern; isolated here

    def _raise():
        raise RuntimeError("no creds")

    monkeypatch.setattr(sync, "_firestore_client", _raise)

    sync.push("s3")  # must not raise


def test_load_env_fallback_fills_missing_key_without_overwriting_existing(tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text("SLACK_BOT_TOKEN=xoxb-from-file\nOTHER_KEY=fromfile\n")
    monkeypatch.setenv("AGENT_BOARD_ENV_FILE", str(env_file))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("OTHER_KEY", "already-set-in-env")

    sync._load_env_fallback()

    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-from-file"
    assert os.environ["OTHER_KEY"] == "already-set-in-env"  # not overwritten


def test_load_env_fallback_missing_file_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BOARD_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    sync._load_env_fallback()  # must not raise

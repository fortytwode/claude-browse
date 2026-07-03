"""Tests for Firestore cross-laptop sync (board/sync.py)."""

from __future__ import annotations

import os

from claude_browse.board import store, sync


def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")


def test_firestore_client_is_constructed_once_and_cached(monkeypatch):
    """push() alone touches _firestore_client() up to 4 times per call
    (directly, plus via _fetch_all_session_docs/_get_stored_slack_ts/
    _store_slack_ts) -- must not construct a fresh Client each time."""
    monkeypatch.setattr(sync, "_firestore_client_cache", None)
    calls = []

    import google.cloud.firestore as firestore_module

    class _FakeClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(firestore_module, "Client", _FakeClient)

    c1 = sync._firestore_client()
    c2 = sync._firestore_client()

    assert c1 is c2
    assert len(calls) == 1


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


def test_push_calls_post_alert_when_pending_alert_set_and_clears_it(tmp_path, monkeypatch):
    """The fix for the real gap found in production: chat.update (what the
    board itself uses) doesn't re-notify Slack channel members, so a
    transition that warrants attention needs a genuinely NEW message too."""
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s-alert", host="air", cwd="/tmp/proj", state="needs-input",
                 name="blocked-thread", pending_alert="needs-input")
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "post_or_update_slack", lambda body: None)
    monkeypatch.setattr(sync, "_firestore_client", lambda: _FakeClient())

    calls = []
    monkeypatch.setattr(sync, "post_alert", lambda sid, kind, name, folder=None: calls.append((sid, kind, name)))

    sync.push("s-alert")

    assert calls == [("s-alert", "needs-input", "blocked-thread")]
    assert store.get("s-alert")["pending_alert"] is None  # cleared after posting


def test_push_does_not_call_post_alert_when_none_pending(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s-no-alert", host="air", cwd="/tmp/proj", state="idle", name="quiet-thread")
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "post_or_update_slack", lambda body: None)
    monkeypatch.setattr(sync, "_firestore_client", lambda: _FakeClient())

    calls = []
    monkeypatch.setattr(sync, "post_alert", lambda sid, kind, name, folder=None: calls.append((sid, kind, name)))

    sync.push("s-no-alert")

    assert calls == []


def test_push_clears_pending_alert_even_if_post_alert_raises(tmp_path, monkeypatch):
    """Best-effort: a failed alert attempt is not retried forever (which
    could pile up duplicate alerts if a later push succeeds)."""
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s-alert-fail", host="air", cwd="/tmp/proj", state="needs-input",
                 name="x", pending_alert="needs-input")
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "post_or_update_slack", lambda body: None)
    monkeypatch.setattr(sync, "_firestore_client", lambda: _FakeClient())

    def _raise(sid, kind, name, folder=None):
        raise RuntimeError("slack down")

    monkeypatch.setattr(sync, "post_alert", _raise)

    sync.push("s-alert-fail")  # must not raise

    assert store.get("s-alert-fail")["pending_alert"] is None


def test_post_alert_needs_input_message_body(monkeypatch):
    captured = {}
    monkeypatch.setattr(sync, "_slack_post_message", lambda body: captured.setdefault("body", body))

    sync.post_alert("abc-123", "needs-input", "my-thread")

    assert "needs your input" in captured["body"]
    assert "my-thread" in captured["body"]
    assert "claude --resume abc-123" in captured["body"]


def test_post_alert_done_message_body(monkeypatch):
    captured = {}
    monkeypatch.setattr(sync, "_slack_post_message", lambda body: captured.setdefault("body", body))

    sync.post_alert("abc-123", "done", "my-thread")

    assert "done" in captured["body"]
    assert "my-thread" in captured["body"]


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


def test_load_env_fallback_strips_inline_comment_on_unquoted_value(tmp_path, monkeypatch):
    """Regression: a trailing `# comment` on an unquoted value used to become
    part of the value itself (e.g. a token rotation note appended in-line)."""
    env_file = tmp_path / "test.env"
    env_file.write_text("SLACK_BOT_TOKEN=xoxb-abc123  # rotated 2026-07-03\n")
    monkeypatch.setenv("AGENT_BOARD_ENV_FILE", str(env_file))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    sync._load_env_fallback()

    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-abc123"


def test_load_env_fallback_quoted_value_with_hash_inside_is_preserved(tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text('SOME_KEY="value#with#hash"\n')
    monkeypatch.setenv("AGENT_BOARD_ENV_FILE", str(env_file))
    monkeypatch.delenv("SOME_KEY", raising=False)

    sync._load_env_fallback()

    assert os.environ["SOME_KEY"] == "value#with#hash"


def test_post_alert_includes_folder_tag_when_provided(monkeypatch):
    captured = {}
    monkeypatch.setattr(sync, "_slack_post_message", lambda body: captured.setdefault("body", body))

    sync.post_alert("abc-123", "needs-input", "my-thread", folder="claude-browse")

    assert "[claude-browse]" in captured["body"]
    assert "my-thread" in captured["body"]

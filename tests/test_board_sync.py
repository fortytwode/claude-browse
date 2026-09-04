"""Tests for Firestore cross-laptop sync (board/sync.py)."""

from __future__ import annotations

import os
import sys
import threading

import pytest

# board-sync is an optional extra (`pip install claude-browse[board-sync]`);
# these tests exercise its Firestore wiring and are meaningless without it.
pytest.importorskip(
    "google.cloud.firestore", reason="board-sync extra not installed"
)

from claude_browse.board import store, sync  # noqa: E402


def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(sync, "_PUBLICATION_LOCK_PATH", tmp_path / "publication.lock")


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

    def set(self, data, merge=False):
        # Mirrors Firestore semantics closely enough: merge=True overlays
        # onto whatever the doc already holds (so sweep-owned fields
        # survive a push), merge=False replaces the doc wholesale.
        if merge and self.doc_id in self.sink:
            merged = dict(self.sink[self.doc_id])
            merged.update(data)
            self.sink[self.doc_id] = merged
        else:
            self.sink[self.doc_id] = dict(data)
        self.sink.setdefault("__merge_flags__", []).append(merge)


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
    store.upsert("s1", host="air", cwd="/tmp/proj", state="idle",
                 name="foo", model_label="Codex")
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "post_or_update_slack", lambda body: None)  # U7 concern; isolated here

    fake_client = _FakeClient()
    monkeypatch.setattr(sync, "_firestore_client", lambda: fake_client)

    sync.push("s1")

    assert "air:s1" in fake_client.sink
    assert fake_client.sink["air:s1"]["state"] == "idle"
    assert fake_client.sink["air:s1"]["name"] == "foo"
    assert fake_client.sink["air:s1"]["model_label"] == "Codex"


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

    assert sync.push("s3") is False  # must not raise


def test_push_serializes_workers_and_latest_local_state_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "_PUBLICATION_LOCK_PATH", tmp_path / "publication.lock")
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "post_or_update_slack", lambda body: None)
    local_rows = {
        "s-race": {
            "session_id": "s-race", "host": "air", "cwd": "/tmp/proj",
            "state": "idle", "name": "thread", "provider": "claude",
        }
    }
    reads = []

    def _read_local(session_id):
        row = local_rows.get(session_id)
        reads.append((session_id, row["state"] if row else None))
        return dict(row) if row else None

    monkeypatch.setattr(store, "get", _read_local)
    monkeypatch.setattr(store, "active", lambda: [dict(row) for row in local_rows.values()])

    first_write_started = threading.Event()
    release_first_write = threading.Event()
    writes = []

    class BlockingDocRef(_FakeDocRef):
        def set(self, data, merge=False):
            writes.append(dict(data))
            if len(writes) == 1:
                first_write_started.set()
                assert release_first_write.wait(timeout=5)
            super().set(data, merge=merge)

    class BlockingCollection(_FakeCollection):
        def document(self, doc_id):
            return BlockingDocRef(self.sink, doc_id)

    class BlockingClient(_FakeClient):
        def collection(self, name):
            assert name == sync.COLLECTION
            return BlockingCollection(self.sink)

    client = BlockingClient()
    monkeypatch.setattr(sync, "_firestore_client", lambda: client)
    waiter_started_waiting = threading.Event()
    original_flock = __import__("fcntl").flock
    nonblocking_locks_acquired = 0

    def _observed_flock(lock_file, operation):
        nonlocal nonblocking_locks_acquired
        result = original_flock(lock_file, operation)
        if (
            operation & __import__("fcntl").LOCK_EX
            and operation & __import__("fcntl").LOCK_NB
        ):
            nonblocking_locks_acquired += 1
            if nonblocking_locks_acquired == 2:
                waiter_started_waiting.set()
        return result

    monkeypatch.setattr(__import__("fcntl"), "flock", _observed_flock)

    results = []
    first = threading.Thread(target=lambda: results.append(sync.push("s-race", coalesce=True)))
    first.start()
    assert first_write_started.wait(timeout=5)

    local_rows["s-race"].update(state="working", done_at=None, done_turn_s=None)
    second = threading.Thread(target=lambda: results.append(sync.push("s-race", coalesce=True)))
    second.start()
    assert waiter_started_waiting.wait(timeout=5)

    # A third hook worker collapses into the existing waiter immediately,
    # rather than joining an unbounded queue behind a stalled backend.
    local_rows["s-other"] = {
        "session_id": "s-other", "host": "air", "cwd": "/tmp/other",
        "state": "needs-input", "name": "other", "provider": "claude",
    }
    assert sync.push("s-other", coalesce=True) is False
    assert len(writes) == 1
    assert reads == [("s-race", "idle"), ("s-race", "idle")]
    release_first_write.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert results == [True, True]
    assert [doc["state"] for doc in writes] == ["idle", "working", "needs-input"]
    assert client.sink["air:s-race"]["state"] == "working"
    assert client.sink["air:s-other"]["state"] == "needs-input"


def test_uncontended_coalesced_push_publishes_only_requested_session(
    tmp_path, monkeypatch
):
    _fresh_store(tmp_path, monkeypatch)
    _quiet(monkeypatch)
    store.upsert(
        "s-requested", host="air", cwd="/tmp/requested", state="working", name="one"
    )
    store.upsert("s-other", host="air", cwd="/tmp/other", state="idle", name="two")
    client = _FakeClient()
    monkeypatch.setattr(sync, "_firestore_client", lambda: client)

    assert sync.push("s-requested", coalesce=True) is True

    assert "air:s-requested" in client.sink
    assert "air:s-other" not in client.sink


def test_push_calls_post_alert_when_pending_alert_set_and_clears_it(tmp_path, monkeypatch):
    """The fix for the real gap found in production: chat.update (what the
    board itself uses) doesn't re-notify Slack channel members, so a
    transition that warrants attention needs a genuinely NEW message too."""
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s-alert", host="air", cwd="/tmp/proj", state="needs-input",
                 name="blocked-thread", pending_alert="needs-input", model_label="Sonnet")
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "post_or_update_slack", lambda body: None)
    monkeypatch.setattr(sync, "_firestore_client", lambda: _FakeClient())

    calls = []
    monkeypatch.setattr(
        sync,
        "post_alert",
        lambda sid, kind, name, folder=None, model_label=None, provider="claude":
            calls.append((sid, kind, name, model_label)),
    )

    sync.push("s-alert")

    assert calls == [("s-alert", "needs-input", "blocked-thread", "Sonnet")]
    assert store.get("s-alert")["pending_alert"] is None  # cleared after posting


def test_push_does_not_call_post_alert_when_none_pending(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s-no-alert", host="air", cwd="/tmp/proj", state="idle", name="quiet-thread")
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "post_or_update_slack", lambda body: None)
    monkeypatch.setattr(sync, "_firestore_client", lambda: _FakeClient())

    calls = []
    monkeypatch.setattr(
        sync,
        "post_alert",
        lambda sid, kind, name, folder=None, model_label=None, provider="claude":
            calls.append((sid, kind, name, model_label)),
    )

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

    def _raise(sid, kind, name, folder=None, model_label=None, provider="claude"):
        raise RuntimeError("slack down")

    monkeypatch.setattr(sync, "post_alert", _raise)

    sync.push("s-alert-fail")  # must not raise

    assert store.get("s-alert-fail")["pending_alert"] is None


def test_post_alert_needs_input_message_body(monkeypatch):
    captured = {}
    monkeypatch.setattr(sync, "_slack_post_message", lambda body: captured.setdefault("body", body))

    sync.post_alert("abc-123", "needs-input", "my-thread", model_label="Codex")

    assert "needs your input" in captured["body"]
    assert "my-thread" in captured["body"]
    assert "Codex" in captured["body"]
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


# ---------------------------------------------------------------------------
# Unattended-completion fields + provider-aware push (2026-09 redesign)
# ---------------------------------------------------------------------------

def _quiet(monkeypatch):
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "post_or_update_slack", lambda body: None)


def test_push_merges_and_carries_provider_and_unattended_fields(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    _quiet(monkeypatch)
    store.upsert("s-u", host="air", cwd="/Users/me/team-operations", state="idle",
                 name="backfill toggl", provider="codex")
    store.mark_done("s-u", 900.0)

    fake_client = _FakeClient()
    # Pre-existing sweep-owned fields must survive the push.
    fake_client.sink["air:s-u"] = {"alert_count": 2, "last_alert_at": 123.0}
    monkeypatch.setattr(sync, "_firestore_client", lambda: fake_client)

    sync.push("s-u")

    doc = fake_client.sink["air:s-u"]
    assert doc["alert_count"] == 2 and doc["last_alert_at"] == 123.0  # merge=True kept them
    assert fake_client.sink["__merge_flags__"] == [True]
    assert doc["provider"] == "codex"
    assert doc["folder"] == "team-operations"
    assert doc["done_at"] is not None and doc["done_turn_s"] == 900.0
    assert doc["acked_at"] is None
    assert doc["resume_command"].startswith("codex resume s-u")


def test_push_skips_immediate_done_alert_by_default_but_clears_pending(tmp_path, monkeypatch):
    """The redesign: a 'done' no longer posts a fresh Slack message the
    instant the turn ends (it could not tell attended from unattended).
    The unattended sweep in team-operations owns that now."""
    _fresh_store(tmp_path, monkeypatch)
    _quiet(monkeypatch)
    monkeypatch.delenv("AGENT_BOARD_IMMEDIATE_DONE_ALERT", raising=False)
    store.upsert("s-d", host="air", cwd="/tmp/p", state="idle", name="t", pending_alert="done")
    monkeypatch.setattr(sync, "_firestore_client", lambda: _FakeClient())
    calls = []
    monkeypatch.setattr(sync, "post_alert", lambda *a, **k: calls.append((a, k)))

    sync.push("s-d")

    assert calls == []
    assert store.get("s-d")["pending_alert"] is None


def test_push_immediate_done_alert_opt_in(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    _quiet(monkeypatch)
    monkeypatch.setenv("AGENT_BOARD_IMMEDIATE_DONE_ALERT", "1")
    store.upsert("s-d2", host="air", cwd="/tmp/p", state="idle", name="t", pending_alert="done")
    store.mark_done("s-d2", 90)
    monkeypatch.setattr(sync, "_firestore_client", lambda: _FakeClient())
    calls = []
    monkeypatch.setattr(sync, "post_alert", lambda *a, **k: calls.append((a, k)))

    sync.push("s-d2")

    assert len(calls) == 1 and calls[0][0][1] == "done"


@pytest.mark.parametrize(
    ("state", "pending_alert"),
    [("working", "needs-input"), ("idle", "needs-input"), ("working", "done")],
)
def test_push_clears_superseded_alert_without_posting(
    tmp_path, monkeypatch, state, pending_alert
):
    _fresh_store(tmp_path, monkeypatch)
    _quiet(monkeypatch)
    monkeypatch.setenv("AGENT_BOARD_IMMEDIATE_DONE_ALERT", "1")
    store.upsert(
        "s-stale",
        host="air",
        cwd="/tmp/p",
        state=state,
        name="t",
        pending_alert=pending_alert,
    )
    monkeypatch.setattr(sync, "_firestore_client", lambda: _FakeClient())
    calls = []
    monkeypatch.setattr(sync, "post_alert", lambda *a, **k: calls.append((a, k)))

    sync.push("s-stale")

    assert calls == []
    assert store.get("s-stale")["pending_alert"] is None


def test_push_needs_input_alert_is_always_immediate(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    _quiet(monkeypatch)
    monkeypatch.delenv("AGENT_BOARD_IMMEDIATE_DONE_ALERT", raising=False)
    store.upsert("s-n", host="air", cwd="/tmp/p", state="needs-input", name="t",
                 pending_alert="needs-input", provider="codex")
    monkeypatch.setattr(sync, "_firestore_client", lambda: _FakeClient())
    calls = []
    monkeypatch.setattr(sync, "post_alert", lambda *a, **k: calls.append((a, k)))

    sync.push("s-n")

    assert len(calls) == 1
    assert calls[0][0][1] == "needs-input"
    assert calls[0][1]["provider"] == "codex"


def test_post_alert_uses_codex_resume_for_codex_rows(monkeypatch):
    captured = {}
    monkeypatch.setattr(sync, "_slack_post_message", lambda body: captured.setdefault("body", body))

    sync.post_alert("abc-123", "needs-input", "my-thread", provider="codex")

    assert "codex resume abc-123 --dangerously-bypass-approvals-and-sandbox" in captured["body"]
    assert "claude --resume" not in captured["body"]


def test_render_slack_body_leads_with_unattended_section(monkeypatch):
    import time as _t

    now = _t.time()
    docs = [
        _FakeDocFirestore({"session_id": "s1", "host": "air", "name": "thread-a", "state": "idle",
                           "cwd": "/tmp/a", "folder": "a", "heartbeat_at": now, "updated_at": now,
                           "provider": "codex", "done_at": now - 1500, "acked_at": None,
                           "resume_command": "codex resume s1 --dangerously-bypass-approvals-and-sandbox"}),
        _FakeDocFirestore({"session_id": "s2", "host": "air", "name": "thread-b", "state": "idle",
                           "cwd": "/tmp/b", "heartbeat_at": now, "updated_at": now,
                           "done_at": now - 1500, "acked_at": now}),  # acked -> not listed
        _FakeDocFirestore({"session_id": "s3", "host": "pro", "name": "thread-c", "state": "working",
                           "cwd": "/tmp/c", "heartbeat_at": now, "updated_at": now,
                           "done_at": now - 1500}),  # working -> not unattended
    ]
    monkeypatch.setattr(sync, "_fetch_all_session_docs", lambda: docs)

    body = sync.render_slack_body()
    first_section = body.split("*air*")[0]

    assert "finished, not picked up (1)" in first_section
    assert "thread-a" in first_section and "25m ago" in first_section
    assert "codex resume s1" in first_section
    assert "thread-b" not in first_section and "thread-c" not in first_section
    # Legacy docs without a provider still get a Claude resume command.
    assert "claude --resume s2 --dangerously-skip-permissions" in body


class _FakeDocFirestore:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


def test_ack_main_marks_local_and_pushes(tmp_path, monkeypatch, capsys):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("abc-1", host="air", cwd="/tmp/p", state="idle", name="deploy mission control")
    store.mark_done("abc-1", 600)
    calls = []

    def _push(session_id):
        row = store.get(session_id)
        calls.append((session_id, row["acked_at"]))
        return True

    monkeypatch.setattr(sync, "push", _push)

    assert sync.ack_main(["mission"]) == 0

    assert store.is_unattended(store.get("abc-1")) is False
    assert calls[0][0] == "abc-1" and calls[0][1] is not None
    assert "acked deploy mission control" in capsys.readouterr().out


def test_ack_full_push_refreshes_slack_with_rendered_body(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "_PUBLICATION_LOCK_PATH", tmp_path / "publication.lock")
    store.upsert("abc-2", host="air", cwd="/tmp/p", state="idle", name="review launch")
    store.mark_done("abc-2", 600)
    monkeypatch.setattr(sync, "naming", type("N", (), {"maybe_name": staticmethod(lambda sid: None)}))
    monkeypatch.setattr(sync, "_firestore_client", lambda: _FakeClient())
    monkeypatch.setattr(sync, "render_slack_body", lambda: "rendered board after ack")
    bodies = []
    monkeypatch.setattr(sync, "post_or_update_slack", bodies.append)

    assert sync.ack_main(["launch"]) == 0

    assert bodies == ["rendered board after ack"]


def test_ack_stays_locally_successful_when_publication_fails(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("abc-3", host="air", cwd="/tmp/p", state="idle", name="offline task")
    store.mark_done("abc-3", 600)
    monkeypatch.setattr(sync, "push", lambda session_id: False)

    assert sync.ack_main(["offline"]) == 0
    assert store.is_unattended(store.get("abc-3")) is False


def test_sync_push_accepts_session_id_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "push", lambda session_id: calls.append(session_id))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("not json and must not be read"))

    with pytest.raises(SystemExit) as exc_info:
        sync.main(["push", "from-argv"])

    assert exc_info.value.code == 0
    assert calls == ["from-argv"]


def test_sync_push_enables_coalescing_for_detached_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sync,
        "push",
        lambda session_id, *, coalesce=False: calls.append((session_id, coalesce)),
    )

    with pytest.raises(SystemExit) as exc_info:
        sync.main(["push", "--coalesce", "from-hook"])

    assert exc_info.value.code == 0
    assert calls == [("from-hook", True)]


def test_sync_push_retains_stdin_json_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "push", lambda session_id: calls.append(session_id))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"session_id":"from-stdin"}'))

    with pytest.raises(SystemExit) as exc_info:
        sync.main(["push"])

    assert exc_info.value.code == 0
    assert calls == ["from-stdin"]


def test_ack_main_refuses_ambiguous_match(tmp_path, monkeypatch, capsys):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("a1", host="air", cwd="/tmp/p", state="idle", name="deploy one")
    store.upsert("a2", host="air", cwd="/tmp/p", state="idle", name="deploy two")

    assert sync.ack_main(["deploy"]) == 1
    assert "2 sessions match" in capsys.readouterr().err
    assert store.get("a1")["acked_at"] is None


def test_ack_main_unknown_returns_1(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    assert sync.ack_main(["nope"]) == 1

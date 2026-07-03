"""Tests for the Slack #agent-status board renderer (board/sync.py, U7)."""

from __future__ import annotations

from claude_browse.board import sync


class _FakeDoc:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


def test_render_slack_body_groups_by_host_and_uses_display_state(monkeypatch):
    docs = [
        _FakeDoc({"session_id": "s1", "host": "air", "name": "thread-a", "state": "working",
                  "cwd": "/tmp/proj-a", "heartbeat_at": None, "updated_at": 1000.0}),
        _FakeDoc({"session_id": "s2", "host": "pro", "name": "thread-b", "state": "idle",
                  "cwd": "/tmp/proj-b", "heartbeat_at": None, "updated_at": 1000.0}),
    ]
    monkeypatch.setattr(sync, "_fetch_all_session_docs", lambda: docs)

    body = sync.render_slack_body()

    assert "air" in body
    assert "thread-a" in body
    assert "pro" in body
    assert "thread-b" in body


def test_render_slack_body_includes_yolo_resume_command_per_row(monkeypatch):
    """R6 requires a resume command per row; per user request it includes the
    provider's skip-permissions flag for one-paste re-entry."""
    docs = [
        _FakeDoc({"session_id": "abc-123", "host": "air", "name": "thread-a", "state": "idle",
                  "cwd": "/tmp/proj-a", "heartbeat_at": None, "updated_at": 1000.0}),
    ]
    monkeypatch.setattr(sync, "_fetch_all_session_docs", lambda: docs)

    body = sync.render_slack_body()

    assert "claude --resume abc-123 --dangerously-skip-permissions" in body


def test_post_alert_includes_yolo_resume_command(monkeypatch):
    captured = {}
    monkeypatch.setattr(sync, "_slack_post_message", lambda body: captured.setdefault("body", body))

    sync.post_alert("abc-123", "needs-input", "my-thread")

    assert "claude --resume abc-123 --dangerously-skip-permissions" in captured["body"]


def test_render_slack_body_empty_shows_all_clear(monkeypatch):
    monkeypatch.setattr(sync, "_fetch_all_session_docs", lambda: [])
    body = sync.render_slack_body()
    assert "no active sessions" in body.lower() or "all clear" in body.lower()


def test_post_or_update_slack_posts_new_message_when_no_stored_ts(monkeypatch):
    calls = {}

    monkeypatch.setattr(sync, "_get_stored_slack_ts", lambda: None)

    def _fake_post(body):
        calls["post"] = body
        return "1234.5678"

    def _fake_update(ts, body):
        calls["update"] = (ts, body)

    monkeypatch.setattr(sync, "_slack_post_message", _fake_post)
    monkeypatch.setattr(sync, "_slack_update_message", _fake_update)
    monkeypatch.setattr(sync, "_store_slack_ts", lambda ts: calls.setdefault("stored_ts", ts))

    sync.post_or_update_slack("hello board")

    assert calls["post"] == "hello board"
    assert "update" not in calls
    assert calls["stored_ts"] == "1234.5678"


def test_post_or_update_slack_updates_existing_message_when_ts_stored(monkeypatch):
    calls = {}
    monkeypatch.setattr(sync, "_get_stored_slack_ts", lambda: "9999.0001")
    monkeypatch.setattr(sync, "_slack_post_message", lambda body: calls.setdefault("post", body))
    monkeypatch.setattr(sync, "_slack_update_message", lambda ts, body: calls.setdefault("update", (ts, body)))

    sync.post_or_update_slack("updated board")

    assert calls["update"] == ("9999.0001", "updated board")
    assert "post" not in calls


def test_post_or_update_slack_never_raises_when_token_missing(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(sync, "_get_stored_slack_ts", lambda: (_ for _ in ()).throw(RuntimeError("no token")))

    sync.post_or_update_slack("board body")  # must not raise

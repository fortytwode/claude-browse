"""Tests for the statusline renderer (board/statusline.py)."""

from __future__ import annotations

import io
import json
import sys

from claude_browse.board import statusline, store


def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")


def test_render_line_shows_name_and_state(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("sess-a", host="air", cwd="/tmp/proj", state="working", name="foo")

    line = statusline.render_line("sess-a", "/tmp/proj")

    assert "foo" in line
    assert "working" in line


def test_render_line_needs_input_uses_warning_color(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("sess-b", host="air", cwd="/tmp/proj", state="needs-input", name="blocked")

    line = statusline.render_line("sess-b", "/tmp/proj")

    assert statusline.COLORS["needs-input"] in line


def test_render_line_no_record_falls_back_to_cwd_basename(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    line = statusline.render_line("unknown-session", "/tmp/my-project")

    assert "my-project" in line


def test_main_prints_line_and_bumps_heartbeat(tmp_path, monkeypatch, capsys):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("sess-c", host="air", cwd="/tmp/proj", state="idle", name="bar")
    before = store.get("sess-c")["heartbeat_at"]

    payload = json.dumps({"session_id": "sess-c", "cwd": "/tmp/proj"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    statusline.main()

    captured = capsys.readouterr()
    assert "bar" in captured.out
    after = store.get("sess-c")["heartbeat_at"]
    assert after is not None
    assert before is None or after >= before


def test_main_never_raises_on_malformed_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    statusline.main()  # must not raise
    captured = capsys.readouterr()
    assert captured.out is not None

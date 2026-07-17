"""Tests for the jobs/board CLI renderer (board/cli.py)."""

from __future__ import annotations

import time

from claude_browse.board import cli, store


def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")


def test_render_board_lists_all_rows_sorted_needs_input_first(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s1", host="air", cwd="/tmp/proj-a", state="idle", name="thread-a")
    store.upsert("s2", host="air", cwd="/tmp/proj-b", state="working", name="thread-b")
    store.upsert("s3", host="air", cwd="/tmp/proj-c", state="needs-input", name="thread-c")

    output = cli.render_board()
    lines = [line for line in output.splitlines() if line.strip()]

    assert len(lines) == 3
    assert "thread-c" in lines[0]  # needs-input sorts first
    assert all("claude --resume" in line for line in lines)


def test_render_board_empty_store_prints_friendly_message(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    output = cli.render_board()
    assert "no active sessions" in output.lower()


def test_render_board_null_name_falls_back_to_cwd_basename(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s4", host="air", cwd="/tmp/my-fallback-project", state="idle")

    output = cli.render_board()
    assert "my-fallback-project" in output


def test_render_board_shows_gone_for_stale_working_session(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s5", host="air", cwd="/tmp/zombie", state="working", name="zombie-thread")
    store._raw_set_updated_at("s5", time.time() - 700)

    output = cli.render_board()
    assert "gone" in output
    assert "working" not in output  # only row present; must render as gone, not the stale stored state


def test_main_runs_without_raising(tmp_path, monkeypatch, capsys):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s6", host="air", cwd="/tmp/proj", state="idle", name="foo")
    cli.main()
    captured = capsys.readouterr()
    assert "foo" in captured.out

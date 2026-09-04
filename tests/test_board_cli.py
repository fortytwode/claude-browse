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


# ---------------------------------------------------------------------------
# Per-row provider + unattended section (2026-09 redesign)
# ---------------------------------------------------------------------------

def test_render_board_uses_each_rows_provider_for_resume(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("c-1", host="air", cwd="/tmp/a", state="idle", name="codex thread", provider="codex")
    store.upsert("k-1", host="air", cwd="/tmp/b", state="idle", name="claude thread")

    output = cli.render_board()
    codex_line = next(line for line in output.splitlines() if "codex thread" in line)
    claude_line = next(line for line in output.splitlines() if "claude thread" in line)

    assert "codex resume c-1 --dangerously-bypass-approvals-and-sandbox" in codex_line
    assert "claude --resume k-1 --dangerously-skip-permissions" in claude_line


def test_render_board_leads_with_unattended_section(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("u-1", host="air", cwd="/tmp/a", state="idle", name="backfill done")
    store.mark_done("u-1", 900)
    store._raw_set_updated_at("u-1", time.time())
    store.upsert("u-2", host="air", cwd="/tmp/b", state="working", name="still running")

    output = cli.render_board()
    lines = output.splitlines()

    assert lines[0].startswith("⏳ finished, not picked up (1)")
    assert "backfill done" in lines[1] and "ago" in lines[1]
    assert "still running" not in "\n".join(lines[:3])


def test_render_board_no_unattended_section_when_none(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("n-1", host="air", cwd="/tmp/a", state="idle", name="plain")
    output = cli.render_board()
    assert "not picked up" not in output

"""Automatic capture of verified terminals that predate board hooks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_browse.board import discovery, presence, store, work_items


def _completed(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


@pytest.fixture(autouse=True)
def _isolated_board(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    monkeypatch.setattr(presence, "_hostname", lambda: "this-mac")
    presence._clear_cache()


def _rollout(root: Path, sid: str, *, cwd: str, source: str = "cli",
             originator: str = "codex-tui", thread_source: str = "user") -> Path:
    path = root / "2026" / "09" / "05" / f"rollout-2026-09-05T01-02-03-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": sid, "cwd": cwd, "source": source, "originator": originator,
        "thread_source": thread_source,
    }}) + "\n")
    return path


def _live_codex(root: Path, monkeypatch, sid: str = "a-b-c-d-e", **metadata) -> Path:
    path = _rollout(root, sid, cwd=metadata.pop("cwd", "/repo/live"), **metadata)
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: root)

    def run(args, timeout):
        if args[0] == "ps":
            return _completed("9 ttys001 codex\n")
        return _completed(f"p9\nf20\nau\nn{path}\n")

    monkeypatch.setattr(presence, "_run", run)
    return path


def test_exact_user_root_is_live_and_captured_without_fts(tmp_path, monkeypatch):
    path = _live_codex(tmp_path / "sessions", monkeypatch)

    assert presence.live_sessions() == [{
        "session_id": "a-b-c-d-e", "provider": "codex", "cwd": "/repo/live",
        "path": str(path),
    }]
    assert discovery.capture_live_sessions() == 1

    runtime = store.get("a-b-c-d-e")
    assert runtime["host"] == "this-mac"
    assert runtime["provider"] == "codex"
    assert runtime["cwd"] == "/repo/live"
    assert runtime["transcript_path"] == str(path)
    assert work_items.get_for_session("a-b-c-d-e")["status"] == "active"


@pytest.mark.parametrize("source,originator,thread_source", [
    ("cli", "codex-tui", "subagent"),
    ("cli", "other", "user"),
    ("api", "codex-tui", "user"),
])
def test_non_root_or_unknown_codex_evidence_never_enrolls(
    tmp_path, monkeypatch, source, originator, thread_source
):
    _live_codex(tmp_path / "sessions", monkeypatch, source=source,
                originator=originator, thread_source=thread_source)

    assert presence.live_sessions() == []
    assert discovery.capture_live_sessions() == 0
    assert store.get("a-b-c-d-e") is None


def test_capture_is_idempotent_and_does_not_overwrite_a_racing_hook(tmp_path, monkeypatch):
    _live_codex(tmp_path / "sessions", monkeypatch)
    store.upsert("a-b-c-d-e", host="hook-host", cwd="/hook/cwd", provider="claude",
                 state="working", name="Hook-owned title")

    assert discovery.capture_live_sessions() == 1
    assert discovery.capture_live_sessions() == 0

    runtime = store.get("a-b-c-d-e")
    assert runtime["host"] == "hook-host"
    assert runtime["cwd"] == "/hook/cwd"
    assert runtime["provider"] == "claude"
    assert runtime["state"] == "working"
    assert runtime["name"] == "Hook-owned title"
    assert len(work_items.list_items(include_done=True)) == 1


def test_linked_archived_task_is_neither_duplicated_nor_reactivated(tmp_path, monkeypatch):
    _live_codex(tmp_path / "sessions", monkeypatch)
    store.upsert("current", cwd="/repo/current", provider="codex", name="Archived manually")
    task = work_items.ensure_for_session(store.get("current"))
    work_items.mutate(task["task_id"], status="archived")
    with store.get_conn() as conn:
        conn.execute(
            """INSERT INTO task_session_links (session_id, task_id, provider, cwd, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("a-b-c-d-e", task["task_id"], "codex", "/repo/live", 1.0),
        )

    assert discovery.capture_live_sessions() == 1
    unchanged = work_items.get(task["task_id"])
    assert unchanged["task_id"] == task["task_id"]
    assert unchanged["session_id"] == "current"
    assert unchanged["status"] == "archived"
    assert unchanged["title"] == "Archived manually"

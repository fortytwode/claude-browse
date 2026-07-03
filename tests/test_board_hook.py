"""Tests for the hook dispatcher (board/hook.py) and its notification gating."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_browse.board import hook, store

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_SCRIPT = REPO_ROOT / "agent-board"


def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")


def test_stop_after_long_run_sets_idle_and_notifies(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: calls.append((title, msg)))

    store.upsert("s1", host="air", cwd="/tmp/proj", state="working",
                 working_since=time.time() - 90, name="my-thread")
    hook.dispatch({"hook_event_name": "Stop", "session_id": "s1", "cwd": "/tmp/proj"})

    assert store.get("s1")["state"] == "idle"
    assert len(calls) == 1
    assert "my-thread" in calls[0][1]


def test_stop_after_short_run_sets_idle_no_notify(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: calls.append((title, msg)))

    store.upsert("s2", host="air", cwd="/tmp/proj", state="working",
                 working_since=time.time() - 8, name="my-thread")
    hook.dispatch({"hook_event_name": "Stop", "session_id": "s2", "cwd": "/tmp/proj"})

    assert store.get("s2")["state"] == "idle"
    assert calls == []


@pytest.mark.parametrize(
    "notification_type", ["permission_prompt", "agent_needs_input", "elicitation_dialog"]
)
def test_notification_needs_input_types_set_state_and_notify(tmp_path, monkeypatch, notification_type):
    _fresh_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: calls.append((title, msg)))

    store.upsert("s3", host="air", cwd="/tmp/proj", state="working", name="blocked-thread")
    hook.dispatch({
        "hook_event_name": "Notification",
        "session_id": "s3",
        "cwd": "/tmp/proj",
        "notification_type": notification_type,
    })

    assert store.get("s3")["state"] == "needs-input"
    assert len(calls) == 1


@pytest.mark.parametrize("notification_type", ["idle_prompt", "auth_success"])
def test_notification_ignored_types_do_not_change_state_or_notify(tmp_path, monkeypatch, notification_type):
    _fresh_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: calls.append((title, msg)))

    store.upsert("s4", host="air", cwd="/tmp/proj", state="working", name="quiet-thread")
    hook.dispatch({
        "hook_event_name": "Notification",
        "session_id": "s4",
        "cwd": "/tmp/proj",
        "notification_type": notification_type,
    })

    assert store.get("s4")["state"] == "working"
    assert calls == []


def test_session_start_creates_row_with_placeholder_name_no_crash(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "s5", "cwd": "/tmp/my-project"})

    row = store.get("s5")
    assert row is not None
    assert row["state"] == "idle"
    assert row["name"]


def test_user_prompt_submit_sets_working_and_captures_provisional_name(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "s6", "cwd": "/tmp/proj"})
    hook.dispatch({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s6",
        "cwd": "/tmp/proj",
        "prompt": "fix the generator priyansha feedback issue please",
    })

    row = store.get("s6")
    assert row["state"] == "working"
    assert row["name_source"] == "provisional"
    assert "fix" in row["name"]


def test_user_prompt_submit_does_not_overwrite_haiku_name(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)

    store.upsert("s7", host="air", cwd="/tmp/proj", state="idle",
                 name="haiku-derived-name", name_source="haiku")
    hook.dispatch({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s7",
        "cwd": "/tmp/proj",
        "prompt": "a totally different followup prompt",
    })

    row = store.get("s7")
    assert row["name"] == "haiku-derived-name"
    assert row["name_source"] == "haiku"


def test_session_end_sets_ended(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s8", host="air", cwd="/tmp/proj", state="working", name="x")
    hook.dispatch({"hook_event_name": "SessionEnd", "session_id": "s8", "cwd": "/tmp/proj"})
    assert store.get("s8")["state"] == "ended"


def test_dispatch_ignores_missing_session_id(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    hook.dispatch({"hook_event_name": "Stop", "cwd": "/tmp/proj"})  # no session_id
    assert store.active() == []


def test_main_exits_zero_on_malformed_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("not valid json {{{"))
    with pytest.raises(SystemExit) as exc_info:
        hook.main()
    assert exc_info.value.code == 0


def test_main_exits_zero_even_if_dispatch_raises(monkeypatch):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(
        json.dumps({"hook_event_name": "Stop", "session_id": "s9"})
    ))

    def _boom(payload):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(hook, "dispatch", _boom)
    with pytest.raises(SystemExit) as exc_info:
        hook.main()
    assert exc_info.value.code == 0


def test_entry_script_end_to_end_working_then_idle_transition(tmp_path, monkeypatch):
    """Integration: pipe real JSON through the actual `agent-board hook` entry script."""
    env = {**__import__("os").environ, "AGENT_BOARD_DB_PATH": str(tmp_path / "state.db")}

    start_payload = json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s-e2e",
        "cwd": "/tmp/e2e",
        "prompt": "integration test prompt",
    })
    result = subprocess.run(
        [sys.executable, str(ENTRY_SCRIPT), "hook"],
        input=start_payload, capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode == 0

    stop_payload = json.dumps({
        "hook_event_name": "Stop", "session_id": "s-e2e", "cwd": "/tmp/e2e",
    })
    result = subprocess.run(
        [sys.executable, str(ENTRY_SCRIPT), "hook"],
        input=stop_payload, capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode == 0

    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    row = store.get("s-e2e")
    assert row is not None
    assert row["state"] == "idle"

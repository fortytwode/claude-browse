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
    assert store.get("s1")["pending_alert"] == "done"  # so sync.py posts a fresh Slack alert too


def test_stop_refreshes_heartbeat(tmp_path, monkeypatch):
    """Stop fires reliably on every turn per the verified hook contract,
    making it a more dependable liveness signal than statusline's refresh
    cadence during long tool-heavy sequences (observed live: this exact
    build's own session showed 'gone' on the real board mid-session, purely
    from statusline gaps, even though it was actively being worked on)."""
    _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: None)

    store.upsert("s-hb", host="air", cwd="/tmp/proj", state="working",
                 working_since=time.time() - 5, heartbeat_at=time.time() - 700)
    hook.dispatch({"hook_event_name": "Stop", "session_id": "s-hb", "cwd": "/tmp/proj"})

    row = store.get("s-hb")
    assert row["heartbeat_at"] is not None
    assert time.time() - row["heartbeat_at"] < 5
    assert store.display_state(row) == "idle"  # not 'gone', despite the stale heartbeat_at set above


def test_stop_after_short_run_sets_idle_no_notify(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: calls.append((title, msg)))

    store.upsert("s2", host="air", cwd="/tmp/proj", state="working",
                 working_since=time.time() - 8, name="my-thread")
    hook.dispatch({"hook_event_name": "Stop", "session_id": "s2", "cwd": "/tmp/proj"})

    assert store.get("s2")["state"] == "idle"
    assert calls == []
    assert store.get("s2")["pending_alert"] is None  # short run -- no alert warranted


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


def test_notification_backfills_host_on_a_pre_existing_row_with_no_host(tmp_path, monkeypatch):
    """Regression: a session whose SessionStart never fired under these hooks
    (e.g. it was already running when hooks were wired) had its row created
    with host=None by whichever event touched it first. Every state-changing
    event must backfill host, not just SessionStart/UserPromptSubmit."""
    _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: None)
    monkeypatch.setattr(hook, "_hostname", lambda: "real-hostname")

    # Row created by a bare set_state call with no host, mirroring how the
    # very first Notification/Stop for a pre-existing session used to behave.
    store.set_state("s10", "working")
    assert store.get("s10")["host"] is None

    hook.dispatch({
        "hook_event_name": "Notification",
        "session_id": "s10",
        "cwd": "/tmp/proj",
        "notification_type": "permission_prompt",
    })

    assert store.get("s10")["host"] == "real-hostname"


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


def test_notification_unrecognized_future_type_fails_safe_to_needs_input(tmp_path, monkeypatch):
    """The core forward-compatibility fix: _IGNORED_NOTIFICATION_TYPES is a
    denylist, not an allowlist. A notification_type this code has never seen
    (e.g. one a future Claude Code version introduces) must default to
    needs-input, not silently no-op -- an allowlist would miss it."""
    _fresh_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: calls.append((title, msg)))

    store.upsert("s11", host="air", cwd="/tmp/proj", state="working", name="future-thread")
    hook.dispatch({
        "hook_event_name": "Notification",
        "session_id": "s11",
        "cwd": "/tmp/proj",
        "notification_type": "some_brand_new_type_from_a_future_claude_code_version",
    })

    assert store.get("s11")["state"] == "needs-input"
    assert len(calls) == 1


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


def test_notify_body_includes_folder_tag():
    assert hook._notify_body("Continue CodeX session context", "/Users/me/team-operations") == \
        "Continue CodeX session context  [team-operations]"
    assert hook._notify_body("my-thread", None) == "my-thread"
    # placeholder case: name IS the folder -- no redundant tag
    assert hook._notify_body("claude-browse", "/Users/me/claude-browse") == "claude-browse"


def test_stop_notification_banner_carries_folder(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: calls.append((title, msg)))

    store.upsert("s-folder", host="air", cwd="/Users/me/claude-browse", state="working",
                 working_since=time.time() - 90, name="agent board build")
    hook.dispatch({"hook_event_name": "Stop", "session_id": "s-folder",
                   "cwd": "/Users/me/claude-browse"})

    assert calls == [("✅ done", "agent board build  [claude-browse]")]

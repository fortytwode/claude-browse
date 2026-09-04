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


def test_notify_title_includes_folder_tag():
    assert hook._notify_title("needs input", "/Users/me/team-operations", "Codex") == \
        "[team-operations] Codex needs input"
    assert hook._notify_title("done", "/Users/me/team-operations", "Sonnet") == \
        "[team-operations] Sonnet done"
    assert hook._notify_title("needs input", None, "Codex") == "Codex needs input"
    # Body carries the name ONLY -- the folder tag lives exclusively in the
    # title. Both surfaces carrying it shipped a double-folder banner,
    # caught by the user from a real screenshot.
    assert hook._notify_body("Continue CodeX session context", "/Users/me/team-operations") == \
        "Continue CodeX session context"
    assert hook._notify_body("team-operations", "/Users/me/team-operations") == "team-operations"


def test_model_label_compacts_common_model_ids():
    assert hook._compact_model_label("gpt-5-codex") == "Codex"
    assert hook._compact_model_label("claude-opus-4-8") == "Opus"
    assert hook._compact_model_label("claude-sonnet-4-5") == "Sonnet"
    assert hook._compact_model_label("fable") == "Fable"


def test_model_label_reads_latest_assistant_model_from_transcript(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"model": "claude-haiku-4-5"}}),
            "not-json",
            json.dumps({"message": {"model": "claude-opus-4-8"}}),
        ])
        + "\n"
    )

    assert hook._model_label({"transcript_path": str(transcript)}, None) == "Opus"


def test_stop_notification_banner_carries_folder(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: calls.append((title, msg)))

    store.upsert("s-folder", host="air", cwd="/Users/me/claude-browse", state="working",
                 working_since=time.time() - 90, name="agent board build")
    hook.dispatch({"hook_event_name": "Stop", "session_id": "s-folder",
                   "cwd": "/Users/me/claude-browse",
                   "model": {"display_name": "Codex"}})

    assert calls == [("[claude-browse] Codex done", "agent board build")]


def test_stop_notification_uses_stored_model_when_payload_omits_it(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: calls.append((title, msg)))

    store.upsert("s-stored-model", host="air", cwd="/Users/me/claude-browse",
                 state="working", working_since=time.time() - 90,
                 name="agent board build", model_label="Sonnet")
    hook.dispatch({"hook_event_name": "Stop", "session_id": "s-stored-model",
                   "cwd": "/Users/me/claude-browse"})

    assert calls == [("[claude-browse] Sonnet done", "agent board build")]


def test_needs_input_notification_title_carries_folder(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: calls.append((title, msg)))

    store.upsert("s-folder-input", host="air", cwd="/Users/me/claude-browse",
                 state="working", name="agent board build")
    hook.dispatch({
        "hook_event_name": "Notification",
        "session_id": "s-folder-input",
        "cwd": "/Users/me/claude-browse",
        "notification_type": "permission_prompt",
        "model": "claude-sonnet-4-5",
    })

    assert calls == [("[claude-browse] Sonnet needs input", "agent board build")]


# ---------------------------------------------------------------------------
# Provider flag, Codex PermissionRequest, unattended done_at (2026-09)
# ---------------------------------------------------------------------------

def test_parse_provider_defaults_to_claude_and_accepts_both_forms():
    assert hook._parse_provider([]) == "claude"
    assert hook._parse_provider(["hook"]) == "claude"
    assert hook._parse_provider(["hook", "--provider", "codex"]) == "codex"
    assert hook._parse_provider(["hook", "--provider=Codex"]) == "codex"
    assert hook._parse_provider(["hook", "--provider"]) == "claude"  # dangling flag never breaks


def test_session_start_records_provider_and_later_events_keep_it(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: None)

    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "c1", "cwd": "/tmp/proj",
                   "model": "gpt-5-codex"}, provider="codex")
    assert store.get("c1")["provider"] == "codex"
    assert store.get("c1")["model_label"] == "Codex"

    hook.dispatch({"hook_event_name": "UserPromptSubmit", "session_id": "c1", "cwd": "/tmp/proj",
                   "prompt": "backfill toggl hours for august"}, provider="codex")
    row = store.get("c1")
    assert row["provider"] == "codex" and row["state"] == "working"
    assert row["name"] == "backfill toggl hours for august"


def test_codex_permission_request_maps_to_needs_input(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: calls.append((title, msg)))
    store.upsert("c2", host="air", cwd="/Users/me/team-operations", state="working",
                 name="deploy sweep", provider="codex", model_label="Codex")

    hook.dispatch({"hook_event_name": "PermissionRequest", "session_id": "c2",
                   "cwd": "/Users/me/team-operations", "tool_name": "shell",
                   "tool_input": {"command": "gcloud run jobs deploy"}}, provider="codex")

    row = store.get("c2")
    assert row["state"] == "needs-input"
    assert row["pending_alert"] == "needs-input"
    assert calls == [("[team-operations] Codex needs input", "deploy sweep")]


def test_every_completed_turn_marks_done_regardless_of_length(tmp_path, monkeypatch):
    """Duration is not the signal; whether you came back is. A 10-second
    turn that ends with a question is still a thread waiting on you."""
    _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: None)
    monkeypatch.delenv("AGENT_BOARD_UNATTENDED_MIN_TURN_S", raising=False)

    store.upsert("long", host="air", cwd="/tmp/p", state="working", working_since=time.time() - 400)
    hook.dispatch({"hook_event_name": "Stop", "session_id": "long", "cwd": "/tmp/p"})
    row = store.get("long")
    assert store.is_unattended(row) is True
    assert 399 < row["done_turn_s"] < 405
    assert row["pending_alert"] == "done"  # banner path unchanged (>60s)

    store.upsert("short", host="air", cwd="/tmp/p", state="working", working_since=time.time() - 10)
    hook.dispatch({"hook_event_name": "Stop", "session_id": "short", "cwd": "/tmp/p"})
    row = store.get("short")
    assert store.is_unattended(row) is True
    assert row["pending_alert"] is None  # <60s: no banner, but still waiting on you

    # No prompt ever sent (no working_since): nothing completed, nothing waits.
    store.upsert("never", host="air", cwd="/tmp/p", state="idle")
    hook.dispatch({"hook_event_name": "Stop", "session_id": "never", "cwd": "/tmp/p"})
    assert store.get("never")["done_at"] is None


def test_unattended_threshold_is_env_tunable(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: None)
    monkeypatch.setenv("AGENT_BOARD_UNATTENDED_MIN_TURN_S", "30")

    store.upsert("t", host="air", cwd="/tmp/p", state="working", working_since=time.time() - 45)
    hook.dispatch({"hook_event_name": "Stop", "session_id": "t", "cwd": "/tmp/p"})
    assert store.get("t")["done_at"] is not None

    store.upsert("u", host="air", cwd="/tmp/p", state="working", working_since=time.time() - 10)
    hook.dispatch({"hook_event_name": "Stop", "session_id": "u", "cwd": "/tmp/p"})
    assert store.get("u")["done_at"] is None  # a raised floor filters short turns

    monkeypatch.setenv("AGENT_BOARD_UNATTENDED_MIN_TURN_S", "not-a-number")
    assert hook._unattended_min_turn_s() == 0.0


def test_new_prompt_is_the_implicit_ack(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: None)
    store.upsert("p", host="air", cwd="/tmp/p", state="working", working_since=time.time() - 900)
    hook.dispatch({"hook_event_name": "Stop", "session_id": "p", "cwd": "/tmp/p"})
    assert store.is_unattended(store.get("p")) is True

    hook.dispatch({"hook_event_name": "UserPromptSubmit", "session_id": "p", "cwd": "/tmp/p",
                   "prompt": "thanks, now run it for september"})
    row = store.get("p")
    assert row["done_at"] is None and row["state"] == "working"
    assert store.is_unattended(row) is False


@pytest.mark.parametrize("reason", ["prompt_input_exit", "clear", "logout", "exit", "other", "", None])
def test_session_end_always_preserves_done_at(tmp_path, monkeypatch, reason):
    """However a session ends (/exit, /clear, a killed window), a finished
    turn you have not come back to stays on the list: you can always resume
    the thread, and the board's job is to keep it visible until you do."""
    _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setattr(hook.notify, "notify", lambda title, msg: None)
    store.upsert("z", host="air", cwd="/tmp/p", state="working", working_since=time.time() - 10)
    hook.dispatch({"hook_event_name": "Stop", "session_id": "z", "cwd": "/tmp/p"})
    payload = {"hook_event_name": "SessionEnd", "session_id": "z", "cwd": "/tmp/p"}
    if reason is not None:
        payload["reason"] = reason
    hook.dispatch(payload)
    row = store.get("z")
    assert row["state"] == "ended" and row["done_at"] is not None
    assert store.is_unattended(row) is True


def test_entry_script_accepts_provider_flag_end_to_end(tmp_path, monkeypatch):
    """The real shim, the real argv, a Codex-shaped payload on stdin."""
    db = tmp_path / "state.db"
    env = {**dict(__import__("os").environ), "AGENT_BOARD_DB_PATH": str(db)}
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "codex-e2e",
                          "cwd": "/tmp/proj", "model": "gpt-5-codex", "transcript_path": "/nope",
                          "permission_mode": "default", "source": "startup"})
    result = subprocess.run(
        [sys.executable, str(ENTRY_SCRIPT), "hook", "--provider", "codex"],
        input=payload, capture_output=True, text=True, env=env, timeout=20,
    )
    assert result.returncode == 0, result.stderr

    monkeypatch.setattr(store, "_DB_PATH", db)
    monkeypatch.setattr(store, "_conn_cache", None)
    row = store.get("codex-e2e")
    assert row["provider"] == "codex" and row["model_label"] == "Codex"

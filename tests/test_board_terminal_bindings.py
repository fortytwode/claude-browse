"""Selection hooks must prove a local provider process and a user root."""

import json
from types import SimpleNamespace

import pytest

from claude_browse.board import hook, presence, store, terminal_bindings

START = "Tue Nov  3 11:01:46 2026"
OLD = "a-b-c-d-e"
NEW = "f-g-h-i-j"


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    roots = {provider: tmp_path / provider for provider in ("codex", "claude")}
    for root in roots.values():
        root.mkdir()
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: roots["codex"])
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: roots["claude"])
    monkeypatch.setattr(terminal_bindings.os, "getppid", lambda: 50)
    monkeypatch.setattr(presence, "_hostname", lambda: "test-host")
    state = {"provider": "codex", "tty": "ttys001", "start": START, "paths": []}

    def run(args, timeout):
        if args[:2] == ["ps", "-p"]:
            pid = int(args[2])
            ancestry = "ppid=" in " ".join(args)
            if pid == 50:
                out = f"50 9 ?? sh {START}\n" if ancestry else ""
            else:
                parent = " 1" if ancestry else ""
                out = f"9{parent} {state['tty']} {state['provider']} {state['start']}\n"
        elif args[0] == "ps":
            out = f"9 {state['tty']} {state['provider']}\n"
        else:
            out = "p9\n" + "".join(f"f{20+i}\nau\nn{p}\n" for i, p in enumerate(state["paths"]))
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(presence, "_run", run)
    presence._clear_cache()

    def payload(sid=OLD, **metadata):
        if state["provider"] == "codex":
            path = roots["codex"] / f"rollout-2026-11-03T11-01-46-{sid}.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "payload": {
                "id": sid, "thread_source": "user", "source": "cli",
                "originator": "codex-tui", "cwd": "/work", **metadata,
            }}) + "\n")
            state["paths"].append(path)
        else:
            path = roots["claude"] / "9.json"
            path.write_text(json.dumps({"sessionId": sid, "pid": 9,
                                        "procStart": START, "cwd": "/work", **metadata}))
        return {"hook_event_name": "SessionStart", "session_id": sid,
                "transcript_path": str(path), "cwd": "/work"}

    yield state, payload
    presence._clear_cache()


def test_selected_hook_updates_binding_but_cannot_choose_between_loaded_writers(native):
    state, payload = native
    first, second = payload(), payload(NEW)
    assert terminal_bindings.record_hook(first, "codex")
    assert terminal_bindings.read_verified("codex", presence.time.monotonic() + 3)[9].session_id == OLD
    assert presence.verified_terminal_tty(OLD, "codex")[0] is None
    second["hook_event_name"] = "UserPromptSubmit"
    assert terminal_bindings.record_hook(second, "codex")
    assert terminal_bindings.read_verified("codex", presence.time.monotonic() + 3)[9].session_id == NEW
    assert presence.verified_terminal_tty(NEW, "codex")[0] is None
    assert presence.verified_terminal_tty(OLD, "codex")[0] is None
    assert presence.live_sessions() == []


def test_multiple_user_writers_without_selection_are_unproven(native):
    _, payload = native
    payload()
    payload(NEW)
    assert presence.verified_terminal_tty(OLD, "codex")[0] is None
    assert presence.live_sessions() == []


@pytest.mark.parametrize("changed", [{"start": "Wed Nov  4 11:01:46 2026"},
                                     {"tty": "ttys002"}, {"tty": "??"},
                                     {"provider": "python"}])
def test_binding_rechecks_pid_start_tty_and_command(native, changed):
    state, payload = native
    assert terminal_bindings.record_hook(payload(), "codex")
    state.update(changed)
    assert terminal_bindings.read_verified("codex", presence.time.monotonic() + 3) == {}


@pytest.mark.parametrize("metadata", [
    {"thread_source": "subagent"}, {"thread_source": None},
    {"source": {"subagent": {"thread_spawn": {}}}}, {"id": NEW},
])
def test_subagent_and_mismatching_metadata_cannot_bind(native, metadata):
    _, payload = native
    assert not terminal_bindings.record_hook(payload(**metadata), "codex")


@pytest.mark.parametrize("event", ["Stop", "Interrupt", "PermissionRequest", "SessionEnd", "SubagentStart"])
def test_background_events_cannot_change_selection(native, event):
    _, payload = native
    assert terminal_bindings.record_hook(payload(), "codex")
    other = payload(NEW)
    other["hook_event_name"] = event
    assert not terminal_bindings.record_hook(other, "codex")
    assert terminal_bindings.read_verified("codex", presence.time.monotonic() + 3)[9].session_id == OLD


def test_prompt_agent_context_cannot_replace_parent_binding(native):
    _, payload = native
    assert terminal_bindings.record_hook(payload(), "codex")
    child = payload(NEW)
    child.update(hook_event_name="UserPromptSubmit", agent_id="child")
    assert not terminal_bindings.record_hook(child, "codex")
    assert terminal_bindings.read_verified("codex", presence.time.monotonic() + 3)[9].session_id == OLD


def test_resumed_vscode_user_root_gets_fresh_terminal_binding(native):
    _, payload = native
    p = payload(source="vscode", originator="codex_vscode", forked_from_id=NEW)
    assert terminal_bindings.record_hook(p, "codex")
    assert presence.live_sessions() == [{"session_id": OLD, "provider": "codex",
                                        "cwd": "/work", "path": p["transcript_path"]}]


def test_daemon_hook_never_binds_even_with_valid_user_rollout(native):
    state, payload = native
    state["tty"] = "??"
    assert not terminal_bindings.record_hook(payload(), "codex")


def test_claude_binding_requires_native_pid_session_and_start(native):
    state, payload = native
    state["provider"] = "claude"
    p = payload()
    assert terminal_bindings.record_hook(p, "claude")
    assert terminal_bindings.read_verified("claude", presence.time.monotonic() + 3)[9].session_id == OLD
    p["session_id"] = NEW
    assert not terminal_bindings.record_hook(p, "claude")


def test_dispatch_records_selection_without_changing_existing_session(native):
    _, payload = native
    p = payload()
    store.upsert(OLD, name="Keep this name", name_source="manual", provider="codex")
    assert hook.dispatch(p, provider="codex")
    assert terminal_bindings.read_verified("codex", presence.time.monotonic() + 3)[9].session_id == OLD
    assert store.get(OLD)["name"] == "Keep this name"

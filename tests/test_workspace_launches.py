"""Launch intent proofs: isolated DB, never Terminal or a real agent process."""
from __future__ import annotations

import os
import shlex

import pytest

from claude_browse.board import commands, hook, launches, projects, store, work_items, workspace


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    monkeypatch.setattr(commands, "_indexed_session", lambda sid: None)
    monkeypatch.setattr(commands, "_raw_provider_path", lambda *args: None)
    monkeypatch.setattr(launches, "_available", lambda provider: True)
    monkeypatch.delenv(launches.TOKEN_ENV, raising=False)
    projects.resolve_project.cache_clear()


def task_at(tmp_path, sid="original"):
    cwd = tmp_path / sid
    cwd.mkdir(exist_ok=True)
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"Plan release"}}\n')
    store.upsert(sid, provider="claude", cwd=str(cwd), name="Release", transcript_path=str(transcript))
    task = work_items.ensure_for_session(store.get(sid))
    workspace.snapshot()
    return task


def destination(tmp_path, name="destination", linked=True):
    cwd = tmp_path / name
    if linked:
        cwd.mkdir(exist_ok=True)
    space = workspace.snapshot()["spaces"][0]
    return workspace.create_list(name, space["space_id"], working_directory=str(cwd) if linked else None)


def prepare_task(task, provider="claude", full_access=False):
    revision = workspace.context_for_task(work_items.get(task["task_id"]))["launch_revision"]
    return launches.prepare("task", task["task_id"], provider, full_access=full_access, launch_revision=revision)


def test_intent_command_contains_only_unguessable_token(tmp_path, monkeypatch):
    task = task_at(tmp_path)
    token = prepare_task(task)
    monkeypatch.setattr(commands, "_agent_board_executable", lambda: "/path with spaces/agent-board")
    assert len(token) >= 40
    assert shlex.split(launches.command(token)) == ["/path with spaces/agent-board", "launch-intent", token]
    with pytest.raises(ValueError):
        launches.command("$(touch /tmp/unwanted)")


def test_stale_duplicate_and_expired_intents_rejected(tmp_path, monkeypatch):
    task = task_at(tmp_path)
    with pytest.raises(ValueError, match="changed"):
        launches.prepare("task", task["task_id"], "claude", full_access=False, launch_revision="stale")
    token = prepare_task(task)
    with pytest.raises(ValueError, match="pending"):
        prepare_task(task)
    monkeypatch.setattr(launches.time, "time", lambda: 10**12)
    with pytest.raises(ValueError, match="expired"):
        launches.claim(token)


def test_claim_is_once_and_rechecks_destination(tmp_path):
    task = task_at(tmp_path)
    token = prepare_task(task)
    intent = launches.claim(token)
    assert intent["source_session_id"] == task["session_id"]
    with pytest.raises(ValueError, match="used"):
        launches.claim(token)


def test_duplicate_cli_cannot_cancel_the_first_claimed_launch(tmp_path):
    task = task_at(tmp_path)
    token = prepare_task(task)
    launches.claim(token)
    with pytest.raises(ValueError, match="used"):
        launches.execute(token)
    assert launches.get(token)["state"] == "claimed"
    launches.fail(token, "test cancellation")
    token = prepare_task(task)
    target = destination(tmp_path)
    workspace.move_task(task["task_id"], target["list_key"], workspace.context_for_task(task)["list_key"])
    with pytest.raises(ValueError, match="changed"):
        launches.claim(token)


def test_moved_task_requires_handoff_even_for_same_provider(tmp_path):
    task = task_at(tmp_path)
    target = destination(tmp_path)
    original_context = workspace.context_for_task(task)
    context = workspace.move_task(task["task_id"], target["list_key"], original_context["list_key"])
    session = commands.session_for_launch(task["session_id"])
    action = launches.action_status(session, context, "claude")
    assert action["available"]
    assert "Continue" in action["label"]
    assert action["mode"] == "handoff"
    session.pop("path")
    blocked = launches.action_status(session, context, "claude")
    assert not blocked["available"]
    assert "transcript" in blocked["reason"]


def test_original_native_resume_does_not_need_transcript(tmp_path):
    task = task_at(tmp_path)
    session = commands.session_for_launch(task["session_id"])
    session.pop("path")
    action = launches.action_status(session, workspace.context_for_task(task), "claude")
    assert action == {"label": "Resume Claude", "available": True, "reason": None, "mode": "native"}


@pytest.mark.parametrize("provider,moved,expected_relocate", [("claude", False, False), ("claude", True, True), ("codex", False, False)])
def test_cli_uses_effective_cwd_and_existing_provider_policy(tmp_path, monkeypatch, provider, moved, expected_relocate):
    from claude_browse import browse

    task = task_at(tmp_path)
    expected_cwd = task["session_cwd"]
    if moved:
        target = destination(tmp_path)
        workspace.move_task(task["task_id"], target["list_key"], workspace.context_for_task(task)["list_key"])
        expected_cwd = target["working_directory"]
    token = prepare_task(task, provider, full_access=True)
    calls = []
    monkeypatch.setattr(os, "chdir", lambda cwd: calls.append(("cwd", cwd)))
    monkeypatch.setattr(browse, "_open_in_target_provider", lambda *args, **kwargs: calls.append((args, kwargs)))
    launches.execute(token)
    assert calls[0] == ("cwd", expected_cwd)
    args, kwargs = calls[1]
    assert args[2] == provider and args[4] == expected_cwd and args[6] is True
    assert kwargs == {"fork": None, "relocate": expected_relocate}
    assert os.environ[launches.TOKEN_ENV] == token


def test_exec_failure_keeps_original_task_and_records_failure(tmp_path, monkeypatch):
    from claude_browse import browse

    task = task_at(tmp_path)
    token = prepare_task(task)
    monkeypatch.setattr(os, "chdir", lambda cwd: None)

    def broken(*args, **kwargs):
        raise OSError("provider unavailable")

    monkeypatch.setattr(browse, "_open_in_target_provider", broken)
    with pytest.raises(OSError):
        launches.execute(token)
    assert launches.get(token)["state"] == "failed"
    assert work_items.get(task["task_id"])["session_id"] == "original"
    assert launches.TOKEN_ENV not in os.environ


def test_session_start_adopts_once_preserves_metadata_and_history(tmp_path, monkeypatch):
    task = task_at(tmp_path)
    work_items.mutate(task["task_id"], title="My release", priority="urgent", due_date="2026-09-10")
    target = destination(tmp_path)
    context = workspace.move_task(task["task_id"], target["list_key"], workspace.context_for_task(task)["list_key"])
    token = prepare_task(task, "codex")
    launches.claim(token)
    monkeypatch.setenv(launches.TOKEN_ENV, token)
    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "continued", "cwd": context["working_directory"]}, "codex")
    current = work_items.get(task["task_id"])
    assert current["session_id"] == "continued"
    assert current["title"] == "My release" and current["priority"] == "urgent"
    assert current["due_date"] == "2026-09-10"
    assert launches.get(token)["state"] == "consumed"
    assert {row["session_id"] for row in work_items.get_session_history(task["task_id"])} == {"original", "continued"}
    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "independent", "cwd": context["working_directory"]}, "codex")
    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "original", "cwd": task["session_cwd"]}, "claude")
    work_items.reconcile_sessions()
    assert work_items.get(task["task_id"])["session_id"] == "continued"
    assert len(work_items.list_items()) == 2


@pytest.mark.parametrize("mismatch", ["unclaimed", "provider", "cwd", "stale"])
def test_bad_adoption_never_steals_task(tmp_path, monkeypatch, mismatch):
    task = task_at(tmp_path)
    token = prepare_task(task)
    if mismatch != "unclaimed":
        launches.claim(token)
    if mismatch == "stale":
        target = destination(tmp_path)
        workspace.move_task(task["task_id"], target["list_key"], workspace.context_for_task(task)["list_key"])
    monkeypatch.setenv(launches.TOKEN_ENV, token)
    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "not-adopted", "cwd": str(tmp_path) if mismatch == "cwd" else task["session_cwd"]}, "codex" if mismatch == "provider" else "claude")
    assert work_items.get(task["task_id"])["session_id"] == "original"
    assert work_items.get_for_session("not-adopted")["task_id"] != task["task_id"]


def test_list_start_captures_into_list_and_unlinked_is_blocked(tmp_path, monkeypatch):
    target = destination(tmp_path)
    context = workspace.context_for_list(target["list_key"])
    token = launches.prepare("list", target["list_key"], "claude", full_access=False, launch_revision=context["launch_revision"])
    launches.claim(token)
    monkeypatch.setenv(launches.TOKEN_ENV, token)
    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "new-work", "cwd": target["working_directory"]}, "claude")
    task = work_items.get_for_session("new-work")
    assert workspace.context_for_task(task)["list_key"] == target["list_key"]
    unlinked = destination(tmp_path, "Planning", linked=False)
    with pytest.raises(ValueError, match="[Ll]ink"):
        launches.prepare("list", unlinked["list_key"], "claude", full_access=False, launch_revision=workspace.context_for_list(unlinked["list_key"])["launch_revision"])


def test_list_start_exec_argv_full_access_opt_in(tmp_path, monkeypatch):
    target = destination(tmp_path)
    context = workspace.context_for_list(target["list_key"])
    token = launches.prepare("list", target["list_key"], "claude", full_access=False, launch_revision=context["launch_revision"])
    calls = []
    monkeypatch.setattr(os, "chdir", lambda cwd: calls.append(cwd))
    monkeypatch.setattr(os, "execvp", lambda binary, argv: calls.append((binary, argv)))
    launches.execute(token)
    assert calls == [target["working_directory"], ("claude", ["claude"])]

"""Focused U2 proofs: fresh task starts and transactional sibling placement."""
from __future__ import annotations

import os

import pytest

from claude_browse.board import commands, hook, launches, projects, store, work_items, workspace
from claude_browse.providers import get_provider


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    monkeypatch.setattr(commands, "_indexed_session", lambda _sid: None)
    monkeypatch.setattr(commands, "_raw_provider_path", lambda *_args: None)
    monkeypatch.setattr(launches, "_available", lambda _provider: True)
    monkeypatch.delenv(launches.TOKEN_ENV, raising=False)
    projects.resolve_project.cache_clear()


def task_at(tmp_path):
    cwd = tmp_path / "source"
    cwd.mkdir()
    transcript = tmp_path / "original.jsonl"
    transcript.write_text("original")
    store.upsert("original", provider="claude", cwd=str(cwd), transcript_path=str(transcript))
    task = work_items.ensure_for_session(store.get("original"))
    workspace.snapshot()
    transcript.unlink()
    return task, cwd


def fresh_token(task, provider="claude", full_access=False):
    revision = workspace.context_for_task(task)["launch_revision"]
    return launches.prepare("task-new", task["task_id"], provider, full_access=full_access, launch_revision=revision)


@pytest.mark.parametrize("provider", ("claude", "codex"))
def test_task_new_ignores_missing_transcript_and_executes_fresh_argv(tmp_path, monkeypatch, provider):
    task, cwd = task_at(tmp_path)
    token = fresh_token(task, provider, full_access=True)
    assert launches.get(token)["source_session_id"] == "original"
    calls = []
    monkeypatch.setattr(os, "chdir", lambda path: calls.append(("cwd", path)))
    monkeypatch.setattr(os, "execvp", lambda binary, argv: calls.append((binary, argv)))

    launches.execute(token)

    spec = get_provider(provider)
    expected = [spec.binary]
    if spec.handoff_yolo_flag:
        expected.append(spec.handoff_yolo_flag)
    assert calls == [("cwd", str(cwd)), (spec.binary, expected)]


def test_task_new_shares_task_lock_and_preserves_old_history_on_adoption(tmp_path, monkeypatch):
    task, cwd = task_at(tmp_path)
    token = fresh_token(task, "codex")
    with pytest.raises(ValueError, match="pending"):
        revision = workspace.context_for_task(task)["launch_revision"]
        launches.prepare("task", task["task_id"], "claude", full_access=False, launch_revision=revision)
    launches.claim(token)
    monkeypatch.setenv(launches.TOKEN_ENV, token)

    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "fresh", "cwd": str(cwd)}, "codex")

    assert work_items.get(task["task_id"])["session_id"] == "fresh"
    assert [row["session_id"] for row in work_items.get_session_history(task["task_id"])] == ["original", "fresh"]
    assert launches.get(token)["state"] == "consumed"


def test_task_new_rejects_stale_revision_and_expired_token(tmp_path, monkeypatch):
    task, _cwd = task_at(tmp_path)
    token = fresh_token(task)
    source = workspace.context_for_task(task)
    destination = tmp_path / "destination"
    destination.mkdir()
    listed = workspace.create_list("Destination", workspace.snapshot()["spaces"][0]["space_id"], working_directory=str(destination))
    workspace.move_task(task["task_id"], listed["list_key"], source["list_key"])
    with pytest.raises(ValueError, match="changed"):
        launches.claim(token)
    launches.fail(token, "test cleanup")

    token = fresh_token(work_items.get(task["task_id"]))
    monkeypatch.setattr(launches.time, "time", lambda: 10**12)
    with pytest.raises(ValueError, match="expired"):
        launches.claim(token)


def test_place_node_before_after_normalizes_only_same_parent_siblings():
    space = workspace.create_space("Space")
    first = workspace.create_list("First", space["space_id"])
    second = workspace.create_list("Second", space["space_id"])
    third = workspace.create_list("Third", space["space_id"])
    workspace.update_list(first["list_key"], position=10)
    workspace.update_list(second["list_key"], position=10)
    workspace.update_list(third["list_key"], position=20)
    lists = [item for item in workspace.snapshot()["lists"] if item["space_id"] == space["space_id"]]
    original_ids = [item["list_key"] for item in lists]

    workspace.place_node("list", third["list_key"], first["list_key"], "before")
    lists = [item for item in workspace.snapshot()["lists"] if item["space_id"] == space["space_id"]]
    expected = [item for item in original_ids if item != third["list_key"]]
    expected.insert(expected.index(first["list_key"]), third["list_key"])
    assert [item["list_key"] for item in lists] == expected
    assert [item["position"] for item in lists] == [1, 2, 3]

    workspace.place_node("list", third["list_key"], second["list_key"], "after")
    lists = [item for item in workspace.snapshot()["lists"] if item["space_id"] == space["space_id"]]
    expected = [item for item in expected if item != third["list_key"]]
    expected.insert(expected.index(second["list_key"]) + 1, third["list_key"])
    assert [item["list_key"] for item in lists] == expected


def test_place_node_rejects_foreign_parent_without_partial_reorder():
    first = workspace.create_space("First")
    second = workspace.create_space("Second")
    left = workspace.create_list("Left", first["space_id"])
    right = workspace.create_list("Right", second["space_id"])
    before = workspace.snapshot()

    with pytest.raises(ValueError, match="share the node parent"):
        workspace.place_node("list", left["list_key"], right["list_key"], "before")

    assert workspace.snapshot() == before


@pytest.mark.parametrize("placement", ([], {}))
def test_place_node_rejects_non_string_placement(placement):
    space = workspace.create_space("Space")
    first = workspace.create_list("First", space["space_id"])
    second = workspace.create_list("Second", space["space_id"])

    with pytest.raises(ValueError, match="placement must be before or after"):
        workspace.place_node("list", first["list_key"], second["list_key"], placement)

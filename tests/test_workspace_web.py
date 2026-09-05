"""HTTP contract tests for the linked workspace routes.

These use the actual threaded local server and a temporary board database so
the request, storage, and JSON boundary is covered together.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from claude_browse import web
from claude_browse.board import commands, launches, store, work_items


@pytest.fixture()
def workspace_server(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    monkeypatch.setattr(store, "_conn_cache_path", None)
    monkeypatch.setattr(store, "_conn_cache_owner", None)

    server = web.ThreadingHTTPServer(("127.0.0.1", 0), web._Handler)
    server.launch_cwd = str(tmp_path)
    server.cwd_filter = None
    server.folder_prefixes = ()
    server.session_limit = 100
    server.csrf_token = "workspace-token"
    server.edit_revision_lock = threading.Lock()
    server.edit_revisions = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", tmp_path
    finally:
        server.shutdown()
        server.server_close()


def _request(base, path, method="GET", payload=None, *, token="workspace-token"):
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Agent-Board-Token": token,
        }
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode())


def _task(tmp_path: Path, session_id="workspace-task"):
    store.upsert(
        session_id,
        cwd=str(tmp_path),
        provider="claude",
        name="Workspace task",
        state="idle",
    )
    return work_items.ensure_for_session(store.get(session_id))


def test_workspace_crud_directory_and_board_context_roundtrip(workspace_server):
    base, tmp_path = workspace_server
    task = _task(tmp_path)

    _, created_space = _request(base, "/api/workspace/spaces", "POST", {"name": "Studio"})
    space = created_space["space"]
    _, created_folder = _request(
        base, "/api/workspace/folders", "POST", {"name": "Apps", "space_id": space["space_id"]}
    )
    folder = created_folder["folder"]
    _, created_list = _request(
        base,
        "/api/workspace/lists",
        "POST",
        {"name": "Browse", "space_id": space["space_id"], "folder_id": folder["folder_id"]},
    )
    listing = created_list["list"]
    _, directory = _request(
        base,
        f"/api/workspace/lists/{listing['list_key']}/directory",
        "POST",
        {"path": str(tmp_path / "browse-work")},
    )
    assert directory["list"]["working_directory"] == str(tmp_path / "browse-work")

    _, moved = _request(
        base,
        f"/api/workspace/tasks/{task['task_id']}/move",
        "POST",
        {"list_key": listing["list_key"], "expected_list_key": task["project_key"]},
    )
    assert moved["context"]["list_key"] == listing["list_key"]
    assert moved["context"]["working_directory"] == str(tmp_path / "browse-work")

    _, board = _request(base, "/api/board")
    row = next(row for row in board["tasks"] if row["task_id"] == task["task_id"])
    assert row["project_key"] == task["project_key"]  # source identity remains legacy data
    assert row["list_key"] == listing["list_key"]
    assert row["list_name"] == "Browse"
    assert row["folder_status"] == "ready"
    assert row["launch_revision"]
    assert board["workspace"]["spaces"]
    assert next(
        item for item in board["workspace"]["lists"] if item["list_key"] == listing["list_key"]
    )["launch_revision"]


def test_workspace_routes_reject_unknown_fields_and_preserve_mutation_guards(workspace_server):
    base, _tmp_path = workspace_server
    for path, body in (
        ("/api/workspace/spaces", {"name": "Studio", "extra": True}),
        ("/api/workspace/folders", {"name": "Apps"}),
        ("/api/workspace/lists", {"name": "Browse", "space_id": "missing", "unknown": 1}),
        ("/api/workspace/tasks/reorder", {"list_key": "x", "task_ids": [], "extra": 1}),
        ("/api/workspace/reorder", {"kind": "space", "node_id": "x", "direction": -1, "extra": 1}),
    ):
        with pytest.raises(urllib.error.HTTPError) as error:
            _request(base, path, "POST", body)
        assert error.value.code == 400

    with pytest.raises(urllib.error.HTTPError) as error:
        _request(base, "/api/workspace/spaces", "POST", {"name": "Studio"}, token="wrong")
    assert error.value.code == 403

    request = urllib.request.Request(
        base + "/api/workspace/spaces",
        data=b'{"name":"Studio"}',
        method="POST",
        headers={"Content-Type": "text/plain", "X-Agent-Board-Token": "workspace-token"},
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 415


def test_workspace_node_reorder_route_uses_strict_body_and_returns_node(workspace_server):
    base, _tmp_path = workspace_server
    _, first = _request(base, "/api/workspace/spaces", "POST", {"name": "First"})
    _, second = _request(base, "/api/workspace/spaces", "POST", {"name": "Second"})
    _, result = _request(
        base,
        "/api/workspace/reorder",
        "POST",
        {"kind": "space", "node_id": second["space"]["space_id"], "direction": -1},
    )
    assert result["node"]["space_id"] == second["space"]["space_id"]

    for body in (
        {"kind": "space", "node_id": second["space"]["space_id"]},
        {"kind": "unknown", "node_id": second["space"]["space_id"], "direction": -1},
    ):
        with pytest.raises(urllib.error.HTTPError) as error:
            _request(base, "/api/workspace/reorder", "POST", body)
        assert error.value.code == 400


def test_workspace_move_reorder_and_history_are_canonical(workspace_server):
    base, tmp_path = workspace_server
    first = _task(tmp_path, "first-workspace-task")
    second = _task(tmp_path, "second-workspace-task")
    _, board = _request(base, "/api/board")
    source_list = next(row for row in board["tasks"] if row["task_id"] == first["task_id"])["list_key"]

    _, reordered = _request(
        base,
        "/api/workspace/tasks/reorder",
        "POST",
        {"list_key": source_list, "task_ids": [second["task_id"], first["task_id"]], "priority": "high"},
    )
    assert [row["task_id"] for row in reordered["tasks"]] == [second["task_id"], first["task_id"]]
    assert {row["priority"] for row in reordered["tasks"]} == {"high"}

    store.upsert(
        "continued-workspace-task",
        cwd=str(tmp_path),
        provider="codex",
        name="Continued workspace task",
        state="idle",
    )
    work_items.attach_continuation(
        first["task_id"],
        store.get("continued-workspace-task"),
        expected_session_id="first-workspace-task",
    )
    status, history = _request(base, f"/api/tasks/{first['task_id']}/history")
    assert status == 200
    assert [session["session_id"] for session in history["sessions"]] == [
        "first-workspace-task",
        "continued-workspace-task",
    ]
    assert {session["provider"] for session in history["sessions"]} == {"claude", "codex"}


def test_launch_routes_require_current_revision_and_only_open_reserved_command(workspace_server, monkeypatch):
    base, tmp_path = workspace_server
    task = _task(tmp_path)
    monkeypatch.setattr(launches, "_available", lambda provider: True)
    opened = []
    monkeypatch.setattr(commands, "open_in_terminal", opened.append)
    _, board = _request(base, "/api/board")
    row = next(row for row in board["tasks"] if row["task_id"] == task["task_id"])
    body = {"provider": "claude", "full_access": False}
    for invalid in (body, {**body, "launch_revision": "stale"}, {**body, "launch_revision": row["launch_revision"], "command": "echo unsafe"}):
        with pytest.raises(urllib.error.HTTPError) as error:
            _request(base, f"/api/tasks/{task['task_id']}/launch", "POST", invalid)
        assert error.value.code == 400
        assert not opened
    _, result = _request(base, f"/api/tasks/{task['task_id']}/launch", "POST", {**body, "launch_revision": row["launch_revision"]})
    assert result == {"ok": True}
    assert "launch-intent" in opened[0] and task["session_id"] not in opened[0]

    listing = next(item for item in board["workspace"]["lists"] if item["list_key"] == row["list_key"])
    _, result = _request(base, f"/api/workspace/lists/{listing['list_key']}/launch", "POST", {**body, "launch_revision": listing["launch_revision"]})
    assert result == {"ok": True} and len(opened) == 2


def test_task_patch_retains_destination_and_correct_handoff_action(workspace_server, monkeypatch):
    from claude_browse.board import workspace

    base, tmp_path = workspace_server
    task = _task(tmp_path)
    monkeypatch.setattr(web, "_provider_available", lambda provider: True)
    space = workspace.snapshot()["spaces"][0]
    listing = workspace.create_list("Planning only", space["space_id"])
    workspace.move_task(task["task_id"], listing["list_key"], task["project_key"])
    _, result = _request(base, f"/api/tasks/{task['task_id']}", "PATCH", {"priority": "urgent"})
    assert result["task"]["list_key"] == listing["list_key"]
    assert result["task"]["working_directory"] is None
    assert not result["task"]["actions"]["claude"]["available"]

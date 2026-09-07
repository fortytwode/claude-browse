"""Real loopback API proofs for open-terminal counts and explicit fresh starts."""

from __future__ import annotations

import json
import shlex
import threading
import urllib.error
import urllib.request

import pytest

from claude_browse import fts, web
from claude_browse.board import commands, hook, launches, presence, store, work_items, workspace


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "board.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    index_path = str(tmp_path / "search.db")
    real_open = fts.open_db
    real_open(index_path).close()
    monkeypatch.setattr(fts, "open_db", lambda *args, **kwargs: real_open(index_path, **kwargs))
    monkeypatch.setattr(commands, "_raw_provider_path", lambda *_args: None)
    monkeypatch.setattr(web, "_provider_available", lambda _provider: True)
    monkeypatch.setattr(launches, "_available", lambda _provider: True)
    monkeypatch.setattr(hook, "_spawn_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(presence, "snapshot", lambda rows: {
        row["session_id"]: "unknown" for row in rows
    })
    server = web.ThreadingHTTPServer(("127.0.0.1", 0), web._Handler)
    server.launch_cwd = str(tmp_path)
    server.cwd_filter = None
    server.folder_prefixes = ()
    server.session_limit = 100
    server.csrf_token = "polish-fixture"
    server.edit_revision_lock = threading.Lock()
    server.edit_revisions = {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", tmp_path
    finally:
        server.shutdown()
        server.server_close()


def request(base, path, body=None, *, method=None, token="polish-fixture", host=None):
    headers = {"Content-Type": "application/json", "X-Agent-Board-Token": token}
    if host:
        headers["Host"] = host
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode() if body is not None else None,
        method=method or ("POST" if body is not None else "GET"), headers=headers,
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def task_at(path, sid, **fields):
    store.upsert(sid, cwd=str(path), provider="claude", host="fixture-mac", state="idle", **fields)
    return work_items.ensure_for_session(store.get(sid))


def test_presence_is_batched_and_counts_follow_placement_not_completion(api, monkeypatch):
    base, path = api
    first = task_at(path, "open")
    task_at(path, "closed")
    task_at(path, "unknown")
    work_items.mutate(first["task_id"], status="done")
    general = workspace.snapshot()["spaces"][0]
    folder = workspace.create_folder("Apps", general["space_id"])
    destination = workspace.create_list("Release", general["space_id"], folder["folder_id"])
    workspace.move_task(first["task_id"], destination["list_key"], first["project_key"])
    scans = []

    def scan(rows):
        scans.append(rows)
        return {row["session_id"]: row["session_id"] for row in rows}

    monkeypatch.setattr(presence, "snapshot", scan)
    board = request(base, "/api/board")
    assert len(scans) == 1 and len(scans[0]) == 3
    assert board["counts"] == {
        "total": 3, "open_terminal": 1, "closed_terminal": 1, "unknown_terminal": 1,
    }
    opened = next(row for row in board["tasks"] if row["session_id"] == "open")
    assert opened["terminal_open"] and opened["work_status"] == "done"
    assert opened["terminal_runtime_state"] == "idle"
    assert next(row for row in board["tasks"] if row["session_id"] == "unknown")["terminal_presence"] == "unknown"
    listed = next(row for row in board["workspace"]["lists"] if row["list_key"] == destination["list_key"])
    assert listed["counts"]["open_terminal"] == listed["counts"]["total"] == 1
    assert board["workspace"]["folders"][0]["counts"] == listed["counts"]
    assert board["workspace"]["spaces"][0]["counts"] == board["counts"]


def test_board_reuses_runtime_rows_and_provider_availability_per_response(api, monkeypatch):
    base, path = api
    task_at(path, "first")
    task_at(path, "second")
    original_get = store.get
    reads = []
    availability = []

    def get(session_id):
        reads.append(session_id)
        return original_get(session_id)

    monkeypatch.setattr(store, "get", get)
    monkeypatch.setattr(web, "_provider_available", lambda provider: availability.append(provider) or True)

    request(base, "/api/board")

    assert reads.count("first") == reads.count("second") == 1
    assert sorted(availability) == ["claude", "codex"]


def test_live_capture_precedes_reconciliation_and_one_failure_does_not_skip_the_other(monkeypatch):
    calls = []
    monkeypatch.setattr(web.discovery, "capture_live_sessions", lambda: calls.append("capture"))
    monkeypatch.setattr(web.work_items, "reconcile_sessions", lambda: calls.append("reconcile"))

    web._capture_and_reconcile()

    assert calls == ["capture", "reconcile"]
    monkeypatch.setattr(web.discovery, "capture_live_sessions", lambda: (_ for _ in ()).throw(OSError()))
    web._capture_and_reconcile()
    assert calls == ["capture", "reconcile", "reconcile"]


@pytest.mark.parametrize("value,expected", [
    (None, 0), (True, 0), ("nonsense", 0), (float("inf"), 0), (float("nan"), 0),
    (-1, 0), ("1700000000", 1700000000),
    ("2026-09-05T00:00:00Z", 1788566400),
    ("2026-09-05T05:30:00+05:30", 1788566400),
    ("2026-09-05T00:00:00", 1788566400),
])
def test_timestamp_boundary_is_finite_epoch_seconds(value, expected):
    assert web._timestamp_seconds(value) == expected


def test_last_update_includes_indexed_conversation_activity(api):
    base, path = api
    task = task_at(path, "indexed")
    store._raw_set_updated_at("indexed", 1)
    with store.get_conn() as conn:
        conn.execute("UPDATE work_items SET updated_at=2 WHERE task_id=?", (task["task_id"],))
    with fts.open_db() as conn:
        conn.execute(
            """INSERT INTO sessions
               (sid,path,provider,cwd,timestamp,last_timestamp,mtime,indexed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("indexed", str(path / "missing.jsonl"), "claude", str(path),
             "2026-09-04T00:00:00Z", "2026-09-05T00:00:00Z", 1, 1),
        )
    row = request(base, "/api/board")["tasks"][0]
    assert row["last_activity_at"] == 1788566400


def test_missing_transcript_offers_fresh_actions_without_claiming_handoff(api, monkeypatch):
    base, path = api
    task = task_at(path, "no-original")
    row = request(base, "/api/board")["tasks"][0]
    assert not row["actions"]["codex"]["available"]
    assert row["actions"]["claude"]["available"]
    assert all(action["available"] and action["mode"] == "new" for action in row["start_actions"].values())
    detail = request(base, "/api/session/no-original")
    assert detail["transcript_error"] and detail["turns"] == []
    assert detail["meta"]["task_launch"]["start_actions"] == row["start_actions"]
    opened = []
    monkeypatch.setattr(commands, "open_in_terminal", opened.append)
    result = request(base, f"/api/tasks/{task['task_id']}/start", {
        "provider": "codex", "full_access": False, "launch_revision": row["launch_revision"],
    })
    assert result == {"ok": True} and len(opened) == 1
    argv = shlex.split(opened[0])
    assert argv[-2] == "launch-intent" and "no-original" not in opened[0]
    intent = launches.get(argv[-1])
    assert intent["kind"] == "task-new" and intent["source_session_id"] == "no-original"
    assert work_items.get(task["task_id"])["session_id"] == "no-original"


@pytest.mark.parametrize("change", [
    {"launch_revision": "stale"}, {"full_access": "true"}, {"provider": "other"},
    {"command": "arbitrary-command"},
])
def test_fresh_start_rejects_invalid_fields_before_terminal(api, monkeypatch, change):
    base, path = api
    task = task_at(path, "invalid")
    row = request(base, "/api/board")["tasks"][0]
    monkeypatch.setattr(commands, "open_in_terminal", lambda *_args: pytest.fail("must not launch"))
    with pytest.raises(urllib.error.HTTPError) as error:
        request(base, f"/api/tasks/{task['task_id']}/start", {
            "provider": "codex", "full_access": False, "launch_revision": row["launch_revision"], **change,
        })
    assert error.value.code == 400


@pytest.mark.parametrize("guard", [{"token": "wrong"}, {"host": "evil.example"}])
def test_fresh_start_preserves_host_and_csrf_checks(api, guard):
    base, _path = api
    with pytest.raises(urllib.error.HTTPError) as error:
        request(base, "/api/tasks/anything/start", {}, **guard)
    assert error.value.code == 403


def test_sidebar_drop_payload_uses_existing_guarded_reorder_route(api):
    base, _path = api
    first = workspace.create_space("First")
    second = workspace.create_space("Second")
    result = request(base, "/api/workspace/reorder", {
        "kind": "space", "node_id": second["space_id"],
        "target_id": first["space_id"], "placement": "before",
    })
    assert result["node"]["space_id"] == second["space_id"]
    spaces = workspace.snapshot()["spaces"]
    ids = [row["space_id"] for row in spaces]
    assert ids.index(second["space_id"]) < ids.index(first["space_id"])
    with pytest.raises(urllib.error.HTTPError) as error:
        request(base, "/api/workspace/reorder", {
            "kind": "space", "node_id": second["space_id"], "direction": -1,
            "target_id": first["space_id"], "placement": "before",
        })
    assert error.value.code == 400


def test_inline_rename_keeps_presence_and_updates_terminal_status_name(api, monkeypatch):
    base, path = api
    task = task_at(path, "rename")
    monkeypatch.setattr(presence, "snapshot", lambda _rows: {"rename": "open"})
    result = request(base, f"/api/tasks/{task['task_id']}", {
        "title": "Plan the release", "_edit_client": "fixture", "_edit_revision": 1,
    }, method="PATCH")
    assert result["task"]["terminal_open"]
    assert result["task"]["title"] == store.get("rename")["name"] == "Plan the release"
    assert store.get("rename")["name_source"] == "manual"
    assert request(base, "/api/session/rename")["meta"]["title"] == "Plan the release"

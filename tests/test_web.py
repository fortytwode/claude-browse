"""Tests for the --web local viewer: HTTP routes, scoping, and error paths.

Drives a real ThreadingHTTPServer (as run_server constructs it) over the
loopback with urllib, against a seeded temp FTS db, so the full
request->sqlite->JSON path is exercised rather than handler internals.
"""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from claude_browse import fts, web
from claude_browse.board import hook, launches, store, work_items, workspace


def _seed(
    conn,
    sid,
    *,
    cwd="/w/home",
    provider="claude",
    path="",
    title=None,
    first_msg=None,
    last_msg="",
):
    now = 1700000000.0
    conn.execute(
        """
        INSERT INTO sessions (sid, path, provider, cwd, timestamp, last_timestamp,
                              title, first_msg, last_msg, msg_count, mtime,
                              indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            path or f"/tmp/{sid}.jsonl",
            provider,
            cwd,
            "2026-05-01T10:00:00Z",
            "2026-05-01T10:00:00Z",
            title or f"Title for {sid}",
            first_msg if first_msg is not None else f"first message for {sid}",
            last_msg,
            4,
            now,
            now,
        ),
    )
    conn.execute(
        """INSERT INTO sessions_fts
           (sid, cwd, title, first_msg, user_text, asst_text, boilerplate)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sid, cwd, "", "", f"corpus for {sid}", "", ""),
    )
    conn.commit()


@pytest.fixture()
def web_server(monkeypatch):
    """A live server on 127.0.0.1 backed by a seeded temp db.

    Yields (base_url, factory) where factory(cwd_filter=..., limit=...)
    rebinds the server attributes so one fixture covers plain and
    --here-forced variants.
    """
    db_path = tempfile.mktemp(suffix=".db")
    real_open_db = fts.open_db
    conn = real_open_db(db_path)
    _seed(conn, "home1", cwd="/w/home")
    _seed(conn, "home-sub", cwd="/w/home/sub")
    _seed(conn, "other1", cwd="/w/other")
    conn.close()

    # open_db's default path is bound at def time, so patching fts.DB_PATH
    # alone would not redirect the handlers -- wrap open_db itself.
    def _open_test_db(path=db_path, *, read_only=False):
        return real_open_db(db_path, read_only=read_only)

    monkeypatch.setattr(fts, "open_db", _open_test_db)
    monkeypatch.setattr(fts, "DB_PATH", db_path)
    board_db_path = db_path + "-board"
    monkeypatch.setattr(store, "_DB_PATH", Path(board_db_path))
    monkeypatch.setattr(store, "_conn_cache", None)

    server = ThreadingHTTPServer(("127.0.0.1", 0), web._Handler)
    server.launch_cwd = "/w/home"
    server.cwd_filter = None
    server.folder_prefixes = ()
    server.session_limit = 100
    server.csrf_token = "test-token"
    server.edit_revision_lock = threading.Lock()
    server.edit_revisions = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    yield base, server

    server.shutdown()
    server.server_close()
    if os.path.exists(db_path):
        os.unlink(db_path)
    if os.path.exists(board_db_path):
        os.unlink(board_db_path)


def _get_json(url, host=None):
    req = urllib.request.Request(url)
    if host is not None:
        req.add_header("Host", host)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


def _mutate_json(url, method, payload, *, token="test-token"):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Agent-Board-Token": token,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


def _board_thread(sid, *, cwd="/w/home", provider="claude", name=None):
    store.upsert(
        sid,
        cwd=cwd,
        provider=provider,
        name=name or f"Thread {sid}",
        state="idle",
        heartbeat_at=1700000000.0,
    )
    return work_items.ensure_for_session(store.get(sid))


def test_sessions_lists_seeded_rows_current_folder_first(web_server):
    base, _server = web_server
    status, data = _get_json(base + "/api/sessions")
    assert status == 200
    sids = [s["session_id"] for s in data["sessions"]]
    assert set(sids) == {"home1", "home-sub", "other1"}
    # launch_cwd=/w/home: its sessions are guaranteed and floated first
    assert set(sids[:2]) == {"home1", "home-sub"}
    assert all("when" in s for s in data["sessions"])


def test_sessions_here_param_scopes_to_folder(web_server):
    base, _server = web_server
    status, data = _get_json(base + "/api/sessions?here=1")
    assert status == 200
    sids = {s["session_id"] for s in data["sessions"]}
    assert sids == {"home1", "home-sub"}


def test_meta_reflects_forced_here(web_server):
    base, server = web_server
    _status, data = _get_json(base + "/api/meta")
    assert data["here_only_forced"] is False
    assert data["csrf_token"] == "test-token"
    assert data["launch_project"]["path"] == "/w/home"

    server.cwd_filter = "/w/home"
    _status, data = _get_json(base + "/api/meta")
    assert data["here_only_forced"] is True
    # forced --here scopes even without the client checkbox
    _status, listing = _get_json(base + "/api/sessions")
    assert {s["session_id"] for s in listing["sessions"]} == {"home1", "home-sub"}


def test_session_detail_missing_sid_is_404(web_server):
    base, _server = web_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get_json(base + "/api/session/does-not-exist")
    assert exc_info.value.code == 404


def test_session_detail_returns_meta_and_turns(web_server, tmp_path):
    base, _server = web_server
    # Point home1's path at a real Claude transcript with a multiline turn.
    transcript = tmp_path / "home1.jsonl"
    body = "Here is code:\n\n```python\ndef foo():\n    return 1\n```\nDone."
    transcript.write_text(
        json.dumps({"message": {"role": "user", "content": "hello world, question"}})
        + "\n"
        + json.dumps({"message": {"role": "assistant", "content": body}})
        + "\n"
    )
    conn = fts.open_db(fts.DB_PATH)
    conn.execute("UPDATE sessions SET path = ? WHERE sid = 'home1'", (str(transcript),))
    conn.commit()
    conn.close()

    status, data = _get_json(base + "/api/session/home1")
    assert status == 200
    assert data["meta"]["session_id"] == "home1"
    roles = [t["role"] for t in data["turns"]]
    assert roles == ["user", "assistant"]
    # Newlines must survive to the client so code blocks/paragraphs render.
    assert "\n" in data["turns"][1]["text"]
    assert "```python" in data["turns"][1]["text"]


def test_unknown_route_is_404(web_server):
    base, _server = web_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get_json(base + "/nope")
    assert exc_info.value.code == 404


def test_foreign_host_header_is_rejected(web_server):
    base, _server = web_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get_json(base + "/api/sessions", host="evil.example.com")
    assert exc_info.value.code == 403


def test_localhost_host_headers_are_accepted(web_server):
    base, _server = web_server
    for host in ("127.0.0.1:9999", "localhost", "localhost:1234"):
        status, _data = _get_json(base + "/api/sessions", host=host)
        assert status == 200


def test_unavailable_index_degrades_to_503(web_server, monkeypatch):
    base, _server = web_server
    import sqlite3

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(web.fts, "open_db", _boom)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get_json(base + "/api/sessions")
    assert exc_info.value.code == 503
    payload = json.loads(exc_info.value.read().decode())
    assert "search index unavailable" in payload["error"]


def test_assets_are_served_with_content_types(web_server):
    base, _server = web_server
    for path, expected_type in (
        ("/", "text/html"),
        ("/app.js", "application/javascript"),
        ("/app.css", "text/css"),
    ):
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith(expected_type)
            assert resp.headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
            assert resp.headers["Cache-Control"] == "no-store, max-age=0"
            assert len(resp.read()) > 0


def test_web_assets_define_reading_first_work_and_history_contract():
    assets = Path(web.__file__).with_name("webassets")
    html = (assets / "index.html").read_text()
    javascript = (assets / "app.js").read_text()
    stylesheet = (assets / "app.css").read_text()

    assert 'id="work-search"' in html
    assert 'id="full-access"' in html
    assert 'Full access (skip permissions)' in " ".join(html.split())
    assert 'id="full-access" type="checkbox" checked' in html
    assert 'data-scope="all"' in html
    assert 'data-scope="today"' in html
    assert 'id="folder-list"' in html
    assert 'id="filter-status"' in html
    assert '<option value="completed">Completed</option>' in html

    for heading in (
        "Name",
        "Due date",
        "Work status",
        "Terminal state",
        "Last update",
        "Open details",
    ):
        assert heading in javascript
    for searchable_field in (
        "task.title",
        "task.project_name",
        "task.session_provider",
        "task.session_id",
    ):
        assert searchable_field in javascript

    assert "meta.actions" in javascript
    assert "action.label" in javascript
    assert "fullAccessEnabled()" in javascript
    assert 'setAttribute("aria-invalid", "true")' in javascript
    assert 'role="alert"' in html
    assert "saveTask" in javascript
    assert "afterPendingEdits" in javascript
    assert "_edit_revision" in javascript
    assert "editRevisionCounters" in javascript
    assert 'window.addEventListener("pagehide"' in javascript
    assert "closeTaskDialog" in javascript
    assert "setLaunchBusy" in javascript
    assert "rowMutationTails" in javascript
    assert "delete rowMutationTails[key]" in javascript
    assert "Save or cancel the project description before changing views." in javascript
    assert "hasProtectedWorkControls" in javascript
    assert "History is read-only" in html
    assert "disabled-reason" in stylesheet
    assert "grid-template-areas" in stylesheet


def test_web_assets_define_project_priority_and_reorder_contract():
    assets = Path(web.__file__).with_name("webassets")
    html = (assets / "index.html").read_text()
    javascript = (assets / "app.js").read_text()
    stylesheet = (assets / "app.css").read_text()

    for element_id in (
        "work-sidebar",
        "folder-list",
        "project-detail",
        "project-description",
        "group-by",
        "reorder-reason",
        "work-announcer",
    ):
        assert f'id="{element_id}"' in html
    assert 'maxlength="10000"' in html
    assert 'data-scope="all"' in html
    assert 'data-scope="today"' in html
    assert 'id="filter-status"' in html
    assert '<option value="completed">Completed</option>' in html

    for contract in (
        'PRIORITY_GROUPS = ["urgent", "high", "normal", "low"]',
        'TERMINAL_GROUPS = ["needs-input", "working", "idle", "ended", "gone"]',
        '"/api/workspace/tasks/reorder"',
        'mutate("/api/projects/reorder"',
        '"/api/projects/" + encodeURIComponent',
        'task.summary',
        'task.priority',
        'dragstart',
        'dragover',
        'Move up',
        'Move down',
        'Set priority',
        'Manual reordering is disabled while searching.',
        'Terminal state is runtime truth',
        'queueMode = "all"',
        'Closed rows cannot change priority by dragging.',
        'function queueReorder',
    ):
        assert contract in javascript

    assert "grid-template-columns: 250px minmax(0, 1fr)" in stylesheet
    assert "work-sidebar" in stylesheet
    assert "task-link" in stylesheet
    assert "@media (max-width: 680px)" in stylesheet


def test_automatic_thread_update_and_board_roundtrip(web_server):
    base, _server = web_server
    _board_thread("automatic", provider="codex", name="Ship the work queue")
    _status, board = _get_json(base + "/api/board")
    task = board["tasks"][0]
    assert task["title"] == "Ship the work queue"
    assert task["due_date"] is None
    assert task["priority"] == "normal"
    assert isinstance(task["position"], int)
    assert task["summary"] == "(no transcript preview)"
    assert "full_command" not in task
    assert "safe_command" not in task

    _status, updated = _mutate_json(
        base + "/api/tasks/" + task["task_id"],
        "PATCH",
        {"title": "Ship it", "status": "active", "due_date": None, "priority": "high"},
    )
    assert updated["task"]["title"] == "Ship it"
    assert updated["task"]["work_status"] == "active"
    assert updated["task"]["due_date"] is None
    assert updated["task"]["priority"] == "high"

    _status, board = _get_json(base + "/api/board")
    assert [item["title"] for item in board["tasks"]] == ["Ship it"]


def test_board_returns_summary_fallback_and_project_aggregates(web_server):
    base, _server = web_server
    conn = fts.open_db(fts.DB_PATH)
    _seed(
        conn,
        "summary-last",
        cwd="/w/summary",
        first_msg="opening",
        last_msg="  newest\nrequest  ",
    )
    _seed(conn, "summary-empty", cwd="/w/summary", first_msg="", last_msg="")
    conn.close()
    first = _board_thread("summary-last", cwd="/w/summary")
    second = _board_thread("summary-empty", cwd="/w/summary")
    work_items.mutate(second["task_id"], status="done")
    work_items.set_project_description(first["project_key"], "Project context")

    _status, board = _get_json(base + "/api/board")
    tasks = {task["session_id"]: task for task in board["tasks"]}
    assert tasks["summary-last"]["summary"] == "newest request"
    assert tasks["summary-empty"]["summary"] == "(no transcript preview)"
    project = next(p for p in board["projects"] if p["project_key"] == first["project_key"])
    assert project["name"] == first["project_name"]
    assert project["path"] == first["project_path"]
    assert project["description"] == "Project context"
    assert project["counts"] == {"active": 1, "today": 0, "needs_input": 0}

    conn = fts.open_db(fts.DB_PATH)
    conn.execute("UPDATE sessions SET last_msg = ? WHERE sid = ?", ("x" * 300, "summary-last"))
    conn.commit()
    conn.close()
    _status, capped = _get_json(base + "/api/board")
    capped_task = next(t for t in capped["tasks"] if t["session_id"] == "summary-last")
    assert capped_task["summary"] == "x" * 200


def test_board_summary_degrades_when_index_fails(web_server, monkeypatch):
    base, _server = web_server
    _board_thread("no-index", cwd="/w/no-index")
    monkeypatch.setattr(
        web.fts,
        "open_db",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("boom")
        ),
    )
    _status, board = _get_json(base + "/api/board")
    assert board["tasks"][0]["summary"] == "(no transcript preview)"


def test_reorder_and_project_routes_are_protected_and_transactional(web_server):
    base, _server = web_server
    one = _board_thread("route-one", cwd="/w/routes")
    two = _board_thread("route-two", cwd="/w/routes")
    payload = {
        "project_key": one["project_key"],
        "task_ids": [two["task_id"], one["task_id"]],
        "priority": "urgent",
    }
    _status, response = _mutate_json(base + "/api/tasks/reorder", "POST", payload)
    assert [task["task_id"] for task in response["tasks"]] == payload["task_ids"]
    assert {task["priority"] for task in response["tasks"]} == {"urgent"}

    _mutate_json(base + "/api/tasks/" + one["task_id"], "PATCH", {"priority": "low"})
    mixed_payload = {
        "project_key": one["project_key"],
        "task_ids": [one["task_id"], two["task_id"]],
    }
    _status, response = _mutate_json(
        base + "/api/tasks/reorder", "POST", mixed_payload
    )
    assert [task["task_id"] for task in response["tasks"]] == mixed_payload["task_ids"]
    assert [task["priority"] for task in response["tasks"]] == ["low", "urgent"]

    project_url = base + "/api/projects/" + urllib.parse.quote(
        one["project_key"], safe=""
    )
    _status, response = _mutate_json(project_url, "PATCH", {"description": "Routes"})
    assert response["project"]["description"] == "Routes"
    _status, response = _mutate_json(
        base + "/api/projects/reorder",
        "POST",
        {"project_keys": [one["project_key"]]},
    )
    assert response["projects"][0]["project_key"] == one["project_key"]

    for url, method, body in (
        (base + "/api/tasks/reorder", "POST", payload),
        (project_url, "PATCH", {"description": "blocked"}),
        (
            base + "/api/projects/reorder",
            "POST",
            {"project_keys": [one["project_key"]]},
        ),
    ):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _mutate_json(url, method, body, token="wrong")
        assert exc_info.value.code == 403

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method=method,
            headers={
                "Content-Type": "text/plain",
                "X-Agent-Board-Token": "test-token",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=10)
        assert exc_info.value.code == 415


def test_every_observed_terminal_session_is_automatically_listed(web_server):
    base, _server = web_server
    _board_thread("first", name="First terminal")
    _board_thread("second", provider="codex", name="Second terminal")
    _status, board = _get_json(base + "/api/board")
    assert {task["session_id"] for task in board["tasks"]} == {"first", "second"}
    assert all(task["work_status"] == "active" for task in board["tasks"])


def test_board_get_is_read_only_and_does_not_enroll_fts_only_or_late_runtime_rows(
    web_server, monkeypatch
):
    base, _server = web_server
    store.upsert("late-runtime", cwd="/w/late", name="Late")
    monkeypatch.setattr(
        work_items,
        "ensure_for_session",
        lambda *_args, **_kwargs: pytest.fail("GET /api/board must be read-only"),
    )

    _status, board = _get_json(base + "/api/board")

    assert board["tasks"] == []
    assert work_items.get_for_session("late-runtime") is None


def test_board_normalizes_today_states_actions_and_order(web_server, monkeypatch):
    base, _server = web_server
    monkeypatch.setattr(web, "_provider_available", lambda _provider: True)
    today = date.today()
    _board_thread("quiet", cwd=tempfile.gettempdir(), name="Quiet")
    overdue = _board_thread("overdue", cwd=tempfile.gettempdir(), name="Overdue")
    _board_thread("attention", cwd=tempfile.gettempdir(), name="Attention")
    future = _board_thread("future", cwd=tempfile.gettempdir(), name="Future")
    closed = _board_thread("closed", cwd=tempfile.gettempdir(), name="Closed")
    work_items.mutate(overdue["task_id"], due_date=(today - timedelta(days=1)).isoformat())
    work_items.mutate(future["task_id"], due_date=(today + timedelta(days=1)).isoformat())
    work_items.mutate(closed["task_id"], status="done")
    store.upsert("attention", state="needs-input")
    store.heartbeat("attention")

    _status, board = _get_json(base + "/api/board")
    tasks = {task["session_id"]: task for task in board["tasks"]}

    assert [task["session_id"] for task in board["tasks"]][:2] == ["attention", "overdue"]
    assert tasks["attention"]["terminal_state"] == "needs-input"
    assert tasks["attention"]["work_status"] == "active"
    assert tasks["attention"]["in_today"] is True
    assert tasks["overdue"]["in_today"] is True
    assert tasks["quiet"]["in_today"] is False
    assert tasks["future"]["in_today"] is False
    assert tasks["closed"]["in_today"] is False
    assert set(tasks["quiet"]["actions"]) == {"claude", "codex"}
    assert tasks["quiet"]["actions"]["claude"]["label"] == "Resume Claude"
    assert tasks["quiet"]["actions"]["codex"]["label"] == "Continue in CodeX"
    assert tasks["quiet"]["actions"]["codex"]["available"] is False
    assert "transcript" in tasks["quiet"]["actions"]["codex"]["reason"].lower()


def test_closing_row_acknowledges_and_publishes_only_after_commit(web_server, monkeypatch):
    base, _server = web_server
    item = _board_thread("close-http", cwd=tempfile.gettempdir(), name="Close")
    store.upsert(
        "close-http", state="idle", done_at=10, acked_at=None, pending_alert="done"
    )
    observed = []

    def capture(session_id):
        runtime = store.get(session_id)
        observed.append((session_id, runtime["acked_at"], runtime["sync_revision"]))

    monkeypatch.setattr(hook, "_spawn_sync", capture)
    _status, payload = _mutate_json(
        base + f"/api/tasks/{item['task_id']}", "PATCH", {"status": "archived"}
    )

    assert payload["task"]["work_status"] == "archived"
    assert observed and observed[0][0] == "close-http"
    assert observed[0][1] >= 10
    assert observed[0][2] == 1


def test_manual_create_and_attention_ack_routes_are_removed(web_server):
    base, _server = web_server
    for path in ("/api/tasks", "/api/sessions/home1/ack"):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _mutate_json(base + path, "POST", {})
        assert exc_info.value.code == 404


def test_mutations_require_csrf_token_and_valid_json(web_server):
    base, _server = web_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _mutate_json(base + "/api/tasks", "POST", {"title": "nope"}, token="wrong")
    assert exc_info.value.code == 403

    req = urllib.request.Request(
        base + "/api/tasks",
        data=b"title=nope",
        method="POST",
        headers={"Content-Type": "text/plain", "X-Agent-Board-Token": "test-token"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=10)
    assert exc_info.value.code == 415


def test_launch_is_server_built_and_rejects_missing_project(web_server, monkeypatch):
    base, _server = web_server
    opened = []
    monkeypatch.setattr(web.commands, "open_in_terminal", opened.append)
    monkeypatch.setattr(web.launches, "_available", lambda provider: True)
    _board_thread("launchable", cwd=tempfile.gettempdir(), provider="claude", name="Start here")
    _status, board = _get_json(base + "/api/board")
    task_id = board["tasks"][0]["task_id"]
    body = {"provider": "claude", "full_access": True, "launch_revision": board["tasks"][0]["launch_revision"]}
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _mutate_json(
            base + f"/api/tasks/{task_id}/launch", "POST", {**body, "command": "echo unwanted"}
        )
    assert exc_info.value.code == 400
    assert not opened
    _status, launched = _mutate_json(
        base + f"/api/tasks/{task_id}/launch",
        "POST",
        body,
    )
    assert launched == {"ok": True}
    assert "launch-intent" in opened[0]
    assert "launchable" not in opened[0]

    _board_thread("missing", cwd="/definitely/not/here", name="Missing")
    _status, board = _get_json(base + "/api/board")
    missing = next(task for task in board["tasks"] if task["session_id"] == "missing")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _mutate_json(
            base + f"/api/tasks/{missing['task_id']}/launch", "POST",
            {"provider": "claude", "full_access": False, "launch_revision": missing["launch_revision"]},
        )
    assert exc_info.value.code == 400


@pytest.mark.parametrize("provider", (None, 7, False, "", "cursor"))
def test_task_launch_requires_explicit_valid_provider(web_server, monkeypatch, provider):
    base, _server = web_server
    opened = []
    monkeypatch.setattr(web.commands, "open_in_terminal", opened.append)
    _board_thread("explicit-provider", cwd=tempfile.gettempdir(), name="Explicit")
    _status, board = _get_json(base + "/api/board")
    task = board["tasks"][0]
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _mutate_json(
            base + f"/api/tasks/{task['task_id']}/launch",
            "POST",
            {"provider": provider, "full_access": False, "launch_revision": task["launch_revision"]},
        )
    assert exc_info.value.code == 400
    assert opened == []


def test_stale_client_edit_revision_cannot_overwrite_newer_value(web_server):
    base, _server = web_server
    _board_thread("revisioned", cwd=tempfile.gettempdir(), name="Original")
    _status, board = _get_json(base + "/api/board")
    task = next(task for task in board["tasks"] if task["session_id"] == "revisioned")
    path = base + f"/api/tasks/{task['task_id']}"

    _mutate_json(
        path,
        "PATCH",
        {"title": "Newest", "_edit_client": "browser-tab", "_edit_revision": 2},
    )
    _status, stale = _mutate_json(
        path,
        "PATCH",
        {"title": "Stale", "_edit_client": "browser-tab", "_edit_revision": 1},
    )

    assert stale["task"]["title"] == "Newest"


@pytest.mark.parametrize(
    "payload",
    ({}, {"full_access": None}, {"full_access": "false"}, {"full_access": 0}),
)
def test_launch_rejects_non_boolean_full_access(web_server, monkeypatch, payload):
    base, _server = web_server
    monkeypatch.setattr(web.commands, "open_in_terminal", lambda _command: None)
    _board_thread("typed", cwd=tempfile.gettempdir(), name="Typed launch")
    _status, board = _get_json(base + "/api/board")
    task = board["tasks"][0]
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _mutate_json(
            base + f"/api/tasks/{task['task_id']}/launch",
            "POST",
            payload,
        )
    assert exc_info.value.code == 400


@pytest.mark.parametrize("full_access", (None, "true", 1))
def test_history_launch_rejects_non_boolean_full_access(
    web_server, monkeypatch, full_access
):
    base, _server = web_server
    monkeypatch.setattr(web, "_provider_available", lambda _provider: True)
    monkeypatch.setattr(web.commands, "open_in_terminal", lambda _command: None)
    conn = fts.open_db(fts.DB_PATH)
    conn.execute("UPDATE sessions SET cwd = ? WHERE sid = 'home1'", (tempfile.gettempdir(),))
    conn.commit()
    conn.close()

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _mutate_json(
            base + "/api/sessions/home1/launch",
            "POST",
            {"provider": "claude", "full_access": full_access},
        )
    assert exc_info.value.code == 400


def test_hook_only_launch_actions_use_captured_transcript_without_fts(
    web_server, tmp_path, monkeypatch
):
    base, _server = web_server
    monkeypatch.setattr(web, "_provider_available", lambda _provider: True)
    transcript = tmp_path / "hook-only.jsonl"
    transcript.write_text('{"message":{"role":"user","content":"continue me"}}\n')
    _board_thread("hook-only", cwd=str(tmp_path), provider="claude", name="Hook only")
    store.upsert("hook-only", transcript_path=str(transcript))

    _status, board = _get_json(base + "/api/board")
    task = next(row for row in board["tasks"] if row["session_id"] == "hook-only")

    assert task["actions"]["claude"]["available"] is True
    assert task["actions"]["codex"]["available"] is True


def test_history_launch_actions_report_scoped_prerequisite_failures(
    web_server, monkeypatch
):
    base, _server = web_server
    monkeypatch.setattr(web, "_provider_available", lambda provider: provider == "claude")

    _status, detail = _get_json(base + "/api/session/home1")

    assert detail["meta"]["actions"]["claude"]["available"] is False
    assert "directory" in detail["meta"]["actions"]["claude"]["reason"].lower()
    assert detail["meta"]["actions"]["codex"]["available"] is False


def test_history_launch_uses_canonical_task_intent_and_preserves_history(
    web_server, tmp_path, monkeypatch
):
    base, _server = web_server
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    transcript = tmp_path / "home1.jsonl"
    transcript.write_text('{"message":{"role":"user","content":"continue"}}\n')
    store.upsert("home1", cwd=str(source), provider="claude", transcript_path=str(transcript))
    task = work_items.ensure_for_session(store.get("home1"))
    space = workspace.snapshot()["spaces"][0]
    listing = workspace.create_list("Destination", space["space_id"], working_directory=str(destination))
    workspace.move_task(task["task_id"], listing["list_key"], task["project_key"])
    monkeypatch.setattr(web, "_provider_available", lambda _provider: True)
    opened = []
    monkeypatch.setattr(web.commands, "open_in_terminal", opened.append)

    _, detail = _get_json(base + "/api/session/home1")
    launch = detail["meta"]["task_launch"]
    assert launch["task_id"] == task["task_id"]
    assert launch["session_id"] == "home1"
    assert launch["working_directory"] == str(destination)
    assert launch["actions"]["codex"]["available"] is True

    body = {"provider": "codex", "full_access": False, "launch_revision": launch["launch_revision"]}
    _, result = _mutate_json(base + "/api/sessions/home1/launch", "POST", body)
    assert result == {"ok": True}
    token = shlex.split(opened[0])[-1]
    launches.claim(token)
    monkeypatch.setenv(launches.TOKEN_ENV, token)
    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "continued-home1", "cwd": str(destination)}, "codex")

    assert [item["task_id"] for item in work_items.list_items()] == [task["task_id"]]
    assert [item["session_id"] for item in work_items.get_session_history(task["task_id"])] == ["home1", "continued-home1"]

    for invalid in ({"provider": "codex", "full_access": False}, {**body, "launch_revision": "stale"}):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _mutate_json(base + "/api/sessions/home1/launch", "POST", invalid)
        assert exc_info.value.code == 400


def test_hook_only_thread_can_be_read_without_search_index(web_server, tmp_path, monkeypatch):
    base, _server = web_server
    transcript = tmp_path / "new-thread.jsonl"
    transcript.write_text('{"message":{"role":"user","content":"Fresh conversation"}}\n')
    item = _board_thread("fresh-reader", cwd=str(tmp_path), name="Renamed task")
    store.upsert("fresh-reader", transcript_path=str(transcript))
    monkeypatch.setattr(web, "_provider_available", lambda _provider: True)
    monkeypatch.setattr(fts, "open_db", lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("rebuilding")))

    status, detail = _get_json(base + "/api/session/fresh-reader")

    assert status == 200
    assert detail["turns"] == [{"role": "user", "text": "Fresh conversation"}]
    assert detail["meta"]["title"] == item["title"]
    assert detail["meta"]["msg_count"] == 1
    assert detail["meta"]["actions"]["codex"]["available"] is True


def test_known_missing_transcript_keeps_metadata_and_native_action(web_server, tmp_path, monkeypatch):
    base, _server = web_server
    _board_thread("lost-reader", cwd=str(tmp_path), name="Keep this task")
    monkeypatch.setattr(web, "_provider_available", lambda _provider: True)

    status, detail = _get_json(base + "/api/session/lost-reader")

    assert status == 200
    assert detail["meta"]["title"] == "Keep this task"
    assert detail["turns"] == []
    assert "not available on this Mac" in detail["transcript_error"]
    assert detail["meta"]["actions"]["claude"]["available"] is True
    assert detail["meta"]["actions"]["codex"]["available"] is False


def test_project_alias_folder_roundtrip_does_not_move_repository(web_server):
    base, _server = web_server
    item = _board_thread("organized", cwd="/w/keep-repository")
    _, created = _mutate_json(base + "/api/folders", "POST", {"name": "Products"})
    folder_id = created["folder"]["folder_id"]
    project_url = base + "/api/projects/" + urllib.parse.quote(item["project_key"], safe="")
    _, changed = _mutate_json(project_url, "PATCH", {"display_name": "Agent tools", "folder_id": folder_id})
    assert changed["project"]["name"] == "Agent tools"
    assert changed["project"]["path"] == item["project_path"]
    _, board = _get_json(base + "/api/board")
    assert board["folders"][0]["folder_id"] == folder_id
    assert board["projects"][0]["folder_id"] == folder_id
    assert board["tasks"][0]["project_name"] == "Agent tools"
    assert board["tasks"][0]["session_cwd"] == item["session_cwd"]
    _, renamed = _mutate_json(base + "/api/folders/" + folder_id, "PATCH", {"name": "Studio"})
    assert renamed["folder"]["name"] == "Studio"
    _, reordered = _mutate_json(base + "/api/folders/reorder", "POST", {"folder_ids": [folder_id]})
    assert reordered["folders"][0]["name"] == "Studio"
    _, unfiled = _mutate_json(project_url, "PATCH", {"folder_id": None})
    assert unfiled["project"]["folder_id"] is None


@pytest.mark.parametrize("route,method,body", [
    ("/api/folders", "POST", {"name": "Blocked"}),
    ("/api/folders/reorder", "POST", {"folder_ids": []}),
    ("/api/folders/unknown", "PATCH", {"name": "Blocked"}),
])
def test_folder_routes_require_csrf(web_server, route, method, body):
    base, _server = web_server
    with pytest.raises(urllib.error.HTTPError) as error:
        _mutate_json(base + route, method, body, token="wrong")
    assert error.value.code == 403


def test_server_can_reuse_a_local_port_without_widening_host(monkeypatch):
    addresses = []

    class BoundAddress(Exception):
        pass

    def bind(address, handler):
        addresses.append(address)
        raise BoundAddress

    monkeypatch.setattr(web, "ThreadingHTTPServer", bind)
    with pytest.raises(BoundAddress):
        web.run_server("/tmp", port=51444)
    assert addresses == [("127.0.0.1", 51444)]

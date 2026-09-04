"""Tests for the --web local viewer: HTTP routes, scoping, and error paths.

Drives a real ThreadingHTTPServer (as run_server constructs it) over the
loopback with urllib, against a seeded temp FTS db, so the full
request->sqlite->JSON path is exercised rather than handler internals.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from claude_browse import fts, web
from claude_browse.board import hook, store, work_items


def _seed(conn, sid, *, cwd="/w/home", provider="claude", path="", title=None):
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
            f"first message for {sid}",
            "",
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
            assert len(resp.read()) > 0


def test_automatic_thread_update_and_board_roundtrip(web_server):
    base, _server = web_server
    _board_thread("automatic", provider="codex", name="Ship the work queue")
    _status, board = _get_json(base + "/api/board")
    task = board["tasks"][0]
    assert task["title"] == "Ship the work queue"
    assert task["due_date"] is None
    assert "codex" in task["full_command"]
    assert "--dangerously-bypass-approvals-and-sandbox" in task["full_command"]

    _status, updated = _mutate_json(
        base + "/api/tasks/" + task["task_id"],
        "PATCH",
        {"title": "Ship it", "status": "active", "due_date": None},
    )
    assert updated["task"]["title"] == "Ship it"
    assert updated["task"]["work_status"] == "active"
    assert updated["task"]["due_date"] is None

    _status, board = _get_json(base + "/api/board")
    assert [item["title"] for item in board["tasks"]] == ["Ship it"]


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
    work_items.update(overdue["task_id"], due_date=(today - timedelta(days=1)).isoformat())
    work_items.update(future["task_id"], due_date=(today + timedelta(days=1)).isoformat())
    work_items.update(closed["task_id"], status="done")
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
    assert tasks["quiet"]["actions"]["claude"]["label"] == "Resume"
    assert tasks["quiet"]["actions"]["codex"]["label"] == "Continue in Codex"
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
    _board_thread("launchable", cwd=tempfile.gettempdir(), provider="claude", name="Start here")
    _status, board = _get_json(base + "/api/board")
    task_id = board["tasks"][0]["task_id"]
    _status, launched = _mutate_json(
        base + f"/api/tasks/{task_id}/launch",
        "POST",
        {"provider": "claude", "full_access": True, "command": "rm -rf /"},
    )
    assert launched["command"] == opened[0]
    assert "rm -rf" not in opened[0]
    assert "claude" in opened[0]
    assert "--dangerously-skip-permissions" in opened[0]

    _board_thread("missing", cwd="/definitely/not/here", name="Missing")
    _status, board = _get_json(base + "/api/board")
    missing = next(task for task in board["tasks"] if task["session_id"] == "missing")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _mutate_json(
            base + f"/api/tasks/{missing['task_id']}/launch", "POST", {}
        )
    assert exc_info.value.code == 400


def test_launch_rejects_non_boolean_full_access(web_server, monkeypatch):
    base, _server = web_server
    monkeypatch.setattr(web.commands, "open_in_terminal", lambda _command: None)
    _board_thread("typed", cwd=tempfile.gettempdir(), name="Typed launch")
    _status, board = _get_json(base + "/api/board")
    task = board["tasks"][0]
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _mutate_json(
            base + f"/api/tasks/{task['task_id']}/launch",
            "POST",
            {"full_access": "false"},
        )
    assert exc_info.value.code == 400

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
from http.server import ThreadingHTTPServer

import pytest

from claude_browse import fts, web


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

    server = ThreadingHTTPServer(("127.0.0.1", 0), web._Handler)
    server.launch_cwd = "/w/home"
    server.cwd_filter = None
    server.folder_prefixes = ()
    server.session_limit = 100
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    yield base, server

    server.shutdown()
    server.server_close()
    if os.path.exists(db_path):
        os.unlink(db_path)


def _get_json(url, host=None):
    req = urllib.request.Request(url)
    if host is not None:
        req.add_header("Host", host)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


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
    assert data == {"here_only_forced": False}

    server.cwd_filter = "/w/home"
    _status, data = _get_json(base + "/api/meta")
    assert data == {"here_only_forced": True}
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
            assert len(resp.read()) > 0

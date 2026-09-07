import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_browse.board.relay import RelayWorker, allowed


def test_allowlist_rejects_urls_traversal_and_unknown_operations():
    assert allowed("GET", "/api/meta")
    assert allowed("PATCH", "/api/workspace/lists/repo%3Agithub.com%2Fowner%2Frepo")
    assert not allowed("GET", "/api/session/%2e%2e")
    assert allowed("POST", "/api/tasks/abc/start")
    assert allowed("PATCH", "/api/workspace/lists/today")
    assert not allowed("GET", "http://127.0.0.1:51444/api/meta")
    assert not allowed("GET", "/api/../meta")
    assert not allowed("DELETE", "/api/tasks/x")
    assert not allowed("POST", "/api/anything")


class _Handler(BaseHTTPRequestHandler):
    seen = None

    def log_message(self, *_args):
        pass

    def do_POST(self):
        type(self).seen = (
            self.path,
            self.headers.get("X-Agent-Board-Token"),
            self.rfile.read(int(self.headers["Content-Length"])),
        )
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()


def test_forwarding_preserves_mutation_token_and_body(local_server):
    worker = RelayWorker("mac", "x" * 32, local_server.server_address[1])
    status, content_type, result = worker._forward(
        "POST", "/api/tasks/x/start", b'{"a":1}', "secret"
    )
    assert (status, content_type, result) == (201, "application/json", b'{"ok":true}')
    assert _Handler.seen == ("/api/tasks/x/start", "secret", b'{"a":1}')


class _Socket:
    def __init__(self):
        self.messages = []

    async def send(self, value):
        self.messages.append(json.loads(value))


def test_duplicate_mutation_is_not_forwarded_twice():
    worker = RelayWorker("mac", "x" * 32)
    calls = []
    worker._forward = lambda *_args: calls.append(1) or (200, "application/json", b"{}")

    async def exercise():
        ws = _Socket()
        request = {
            "id": "once",
            "method": "POST",
            "path": "/api/tasks/x/start",
            "body": {},
            "token": "csrf",
            "expires_at": time.time() + 5,
        }
        await worker.process(ws, request)
        await worker.process(ws, request)

    import asyncio

    asyncio.run(exercise())
    assert calls == [1]


def test_token_file_requires_0600(tmp_path):
    from claude_browse.board.relay import read_token

    path = tmp_path / "token"
    path.write_text("x" * 32)
    os.chmod(path, 0o644)
    with pytest.raises(ValueError):
        read_token(str(path))
    os.chmod(path, 0o600)
    assert read_token(str(path)) == "x" * 32

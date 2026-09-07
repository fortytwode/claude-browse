"""Outbound, bounded relay from Mission Control to a running local board."""

from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import json
import math
import os
import re
import stat
import time
from collections import deque
from contextlib import contextmanager
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BODY = 64 * 1024
MAX_FRAME = 16 * 1024 * 1024
CHUNK_SIZE = 480 * 1024
_LOCK = os.path.expanduser("~/.claude/agent-board/relay.lock")
_ID = r"[^/?#]+"


def allowed(method: str, path: str) -> bool:
    """Strict API allowlist; never permits an arbitrary URL."""
    method, parsed = method.upper(), urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not path.startswith("/"):
        return False
    clean = parsed.path
    if any(unquote(part) in (".", "..") for part in clean.split("/")):
        return False
    if "//" in clean or "\\" in clean or any(x in ("", ".", "..") for x in clean.split("/")[1:]):
        return False
    if method == "GET":
        return bool(
            re.fullmatch(
                rf"/api/(?:meta|board|sessions|session/{_ID}|tasks/{_ID}/history|workspace(?:/{_ID})*|projects(?:/{_ID})*|folders(?:/{_ID})*)",
                clean,
            )
        )
    if method == "POST":
        return bool(
            re.fullmatch(
                rf"/api/(?:tasks/(?:reorder|{_ID}/(?:launch|focus|start))|sessions/{_ID}/launch|workspace/(?:spaces|folders|lists|reorder|tasks/reorder|lists/{_ID}/(?:launch|directory)|tasks/{_ID}/move)|projects/reorder|folders(?:/reorder)?)",
                clean,
            )
        )
    if method == "PATCH":
        return bool(
            re.fullmatch(
                rf"/api/(?:tasks|workspace/(?:spaces|folders|lists)|projects|folders)/{_ID}", clean
            )
        )
    return False


def read_token(path: str) -> str:
    """Read a secret only from a private, non-symlink regular file."""
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("relay token file must have mode 0600")
    with open(path, encoding="utf-8") as f:
        token = f.read().strip()
    if len(token) < 32:
        raise ValueError("relay token is unexpectedly short")
    return token


def expired(value: Any) -> bool:
    try:
        return not math.isfinite(float(value)) or float(value) <= time.time()
    except (TypeError, ValueError):
        return True


@contextmanager
def _single_worker():
    os.makedirs(os.path.dirname(_LOCK), exist_ok=True)
    with open(_LOCK, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another agent-board relay is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RelayWorker:
    def __init__(self, machine_id: str, token: str, port: int = 51444):
        if not machine_id or len(machine_id) > 128:
            raise ValueError("machine_id is required")
        self.machine_id, self.token, self.port = machine_id, token, port
        self.seen, self.seen_order = set(), deque(maxlen=4096)
        self.send_lock, self.limit = asyncio.Lock(), asyncio.Semaphore(4)
        self.tasks: set[asyncio.Task] = set()

    def _forward(self, method: str, path: str, body: bytes, token: str) -> tuple[int, str, bytes]:
        headers = {
            "Host": f"127.0.0.1:{self.port}",
            "Origin": f"http://127.0.0.1:{self.port}",
            "Accept": "application/json",
        }
        if method != "GET":
            headers.update({"Content-Type": "application/json", "X-Agent-Board-Token": token})
        req = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body if method != "GET" else None,
            headers=headers,
            method=method,
        )

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *_args):
                return None

        try:
            response = build_opener(NoRedirect).open(req, timeout=20)
        except HTTPError as exc:
            response = exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ConnectionError("local agent board is unavailable") from exc
        with response:
            payload = response.read(MAX_FRAME + 1)
            if len(payload) > MAX_FRAME:
                raise ValueError("local response exceeds relay limit")
            return response.status, response.headers.get_content_type(), payload

    async def _send(self, ws: Any, message: dict) -> None:
        async with self.send_lock:
            await ws.send(json.dumps(message, separators=(",", ":")))

    def _reserve(self, request_id: str) -> bool:
        if request_id in self.seen:
            return False
        if len(self.seen_order) == self.seen_order.maxlen:
            self.seen.remove(self.seen_order.popleft())
        self.seen.add(request_id)
        self.seen_order.append(request_id)
        return True

    async def process(self, ws: Any, message: dict) -> None:
        request_id = message.get("id")
        method = str(message.get("method", "")).upper()
        path = message.get("path", "")
        token = message.get("token", "")
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 128
            or not self._reserve(request_id)
        ):
            return
        try:
            body = (
                json.dumps(message.get("body", {}), separators=(",", ":")).encode()
                if method != "GET"
                else b""
            )
        except (TypeError, ValueError):
            body = b"x" * (MAX_BODY + 1)
        # The server also enforces this. Check again immediately before the
        # loopback write so a queued mutation cannot execute after its lease.
        if (
            not isinstance(path, str)
            or expired(message.get("expires_at"))
            or not allowed(method, path)
            or len(body) > MAX_BODY
            or (method != "GET" and not isinstance(token, str))
        ):
            await self._send(
                ws, {"type": "response_error", "id": request_id, "error": "invalid relay request"}
            )
            return
        async with self.limit:
            try:
                if expired(message.get("expires_at")):
                    await self._send(
                        ws,
                        {
                            "type": "response_error",
                            "id": request_id,
                            "error": "relay request expired",
                        },
                    )
                    return
                status, ctype, payload = await asyncio.to_thread(
                    self._forward, method, path, body, token
                )
                await self._send(
                    ws,
                    {
                        "type": "response_start",
                        "id": request_id,
                        "status": status,
                        "content_type": ctype,
                    },
                )
                for offset in range(0, len(payload), CHUNK_SIZE):
                    await self._send(
                        ws,
                        {
                            "type": "response_chunk",
                            "id": request_id,
                            "index": offset // CHUNK_SIZE,
                            "data": base64.b64encode(payload[offset : offset + CHUNK_SIZE]).decode(
                                "ascii"
                            ),
                        },
                    )
                await self._send(
                    ws,
                    {
                        "type": "response_end",
                        "id": request_id,
                        "chunks": (len(payload) + CHUNK_SIZE - 1) // CHUNK_SIZE,
                    },
                )
            except ConnectionError:
                await self._send(
                    ws,
                    {
                        "type": "response_error",
                        "id": request_id,
                        "error": "local board unavailable",
                    },
                )
            except Exception:
                await self._send(
                    ws, {"type": "response_error", "id": request_id, "error": "relay failed"}
                )

    async def run(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "wss" and not (
            parsed.scheme == "ws" and parsed.hostname in {"127.0.0.1", "localhost"}
        ):
            raise ValueError("relay requires TLS except for loopback tests")
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            try:
                from websockets import connect
            except ImportError:
                raise RuntimeError("relay requires the websockets package") from None
        try:
            async with connect(
                url, max_size=MAX_FRAME, open_timeout=20, ping_interval=20, ping_timeout=20
            ) as ws:
                await self._send(
                    ws, {"type": "authenticate", "token": self.token, "machine_id": self.machine_id}
                )
                ready = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if ready.get("type") != "ready":
                    raise RuntimeError("Mission Control rejected relay authentication")
                async for raw in ws:
                    if isinstance(raw, bytes) or len(raw) > MAX_FRAME:
                        raise RuntimeError("invalid relay frame")
                    message = json.loads(raw)
                    if isinstance(message, dict) and message.get("type") == "request":
                        if len(self.tasks) >= 64:
                            raise RuntimeError("too many outstanding relay requests")
                        task = asyncio.create_task(self.process(ws, message))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
        finally:
            tasks = list(self.tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Relay a local agent board through Mission Control"
    )
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--port", type=int, default=51444)
    parser.add_argument(
        "--url", default="wss://missioncontrol.rocketshiphq.com/tools/agent-board/relay"
    )
    args = parser.parse_args(argv)
    with _single_worker():
        asyncio.run(
            RelayWorker(args.machine_id, read_token(args.token_file), args.port).run(args.url)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Persist manual names in local provider metadata, without agent input.

``rename_session`` returns ``{"updated": bool, "reason": str}``. Success
means the provider's durable name was saved, not that a running agent refreshed
its in-memory UI. Terminal custom titles are a separate caller responsibility.

Claude uses the documented SDK custom-title event format (the SDK is optional
and is not installed here). Append one bounded record; never replace/truncate a
transcript or try to repair an incomplete record. Active Claude processes can
retain/re-emit an older in-memory title, so this cannot promise a live UI rename.

Codex owns session_index.jsonl (id/thread_name/updated_at) and its SQLite title
metadata. Use native thread/name/set, not direct edits to either store. Prefer
the running daemon's proxy so connected clients receive its notification; older
or daemonless installations use a private stdio app-server. Neither path starts
or resumes a thread, submits a turn, or sends terminal keystrokes.
"""

from __future__ import annotations

import fcntl
import json
import os
import selectors
import stat
import subprocess
import threading
import time
from pathlib import Path
from uuid import UUID

from claude_browse.board import store
from claude_browse.providers import claude

_RENAME_LOCK = threading.Lock()
_RPC_TIMEOUT_S = 8.0
_MAX_RECORD_BYTES = 4096
_SCAN_BYTES = 64 * 1024


def rename_session(session_id: str, provider: str, title: str) -> dict[str, object]:
    """Best-effort provider persistence; expected failures never raise.

    Call after committing the manual board name. No board database mutation,
    remote naming API, model inference, or Terminal manipulation occurs here.
    A failed/timeout write may have persisted; callers must not blindly retry.
    """
    if provider not in ("claude", "codex"):
        return {"updated": False, "reason": "This provider does not support local session renaming."}
    try:
        if not isinstance(session_id, str) or str(UUID(session_id)) != session_id:
            raise ValueError
    except (ValueError, AttributeError):
        return {"updated": False, "reason": "The session ID must be a canonical UUID."}
    if (
        not isinstance(title, str) or not title.strip() or len(title) > 500
        or any(not char.isprintable() for char in title)
    ):
        return {"updated": False, "reason": "The session title must be one printable line of 1–500 characters."}
    title = title.strip()
    try:
        with _RENAME_LOCK:
            desired = title
            for _attempt in range(3):
                current = store.get(session_id)
                desired = str((current or {}).get("name") or desired).strip()
                if not desired or len(desired) > 500 or not desired.isprintable():
                    raise ValueError("The latest board title is not a valid provider title.")
                if provider == "claude":
                    _rename_claude(session_id, desired)
                else:
                    _rename_codex(session_id, desired)
                latest = store.get(session_id)
                latest_title = str((latest or {}).get("name") or desired).strip()
                if latest_title == desired:
                    if desired != title:
                        return {"updated": False, "reason": "A newer session title replaced this rename."}
                    return {
                        "updated": True,
                        "reason": (
                            "Saved to Claude history. A running Claude session may retain or "
                            "restore its previous in-memory name until reopened."
                            if provider == "claude" else ""
                        ),
                    }
            return {"updated": False, "reason": "The session title changed repeatedly while the provider was updating."}
    except Exception as exc:
        # Provider/config/transport failures are publication status, never a
        # reason to fail an already-committed manual board edit.
        return {"updated": False, "reason": f"{provider.capitalize()} session name was not confirmed: {exc}"}


def _rename_claude(session_id: str, title: str) -> None:
    root = (
        Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects"
        if os.environ.get("CLAUDE_CONFIG_DIR") else Path(claude.SESSIONS_DIR)
    ).resolve()
    matches = list(root.glob(f"*/{session_id}.jsonl"))
    if len(matches) != 1:
        raise ValueError("No unique local Claude transcript was found.")
    path = matches[0]
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("The Claude transcript is outside the local project store.")
    record = (json.dumps(
        {"type": "custom-title", "customTitle": title, "sessionId": session_id},
        ensure_ascii=True, separators=(",", ":"),
    ) + "\n").encode("utf-8")
    if len(record) > _MAX_RECORD_BYTES:
        raise ValueError("The encoded title is too large for a single metadata record.")
    # No O_CREAT: an unknown/deleted session must never become a ghost transcript.
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("The Claude transcript is not a regular file.")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        head = os.pread(fd, _SCAN_BYTES, 0)
        identity = None
        for line in head.splitlines():
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(entry, dict) and entry.get("sessionId"):
                identity = entry["sessionId"]
                break
        if identity != session_id:
            raise ValueError("The Claude transcript's session identity could not be verified.")
        size = os.fstat(fd).st_size
        if not size or os.pread(fd, 1, size - 1) != b"\n":
            raise ValueError("Claude is writing an incomplete record; history was left unchanged.")
        # O_APPEND and one write preserve concurrent complete provider records.
        # flock also serializes cooperating writers. Never retry/truncate a short
        # write: the provider may have appended since then.
        if os.write(fd, record) != len(record):
            raise OSError(
                "Unconfirmed metadata write: a partial title record may remain. "
                "Original transcript bytes were preserved; no retry or truncation was attempted."
            )
        os.fsync(fd)
    finally:
        os.close(fd)


class _CodexRPC:
    """Small bounded stdio client for provider metadata RPCs only."""

    def __init__(self, command: list[str], deadline: float):
        self.deadline = deadline
        self.buffer = b""
        self.request_id = 0
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def close(self) -> None:
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.process.stdin.close()
        self.process.stdout.close()

    def send(self, value: dict) -> None:
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        self.process.stdin.flush()

    def call(self, method: str, params: dict) -> dict:
        self.request_id += 1
        self.send({"id": self.request_id, "method": method, "params": params})
        while True:
            while b"\n" not in self.buffer:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0 or not self.selector.select(remaining):
                    raise TimeoutError("The Codex naming API timed out; persistence is unconfirmed.")
                chunk = os.read(self.process.stdout.fileno(), _SCAN_BYTES)
                if not chunk:
                    raise RuntimeError("The Codex naming API closed its connection.")
                self.buffer += chunk
                if len(self.buffer) > 2 * 1024 * 1024:
                    raise RuntimeError("The Codex naming API response exceeded its size limit.")
            line, self.buffer = self.buffer.split(b"\n", 1)
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Invalid response from the Codex naming API.")
            if message.get("id") != self.request_id:
                # Ignore notifications; never answer server-initiated requests.
                continue
            if "error" in message:
                error = message["error"]
                detail = error.get("message", "Request rejected") if isinstance(error, dict) else "Request rejected"
                raise RuntimeError(str(detail))
            result = message.get("result")
            if not isinstance(result, dict):
                raise ValueError("Invalid result from the Codex naming API.")
            return result


def _rename_codex(session_id: str, title: str) -> None:
    deadline = time.monotonic() + _RPC_TIMEOUT_S
    rpc = None
    # Only fall back before mutation. An error after name/set may already have
    # persisted and must not dispatch a competing write on another connection.
    for command in (
        ["codex", "app-server", "proxy"],
        ["codex", "app-server", "--listen", "stdio://"],
    ):
        candidate = _CodexRPC(command, deadline)
        try:
            candidate.call("initialize", {
                "clientInfo": {"name": "agent_board_names", "version": "1.0"},
            })
            candidate.send({"method": "initialized"})
        except Exception as exc:
            candidate.close()
            if isinstance(exc, TimeoutError) or command[-1] != "proxy":
                raise
        else:
            rpc = candidate
            break
    if rpc is None:
        raise RuntimeError("The Codex naming API is unavailable.")
    try:
        before = rpc.call("thread/read", {"threadId": session_id, "includeTurns": False})
        thread = before.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != session_id:
            raise ValueError("The Codex session identity could not be verified.")
        rpc.call("thread/name/set", {"threadId": session_id, "name": title})
        after = rpc.call("thread/read", {"threadId": session_id, "includeTurns": False})
        thread = after.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != session_id or thread.get("name") != title:
            raise RuntimeError("Codex did not read back the requested session name.")
    finally:
        rpc.close()

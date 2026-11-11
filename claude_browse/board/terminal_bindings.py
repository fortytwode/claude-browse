"""Host-local interaction evidence from synchronous provider hooks.

Only SessionStart/UserPromptSubmit select a conversation. Stop and other
background events cannot select an older loaded thread. No argv is inspected.
These hooks do not prove silent UI selection changes between cached threads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from claude_browse.board import presence, store


@dataclass(frozen=True)
class Binding:
    provider: str
    pid: int
    tty: str
    started: str
    session_id: str
    path: str
    cwd: str


def _db_path() -> Path:
    # Separate local evidence is never published as a session heartbeat.
    return store._DB_PATH.with_name("terminal-bindings.sqlite3")


def _process(pid: int, deadline: float) -> tuple[int, int, str, str, str] | None:
    output = presence._command(
        ["ps", "-p", str(pid), "-o", "pid=,ppid=,tty=,comm=,lstart="], deadline
    )
    lines = output.splitlines()
    if len(lines) != 1:
        return None
    parts = lines[0].split(maxsplit=4)
    if len(parts) != 5 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    actual, parent, tty, command, started = parts
    if int(actual) != pid:
        return None
    # A missing/unparseable clock can never protect against PID reuse.
    presence.datetime.strptime(started, "%a %b %d %H:%M:%S %Y")
    return pid, int(parent), tty, command, " ".join(started.split())


def _ancestor(provider: str, deadline: float):
    pid = os.getppid()
    seen = set()
    while pid > 1 and pid not in seen and len(seen) < 16:
        seen.add(pid)
        proc = _process(pid, deadline)
        if proc is None:
            return None
        if presence._command_is(proc[3], provider):
            # Never climb past a daemon to a more distant terminal ancestor.
            return proc if presence._terminal_tty(proc[2]) else None
        pid = proc[1]
    return None


def _source(provider: str, sid: str, pid: int, started: str, path: str) -> tuple[str, str] | None:
    if provider == "claude":
        native = presence._claude_sessions_root() / f"{pid}.json"
        with native.open("rb") as handle:
            data = json.loads(handle.read(presence.METADATA_SCAN_BYTES))
        if (
            not isinstance(data, dict) or data.get("sessionId") != sid
            or type(data.get("pid")) is not int or data["pid"] != pid
            or not data.get("procStart")
            or not presence._same_start(data["procStart"], started)
        ):
            return None
        return str(native), str(data.get("cwd") or "")

    native = Path(path)
    if (
        not presence._within_root(native, presence._codex_sessions_root())
        or not native.name.startswith("rollout-") or native.suffix != ".jsonl"
        or presence.codex_provider._session_id_from_path(str(native)) != sid
    ):
        return None
    with native.open("rb") as handle:
        chunk = handle.read(presence.METADATA_SCAN_BYTES)
    for line in chunk.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "session_meta":
            continue
        data = record.get("payload")
        if (
            not isinstance(data, dict) or data.get("id") != sid
            or data.get("thread_source") != "user"
            or not isinstance(data.get("source"), str)
            or data["source"].lower() == "subagent"
        ):
            return None
        # Historical originator/source needn't be CLI: this hook proves that
        # a user root originally created in an IDE is now in a local TUI.
        return str(native), str(data.get("cwd") or "")
    return None


def record_hook(payload: dict, provider: str) -> bool:
    """Best effort; an ordinary hook failure must never break the provider."""
    if (
        provider not in {"claude", "codex"}
        or payload.get("hook_event_name") not in {"SessionStart", "UserPromptSubmit"}
        or payload.get("agent_id") or payload.get("agent_transcript_path")
        or not isinstance(payload.get("session_id"), str) or not payload["session_id"]
    ):
        return False
    try:
        deadline = time.monotonic() + 1.0
        proc = _ancestor(provider, deadline)
        if proc is None:
            return False
        pid, _, tty, _, started = proc
        source = _source(provider, payload["session_id"], pid, started,
                         str(payload.get("transcript_path") or ""))
        if source is None or _process(pid, deadline) != proc:
            return False
        db = _db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db, timeout=0.1)) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS bindings (
                provider TEXT, pid INTEGER, tty TEXT, started TEXT,
                session_id TEXT, path TEXT, cwd TEXT, PRIMARY KEY(provider, pid))""")
            conn.execute("INSERT OR REPLACE INTO bindings VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (provider, pid, tty, started, payload["session_id"], *source))
        presence._clear_cache()
        return True
    except (OSError, ValueError, RuntimeError, TimeoutError, sqlite3.Error,
            presence.subprocess.TimeoutExpired):
        return False


def read_verified(provider: str, deadline: float) -> dict[int, Binding]:
    """Read without creating state; freshly check PID/start/TTY and source."""
    result = {}
    db = _db_path()
    if not db.exists():
        return result
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as conn:
            rows = conn.execute("SELECT * FROM bindings WHERE provider = ?", (provider,)).fetchall()
        for row in rows:
            try:
                binding = Binding(*row)
                proc = _process(binding.pid, deadline)
                if (
                    proc is not None and presence._command_is(proc[3], provider)
                    and presence._terminal_tty(proc[2])
                    and (proc[2], proc[4]) == (binding.tty, binding.started)
                    and _source(provider, binding.session_id, binding.pid,
                                binding.started, binding.path) is not None
                ):
                    result[binding.pid] = binding
            except (OSError, ValueError, RuntimeError, TimeoutError,
                    presence.subprocess.TimeoutExpired):
                continue
    except (OSError, sqlite3.Error):
        pass
    return result

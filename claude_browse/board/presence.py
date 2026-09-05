"""Read-only proof that a board session still owns a local terminal.

Runtime heartbeats are deliberately not input to this module.  A session is
open only when its provider's native process artefacts identify the same live,
terminal-backed conversation on this host.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from claude_browse.providers import codex as codex_provider

Presence = Literal["open", "closed", "unknown"]

CACHE_TTL_S = 5.0
COMMAND_TIMEOUT_S = 1.0
TOTAL_SCAN_TIMEOUT_S = 3.0
METADATA_SCAN_BYTES = 64 * 1024


@dataclass(frozen=True)
class _Scan:
    """Result of one complete provider-root scan.

    ``uncertain`` is intentionally separate from ``open``: a descriptor that
    almost matches is evidence that guessing closed would be unsafe.
    """

    complete: bool
    open_ids: frozenset[str] = frozenset()
    uncertain_ids: frozenset[str] = frozenset()
    live: tuple[_LiveSession, ...] = ()


@dataclass(frozen=True)
class _LiveSession:
    """A provider-native, terminal-backed conversation safe to enroll."""

    session_id: str
    provider: str
    cwd: str
    path: str | None = None


_cache: dict[tuple[str, str], tuple[float, _Scan]] = {}


def _hostname() -> str:
    return socket.gethostname()


def _claude_sessions_root() -> Path:
    return Path.home() / ".claude" / "sessions"


def _codex_sessions_root() -> Path:
    # Read at call time so provider configuration/test fixtures remain the
    # source of truth rather than duplicating a stale home-directory constant.
    return Path(codex_provider.CODEX_SESSIONS_DIR)


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed native inspection commands
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _clear_cache() -> None:
    """Test-only cache reset; production refreshes through the TTL."""
    _cache.clear()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("presence scan deadline elapsed")
    return min(COMMAND_TIMEOUT_S, remaining)


def _command(args: list[str], deadline: float) -> str:
    result = _run(args, _remaining(deadline))
    if result.returncode != 0:
        raise RuntimeError(f"native inspection failed: {args[0]}")
    return result.stdout


def _claude_pid_command(pid: int, deadline: float) -> str | None:
    """Inspect one Claude PID, distinguishing macOS's absent-PID response."""
    result = _run(
        ["ps", "-p", str(pid), "-o", "pid=", "-o", "tty=", "-o", "comm=", "-o", "lstart="],
        _remaining(deadline),
    )
    # `ps -p` on macOS reports an otherwise normal, absent selected PID as
    # exit 1 with no output.  Do not generalize this exception to other
    # commands or non-empty error output: those leave the scan incomplete.
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return None
    if result.returncode != 0:
        raise RuntimeError("native inspection failed: ps")
    return result.stdout


def _terminal_tty(value: str) -> bool:
    return bool(value and value not in {"?", "??", "-"})


def _command_is(command: str, expected: str) -> bool:
    return Path(command).name == expected


def _parse_ps_line(line: str, fields: int) -> tuple[str, ...] | None:
    values = line.strip().split(maxsplit=fields - 1)
    if len(values) != fields:
        return None
    return tuple(values)


def _same_start(recorded: object, observed: str) -> bool:
    if not recorded:
        return True
    recorded_value = " ".join(str(recorded).split())
    observed_value = " ".join(observed.split())
    if recorded_value == observed_value:
        return True
    # Claude's pid artefact currently stores this clock in UTC, while macOS
    # ``ps lstart`` renders local time.  Accept only that host offset, never
    # an arbitrary close-enough timestamp, so PID reuse still fails proof.
    try:
        recorded_dt = datetime.strptime(recorded_value, "%a %b %d %H:%M:%S %Y")
        observed_dt = datetime.strptime(observed_value, "%a %b %d %H:%M:%S %Y")
        offset = datetime.now().astimezone().utcoffset()
    except ValueError:
        return False
    return offset is not None and observed_dt - recorded_dt == offset


def _scan_claude(root: Path, deadline: float) -> _Scan:
    open_ids: set[str] = set()
    uncertain: set[str] = set()
    live: dict[str, _LiveSession] = {}
    try:
        try:
            entries = list(root.iterdir())
        except FileNotFoundError:
            return _Scan(True)
        except OSError:
            return _Scan(False)

        for path in entries:
            if time.monotonic() >= deadline:
                return _Scan(False, frozenset(open_ids), frozenset(uncertain))
            if path.suffix != ".json" or not path.stem.isdecimal():
                continue
            try:
                with path.open("rb") as handle:
                    payload = json.loads(handle.read(METADATA_SCAN_BYTES))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                # The filename carries no session identity, so this makes the
                # scan incomplete rather than declaring unrelated rows closed.
                return _Scan(False, frozenset(open_ids), frozenset(uncertain))
            if not isinstance(payload, dict):
                return _Scan(False, frozenset(open_ids), frozenset(uncertain))
            sid = payload.get("sessionId")
            pid = payload.get("pid")
            if not isinstance(sid, str) or not sid or not isinstance(pid, int):
                return _Scan(False, frozenset(open_ids), frozenset(uncertain))
            if pid != int(path.stem):
                uncertain.add(sid)
                continue
            try:
                output = _claude_pid_command(pid, deadline)
            except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                return _Scan(False, frozenset(open_ids), frozenset(uncertain))
            if output is None:
                continue
            process = next((
                parsed for line in output.splitlines()
                if (parsed := _parse_ps_line(line, 4)) is not None
            ), None)
            if process is None:
                # Missing PID is a completed, unmatched check; a stale
                # metadata file cannot make its former session unknown.
                continue
            actual_pid, tty, command, started = process
            if (
                actual_pid != str(pid)
                or not _terminal_tty(tty)
                or not _command_is(command, "claude")
                or not _same_start(payload.get("procStart"), started)
            ):
                uncertain.add(sid)
                continue
            open_ids.add(sid)
            cwd = payload.get("cwd")
            live[sid] = _LiveSession(
                sid, "claude", cwd if isinstance(cwd, str) else ""
            )
    except (OSError, TimeoutError):
        return _Scan(False, frozenset(open_ids), frozenset(uncertain))
    open_ids.difference_update(uncertain)
    return _Scan(
        True,
        frozenset(open_ids),
        frozenset(uncertain),
        tuple(candidate for sid, candidate in live.items() if sid in open_ids),
    )


def _parse_processes(output: str) -> list[tuple[str, str, str]]:
    processes: list[tuple[str, str, str]] = []
    for line in output.splitlines():
        parsed = _parse_ps_line(line, 3)
        if parsed is not None:
            processes.append((parsed[0], parsed[1], parsed[2]))
    return processes


def _lsof_descriptors(output: str) -> list[tuple[str, str]]:
    """Return ``(access, path)`` pairs from machine-readable lsof output."""
    descriptors: list[tuple[str, str]] = []
    access = ""
    for line in output.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == "f":
            access = ""
        elif field == "a":
            access = value
        elif field == "n" and access:
            descriptors.append((access, value))
    return descriptors


def _within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _codex_descriptor_identity(
    path: Path, deadline: float
) -> tuple[str, bool, _LiveSession | None] | None:
    """Read the first bounded session_meta identity, never transcript text."""
    filename_sid = codex_provider._session_id_from_path(str(path))
    if time.monotonic() >= deadline:
        raise TimeoutError("presence scan deadline elapsed")
    try:
        with path.open("rb") as handle:
            chunk = handle.read(METADATA_SCAN_BYTES)
    except OSError:
        return filename_sid, False, None
    if not path.name.startswith("rollout-"):
        return filename_sid, False, None
    for raw in chunk.splitlines():
        try:
            record = json.loads(raw)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "session_meta":
            continue
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            return filename_sid, False, None
        sid = payload.get("id")
        if not isinstance(sid, str) or sid != filename_sid:
            return filename_sid, False, None
        thread_source = payload.get("thread_source")
        if not isinstance(thread_source, str) or thread_source.lower() == "subagent":
            return sid, False, None
        source = str(payload.get("source") or "").strip().lower()
        originator = str(payload.get("originator") or "").strip().lower()
        user_root = (
            thread_source.strip().lower() == "user"
            and source == "cli"
            and originator == "codex-tui"
        )
        cwd = payload.get("cwd")
        enrollment = (
            _LiveSession(sid, "codex", cwd if isinstance(cwd, str) else "", str(path))
            if user_root else None
        )
        return sid, True, enrollment
    return filename_sid, False, None


def _scan_codex(root: Path, deadline: float) -> _Scan:
    open_counts: dict[str, int] = {}
    uncertain: set[str] = set()
    live: dict[str, _LiveSession] = {}
    try:
        output = _command(["ps", "-axo", "pid=,tty=,comm="], deadline)
        processes = _parse_processes(output)
    except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
        return _Scan(False)

    for pid, tty, command in processes:
        if time.monotonic() >= deadline:
            return _Scan(False, frozenset(open_counts), frozenset(uncertain))
        if not _terminal_tty(tty) or not _command_is(command, "codex"):
            continue
        try:
            lsof = _command(["lsof", "-n", "-P", "-Ffan", "-p", pid], deadline)
        except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
            return _Scan(False, frozenset(open_counts), frozenset(uncertain))
        for access, raw_path in _lsof_descriptors(lsof):
            if time.monotonic() >= deadline:
                return _Scan(False, frozenset(open_counts), frozenset(uncertain))
            path = Path(raw_path)
            if path.suffix != ".jsonl" or not _within_root(path, root):
                continue
            try:
                identity = _codex_descriptor_identity(path, deadline)
            except TimeoutError:
                return _Scan(False, frozenset(open_counts), frozenset(uncertain))
            if identity is None:
                continue
            sid, valid_identity, enrollment = identity
            if access not in {"w", "u"} or not valid_identity:
                uncertain.add(sid)
                continue
            open_counts[sid] = open_counts.get(sid, 0) + 1
            if enrollment is not None:
                live[sid] = enrollment
    open_ids = {sid for sid, count in open_counts.items() if count == 1 and sid not in uncertain}
    uncertain.update(sid for sid, count in open_counts.items() if count != 1)
    return _Scan(
        True,
        frozenset(open_ids),
        frozenset(uncertain),
        tuple(candidate for sid, candidate in live.items() if sid in open_ids),
    )


def _scan_cached(provider: str, root: Path, deadline: float) -> _Scan:
    key = (provider, str(root.resolve()))
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < CACHE_TTL_S:
        return cached[1]
    # A scan that never starts because another provider used the request's
    # aggregate budget is only unknown for this request, never a cache entry.
    if now >= deadline:
        return _Scan(False)
    scan = _scan_claude(root, deadline) if provider == "claude" else _scan_codex(root, deadline)
    _cache[key] = (now, scan)
    return scan


def snapshot(rows: list[dict]) -> dict[str, Presence]:
    """Return verified local terminal presence for the supplied runtime rows.

    A provider scan happens once per provider/root cache key.  Rows are
    filtered *after* the scan so a cached result never sticks to a prior task
    candidate set.  This function has no store, hook, or provider-state writes.
    """
    result: dict[str, Presence] = {}
    local: dict[str, list[str]] = {"claude": [], "codex": []}
    host = _hostname()
    for row in rows:
        sid = row.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        result[sid] = "unknown"
        provider = row.get("provider") or "claude"
        if (
            isinstance(provider, str)
            and provider in local
            and isinstance(row.get("host"), str)
            and row["host"] == host
        ):
            local[provider].append(sid)

    deadline = time.monotonic() + TOTAL_SCAN_TIMEOUT_S
    for provider, ids in local.items():
        if not ids:
            continue
        root = _claude_sessions_root() if provider == "claude" else _codex_sessions_root()
        scan = _scan_cached(provider, root, deadline)
        for sid in ids:
            if sid in scan.uncertain_ids or not scan.complete:
                result[sid] = "unknown"
            elif sid in scan.open_ids:
                result[sid] = "open"
            else:
                result[sid] = "closed"
    return result


def live_sessions() -> list[dict[str, str]]:
    """Return verified local user conversations from cached native evidence.

    FTS and board runtime rows are deliberately not consulted: they may enrich
    a later task, but cannot establish that a terminal is alive.  Partial
    scans are withheld from enrollment so a failed aggregate scan cannot
    expand persistent board state.
    """
    records: list[dict[str, str]] = []
    deadline = time.monotonic() + TOTAL_SCAN_TIMEOUT_S
    for provider, root in (
        ("claude", _claude_sessions_root()),
        ("codex", _codex_sessions_root()),
    ):
        scan = _scan_cached(provider, root, deadline)
        if not scan.complete:
            continue
        for session in scan.live:
            record = {
                "session_id": session.session_id,
                "provider": session.provider,
                "cwd": session.cwd,
            }
            if session.path:
                record["path"] = session.path
            records.append(record)
    return sorted(records, key=lambda record: (record["provider"], record["session_id"]))

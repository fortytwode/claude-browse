"""CodeX session adapter."""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import urllib.request

from .base import ProviderSpec
from .common import (
    canonicalize_path,
    epoch_ms_to_iso,
    epoch_s_to_iso,
    flatten_text,
    is_noise_text,
    is_substantive_text,
    split_boilerplate,
)

CODEX_STATE_DB = os.path.expanduser("~/.codex/state_5.sqlite")
CODEX_HISTORY_PATH = os.path.expanduser("~/.codex/history.jsonl")
CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
SQLITE_BUSY_TIMEOUT_MS = 30_000
METADATA_SCAN_BYTES = 256 * 1024

_CODEX_HISTORY_CACHE: dict[str, object] = {
    "mtime": None,
    "entries": {},
}
_CODEX_SESSION_TURNS_CACHE: dict[str, object] = {
    "entries": {},
}
_CODEX_ROLLOUT_PREFIX_RE = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T")


def has_local_state() -> bool:
    return (
        os.path.exists(CODEX_STATE_DB)
        or os.path.exists(CODEX_HISTORY_PATH)
        or os.path.isdir(CODEX_SESSIONS_DIR)
    )


def _extract_codex_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if text and part.get("type") in ("input_text", "output_text", "text"):
                parts.append(str(text))
        return "\n".join(parts).strip()
    return ""


def _append_turn(
    turns: list[tuple[str, str]],
    role: str,
    text: str,
    flatten: bool = True,
) -> None:
    # Selection and dedup always judge the flattened form so both modes
    # emit the same turn list; flatten only changes the stored text.
    cleaned = flatten_text(text)
    if len(cleaned) <= 3 or is_noise_text(cleaned):
        return
    emitted = cleaned if flatten else text.strip()
    if turns and turns[-1] == (role, emitted):
        return
    turns.append((role, emitted))


def _load_session_turns(
    session_path: str, flatten: bool = True
) -> list[tuple[str, str]]:
    if not session_path or not os.path.exists(session_path):
        return []

    mtime = os.path.getmtime(session_path)
    cache_key = f"{session_path}:{mtime}:{flatten}"
    cached = _CODEX_SESSION_TURNS_CACHE["entries"].get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    turns: list[tuple[str, str]] = []
    try:
        with open(session_path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                item_type = data.get("type")
                payload = data.get("payload", {})

                if item_type == "event_msg":
                    event_type = payload.get("type")
                    if event_type == "user_message":
                        _append_turn(
                            turns,
                            "user",
                            str(payload.get("message", "")),
                            flatten=flatten,
                        )
                    elif event_type == "agent_message":
                        _append_turn(
                            turns,
                            "assistant",
                            str(payload.get("message", "")),
                            flatten=flatten,
                        )
                    elif event_type == "task_complete":
                        _append_turn(
                            turns,
                            "assistant",
                            str(payload.get("last_agent_message", "")),
                            flatten=flatten,
                        )
                    continue

                if item_type != "response_item":
                    continue

                payload_type = payload.get("type")
                role = payload.get("role")
                if payload_type == "message" and role in ("user", "assistant"):
                    text = _extract_codex_content_text(payload.get("content"))
                    _append_turn(turns, role, text, flatten=flatten)
    except Exception:
        turns = []

    entries = _CODEX_SESSION_TURNS_CACHE["entries"]
    if isinstance(entries, dict):
        entries[cache_key] = turns
    return turns


def list_session_files() -> list[str]:
    pattern = os.path.join(CODEX_SESSIONS_DIR, "**", "*.jsonl")
    return sorted(glob.glob(pattern, recursive=True))


def _session_id_from_path(session_path: str) -> str:
    stem = os.path.splitext(os.path.basename(session_path))[0]
    if _CODEX_ROLLOUT_PREFIX_RE.match(stem):
        parts = stem.split("-")
        if len(parts) >= 6:
            return "-".join(parts[-5:])
    return stem


def _is_sqlite_lock_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _open_state_db(path: str) -> sqlite3.Connection:
    uri_path = urllib.request.pathname2url(os.path.abspath(path))
    conn = sqlite3.connect(
        f"file:{uri_path}?mode=ro",
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        uri=True,
    )
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _load_session_metadata(session_path: str) -> dict[str, object]:
    metadata: dict[str, object] = {
        "session_id": _session_id_from_path(session_path),
        "cwd": "",
        "timestamp": None,
        "last_timestamp": None,
        "thread_source": "",
    }
    try:
        with open(session_path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                timestamp = data.get("timestamp")
                if timestamp:
                    if not metadata.get("timestamp"):
                        metadata["timestamp"] = timestamp
                    metadata["last_timestamp"] = timestamp

                if data.get("type") != "session_meta":
                    continue

                payload = data.get("payload", {})
                if payload.get("id"):
                    metadata["session_id"] = str(payload.get("id"))
                if payload.get("cwd"):
                    metadata["cwd"] = str(payload.get("cwd"))
                if payload.get("timestamp") and not metadata.get("timestamp"):
                    metadata["timestamp"] = payload.get("timestamp")
                if payload.get("thread_source"):
                    metadata["thread_source"] = str(payload.get("thread_source"))
    except Exception:
        return metadata
    return metadata


def _metadata_from_lines(lines: list[bytes], metadata: dict[str, object]) -> None:
    """Populate cheap session identity fields from complete JSONL lines."""
    for raw_line in lines:
        try:
            data = json.loads(raw_line)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        timestamp = data.get("timestamp")
        if timestamp:
            if not metadata.get("timestamp"):
                metadata["timestamp"] = str(timestamp)
            metadata["last_timestamp"] = str(timestamp)
        if data.get("type") == "session_meta":
            payload = data.get("payload") or {}
            if payload.get("id"):
                metadata["session_id"] = str(payload["id"])
            if payload.get("cwd"):
                metadata["cwd"] = str(payload["cwd"])
            if payload.get("thread_source"):
                metadata["thread_source"] = str(payload["thread_source"])
        if not metadata.get("first_msg") and data.get("type") == "event_msg":
            payload = data.get("payload") or {}
            if payload.get("type") == "user_message":
                text = _searchable_body(str(payload.get("message") or ""))
                if text:
                    metadata["first_msg"] = text[:200]


def read_session_metadata(session_path: str) -> dict[str, object]:
    """Return session identity from bounded head/tail reads only.

    This is intentionally separate from ``_load_session_metadata``.  It is
    safe to call while a multi-gigabyte transcript is still growing: the
    first scan finds session_meta/the opening request and the tail supplies
    current activity, while an incomplete trailing line is simply ignored.
    """
    metadata: dict[str, object] = {
        "session_id": _session_id_from_path(session_path),
        "cwd": "",
        "timestamp": None,
        "last_timestamp": None,
        "thread_source": "",
        "first_msg": "",
        "size": 0,
        "mtime": 0.0,
    }
    try:
        with open(session_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            metadata["size"] = size
            metadata["mtime"] = os.path.getmtime(session_path)
            handle.seek(0)
            head = handle.read(METADATA_SCAN_BYTES)
            handle.seek(max(0, size - METADATA_SCAN_BYTES))
            tail = handle.read()
    except OSError:
        return metadata
    _metadata_from_lines(head.splitlines(), metadata)
    # The head's opening timestamp must remain the start; only take the tail's
    # newest timestamp for recency and do not let an incomplete final line win.
    tail_metadata = dict(metadata)
    tail_metadata["timestamp"] = None
    tail_metadata["first_msg"] = ""
    _metadata_from_lines(tail.splitlines(), tail_metadata)
    if tail_metadata.get("last_timestamp"):
        metadata["last_timestamp"] = tail_metadata["last_timestamp"]
    return metadata


def list_metadata_records(
    known_sources: dict[str, float] | None = None,
) -> list[dict[str, object]]:
    """Discover CodeX sessions newest-first without parsing transcript bodies."""
    records: list[dict[str, object]] = []
    try:
        paths = sorted(list_session_files(), key=os.path.getmtime, reverse=True)
    except OSError:
        paths = list_session_files()
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if known_sources is not None and abs(
            float(known_sources.get(path, -1)) - mtime
        ) <= 0.001:
            continue
        metadata = read_session_metadata(path)
        if str(metadata.get("thread_source") or "").lower() == "subagent":
            continue
        sid = str(metadata.get("session_id") or _session_id_from_path(path))
        first_msg = str(metadata.get("first_msg") or "")
        records.append({
            "path": path,
            "provider": "codex",
            "session_id": sid,
            "cwd": canonicalize_path(str(metadata.get("cwd") or "")),
            "timestamp": metadata.get("timestamp"),
            "last_timestamp": metadata.get("last_timestamp") or metadata.get("timestamp"),
            "name": first_msg or sid,
            "first_msg": first_msg,
            "last_msg": "",
            "msg_count": 0,
            "mtime": float(metadata.get("mtime") or 0.0),
            "source_size": int(metadata.get("size") or 0),
            "coverage": "pending",
        })
    return records


def get_live_activity(session_path: str) -> tuple[str | None, float | None]:
    """Provider-parity bounded recency reader used by the index refresh."""
    metadata = read_session_metadata(session_path)
    return (
        str(metadata["last_timestamp"]) if metadata.get("last_timestamp") else None,
        float(metadata["mtime"]) if metadata.get("mtime") else None,
    )


def _searchable_body(text: str) -> str:
    body, _boilerplate = split_boilerplate(text)
    return flatten_text(body).strip()


def load_history() -> dict[str, list[dict[str, object]]]:
    if not os.path.exists(CODEX_HISTORY_PATH):
        return {}

    mtime = os.path.getmtime(CODEX_HISTORY_PATH)
    cached_mtime = _CODEX_HISTORY_CACHE.get("mtime")
    if cached_mtime == mtime:
        return _CODEX_HISTORY_CACHE["entries"]  # type: ignore[return-value]

    entries: dict[str, list[dict[str, object]]] = {}
    try:
        with open(CODEX_HISTORY_PATH) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                sid = data.get("session_id")
                text = data.get("text")
                if not sid or not isinstance(text, str) or not text.strip():
                    continue
                entries.setdefault(sid, []).append({
                    "text": text,
                    "ts": data.get("ts"),
                })
    except Exception:
        entries = {}

    _CODEX_HISTORY_CACHE["mtime"] = mtime
    _CODEX_HISTORY_CACHE["entries"] = entries
    return entries


def _load_state_records() -> tuple[list[dict[str, object]], float]:
    if not os.path.exists(CODEX_STATE_DB):
        return ([], 0.0)

    state_mtime = os.path.getmtime(CODEX_STATE_DB)
    try:
        conn = _open_state_db(CODEX_STATE_DB)
    except sqlite3.OperationalError as exc:
        if _is_sqlite_lock_error(exc):
            return ([], state_mtime)
        raise
    try:
        try:
            rows = conn.execute(
                """
                SELECT id, rollout_path, cwd, title, first_user_message, created_at_ms,
                       updated_at_ms, created_at, updated_at
                FROM threads
                WHERE COALESCE(thread_source, '') != 'subagent'
                ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_sqlite_lock_error(exc):
                return ([], state_mtime)
            try:
                rows = conn.execute(
                    """
                    SELECT id, path, cwd, title, first_user_message, created_at_ms,
                           updated_at_ms, created_at, updated_at
                    FROM threads
                    WHERE COALESCE(thread_source, '') != 'subagent'
                    ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC
                    """
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if _is_sqlite_lock_error(exc):
                    return ([], state_mtime)
                try:
                    legacy_rows = conn.execute(
                        """
                        SELECT id, cwd, title, first_user_message, created_at_ms,
                               updated_at_ms, created_at, updated_at
                        FROM threads
                        WHERE COALESCE(thread_source, '') != 'subagent'
                        ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC
                        """
                    ).fetchall()
                except sqlite3.OperationalError as exc:
                    if _is_sqlite_lock_error(exc):
                        return ([], state_mtime)
                    raise
                rows = [
                    (
                        sid,
                        "",
                        cwd,
                        title,
                        first_user_message,
                        created_ms,
                        updated_ms,
                        created_s,
                        updated_s,
                    )
                    for (
                        sid,
                        cwd,
                        title,
                        first_user_message,
                        created_ms,
                        updated_ms,
                        created_s,
                        updated_s,
                    ) in legacy_rows
                ]
    finally:
        conn.close()

    records: list[dict[str, object]] = []
    for row in rows:
        (
            sid,
            session_path,
            cwd,
            title,
            first_user_message,
            created_ms,
            updated_ms,
            created_s,
            updated_s,
        ) = row
        records.append({
            "session_id": sid,
            "path": session_path or "",
            "cwd": cwd or "",
            "title": title or "",
            "first_user_message": first_user_message or "",
            "created_ms": created_ms,
            "updated_ms": updated_ms,
            "created_s": created_s,
            "updated_s": updated_s,
        })
    return (records, state_mtime)


def _state_updated_s(state: dict[str, object]) -> float:
    """Per-thread freshness from the state row, in epoch seconds."""
    if not state:
        return 0.0
    ms = state.get("updated_ms")
    if ms:
        try:
            return float(ms) / 1000.0
        except (TypeError, ValueError):
            pass
    try:
        return float(state.get("updated_s") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _history_last_ts(events: list[dict[str, object]]) -> float:
    last = 0.0
    for event in events or ():
        ts = event.get("ts")
        if isinstance(ts, (int, float)) and float(ts) > last:
            last = float(ts)
    return last


def _record_freshness(
    session_path: str,
    state: dict[str, object],
    events: list[dict[str, object]],
) -> float:
    """Per-session change fingerprint stored as the record's mtime.

    Deliberately NOT the file mtimes of state_5.sqlite/history.jsonl:
    those are global -- any codex activity anywhere bumped them, which
    made every codex record look changed on every launch and forced a
    full rewrite of all of them. Per-row updated_at and per-sid history
    timestamps only move when THIS session moves.
    """
    session_mtime = (
        os.path.getmtime(session_path)
        if session_path and os.path.exists(session_path)
        else 0.0
    )
    return max(session_mtime, _state_updated_s(state), _history_last_ts(events))


def _build_index_record(
    sid: str,
    session_path: str,
    state: dict[str, object],
    metadata: dict[str, object],
    history: dict[str, list[dict[str, object]]],
) -> dict[str, object] | None:
    events = history.get(sid, [])
    event_texts = [str(event.get("text", "")) for event in events]
    turns = _load_session_turns(session_path)

    msg_count = len(turns) or len(events)
    first_msg = _searchable_body(str(state.get("first_user_message") or ""))
    if not first_msg:
        for role, text in turns:
            if role == "user":
                first_msg = _searchable_body(text)
                if first_msg:
                    break
        if not first_msg:
            for text in event_texts:
                cleaned = _searchable_body(text)
                if cleaned and not is_noise_text(cleaned):
                    first_msg = cleaned
                    break
    if not first_msg:
        return None

    last_msg = ""
    user_parts: list[str] = []
    asst_parts: list[str] = []
    boilerplate_parts: list[str] = []
    if turns:
        first_user_seen = False
        for role, text in turns:
            if role == "user":
                body, boilerplate = split_boilerplate(text)
                boilerplate_parts.extend(boilerplate)
                if not body:
                    continue
                body_flat = flatten_text(body)
                if first_user_seen:
                    user_parts.append(body)
                else:
                    first_user_seen = True
                if is_substantive_text(body_flat):
                    last_msg = body_flat
            elif role == "assistant":
                asst_parts.append(text)
    else:
        for text in event_texts:
            cleaned = flatten_text(text)
            if not cleaned or is_noise_text(cleaned):
                continue
            body, boilerplate = split_boilerplate(text)
            boilerplate_parts.extend(boilerplate)
            if not body:
                continue
            user_parts.append(body)
            body_flat = flatten_text(body)
            if is_substantive_text(body_flat):
                last_msg = body_flat

    created_at = (
        epoch_ms_to_iso(state.get("created_ms"))
        or epoch_s_to_iso(state.get("created_s"))
        or metadata.get("timestamp")
    )
    updated_at = (
        epoch_ms_to_iso(state.get("updated_ms"))
        or epoch_s_to_iso(state.get("updated_s"))
        or metadata.get("last_timestamp")
        or metadata.get("timestamp")
    )
    record_mtime = _record_freshness(session_path, state, events)
    try:
        source_size = os.path.getsize(session_path) if session_path else 0
        source_mtime = os.path.getmtime(session_path) if session_path else 0.0
    except OSError:
        source_size = 0
        source_mtime = 0.0
    cwd = state.get("cwd") or metadata.get("cwd") or ""
    title_raw = str(state.get("title") or "").strip()
    title, title_boilerplate = split_boilerplate(title_raw)
    title = flatten_text(title).strip()
    boilerplate_parts.extend(title_boilerplate)

    return {
        "path": session_path or f"codex://{sid}",
        "provider": "codex",
        "session_id": sid,
        "first_msg": first_msg[:200],
        "last_msg": last_msg[:200],
        "timestamp": created_at,
        "last_timestamp": updated_at,
        "cwd": canonicalize_path(cwd),
        "name": title or first_msg[:200],
        "msg_count": msg_count,
        "mtime": record_mtime,
        "source_mtime": source_mtime,
        "source_size": source_size,
        "fields": {
            "cwd": str(cwd or "").lower(),
            "title": title.lower(),
            "first_msg": first_msg.lower(),
            "user_text": " ".join(user_parts).lower(),
            "asst_text": " ".join(asst_parts).lower(),
            "boilerplate": " ".join(boilerplate_parts).lower(),
        },
    }


def list_index_records(
    known_sessions: dict[str, tuple[str, float]] | None = None,
) -> list[dict[str, object]]:
    history = load_history()
    state_rows, _state_mtime = _load_state_records()
    state_by_sid = {str(row["session_id"]): row for row in state_rows}
    state_by_path = {
        str(row["path"]): row
        for row in state_rows
        if row.get("path")
    }
    records: list[dict[str, object]] = []
    seen_sids: set[str] = set()

    for session_path in list_session_files():
        if known_sessions is not None and session_path in known_sessions:
            # The stored sid came from this file's last full parse, so
            # state/history lookups don't depend on guessing the sid from
            # the filename. An indexed path is never a subagent session
            # (those are filtered before indexing), so skipping the
            # metadata parse cannot let one back in.
            known_sid, known_mtime = known_sessions[session_path]
            state = state_by_sid.get(known_sid) or state_by_path.get(session_path) or {}
            freshness = _record_freshness(
                session_path, state, history.get(known_sid, [])
            )
            if abs(freshness - float(known_mtime)) <= 0.001:
                records.append({
                    "path": session_path,
                    "provider": "codex",
                    "mtime": known_mtime,
                    "unchanged": True,
                })
                seen_sids.add(known_sid)
                continue
        metadata = _load_session_metadata(session_path)
        if str(metadata.get("thread_source") or "").lower() == "subagent":
            continue
        sid = str(metadata.get("session_id") or _session_id_from_path(session_path))
        state = state_by_sid.get(sid) or state_by_path.get(session_path) or {}
        record = _build_index_record(sid, session_path, state, metadata, history)
        if record:
            records.append(record)
            seen_sids.add(sid)

    for state in state_rows:
        sid = str(state["session_id"])
        if sid in seen_sids:
            continue
        state_path = str(state.get("path") or "")
        record_path = state_path or f"codex://{sid}"
        if known_sessions is not None and record_path in known_sessions:
            _known_sid, known_mtime = known_sessions[record_path]
            freshness = _record_freshness(state_path, state, history.get(sid, []))
            if abs(freshness - float(known_mtime)) <= 0.001:
                records.append({
                    "path": record_path,
                    "provider": "codex",
                    "mtime": known_mtime,
                    "unchanged": True,
                })
                seen_sids.add(sid)
                continue
        metadata = {"timestamp": None, "last_timestamp": None, "cwd": ""}
        record = _build_index_record(sid, state_path, state, metadata, history)
        if record:
            records.append(record)

    return records


def preview_messages(path: str, session_id: str) -> list[tuple[int, str]]:
    messages: list[tuple[int, str]] = []
    turns = _load_session_turns(path)
    if turns:
        msg_num = 0
        for role, text in turns:
            if role != "user":
                continue
            msg_num += 1
            messages.append((msg_num, text[:140]))
        return messages

    for idx, entry in enumerate(load_history().get(session_id, []), 1):
        cleaned = flatten_text(str(entry.get("text", "")))
        if len(cleaned) <= 3 or is_noise_text(cleaned):
            continue
        messages.append((idx, cleaned[:140]))
    return messages


def transcript_turns(
    path: str, session_id: str, flatten: bool = True
) -> list[tuple[str, str]]:
    turns = _load_session_turns(path, flatten=flatten)
    if turns:
        return turns

    excerpt: list[tuple[str, str]] = []
    for entry in load_history().get(session_id, []):
        cleaned = flatten_text(str(entry.get("text", "")))
        if len(cleaned) <= 3 or is_noise_text(cleaned):
            continue
        excerpt.append(("user", cleaned))
    return excerpt


PROVIDER = ProviderSpec(
    provider_id="codex",
    display_name="CodeX",
    binary="codex",
    native_resume_prefix=("codex", "resume"),
    native_fork_prefix=("codex", "fork"),
    list_index_records_reader=list_index_records,
    preview_messages_reader=preview_messages,
    transcript_turns_reader=transcript_turns,
    has_local_state_reader=has_local_state,
    session_files_reader=list_session_files,
    native_yolo_flag="--dangerously-bypass-approvals-and-sandbox",
    handoff_yolo_flag="--dangerously-bypass-approvals-and-sandbox",
    can_native_resume=True,
    assistant_turns_available=True,
)

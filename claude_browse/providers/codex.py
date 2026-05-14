"""CodeX session adapter."""

from __future__ import annotations

import glob
import json
import os
import sqlite3

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

_CODEX_HISTORY_CACHE: dict[str, object] = {
    "mtime": None,
    "entries": {},
}
_CODEX_SESSION_TURNS_CACHE: dict[str, object] = {
    "entries": {},
}


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
) -> None:
    cleaned = flatten_text(text)
    if len(cleaned) <= 3 or is_noise_text(cleaned):
        return
    if turns and turns[-1] == (role, cleaned):
        return
    turns.append((role, cleaned))


def _load_session_turns(session_path: str) -> list[tuple[str, str]]:
    if not session_path or not os.path.exists(session_path):
        return []

    mtime = os.path.getmtime(session_path)
    cache_key = f"{session_path}:{mtime}"
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
                        _append_turn(turns, "user", str(payload.get("message", "")))
                    elif event_type == "agent_message":
                        _append_turn(
                            turns, "assistant", str(payload.get("message", ""))
                        )
                    elif event_type == "task_complete":
                        _append_turn(
                            turns,
                            "assistant",
                            str(payload.get("last_agent_message", "")),
                        )
                    continue

                if item_type != "response_item":
                    continue

                payload_type = payload.get("type")
                role = payload.get("role")
                if payload_type == "message" and role in ("user", "assistant"):
                    text = _extract_codex_content_text(payload.get("content"))
                    _append_turn(turns, role, text)
    except Exception:
        turns = []

    entries = _CODEX_SESSION_TURNS_CACHE["entries"]
    if isinstance(entries, dict):
        entries[cache_key] = turns
    return turns


def list_session_files() -> list[str]:
    pattern = os.path.join(CODEX_SESSIONS_DIR, "**", "*.jsonl")
    return sorted(glob.glob(pattern, recursive=True))


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


def list_index_records() -> list[dict[str, object]]:
    if not os.path.exists(CODEX_STATE_DB):
        return []

    history = load_history()
    history_mtime = (
        os.path.getmtime(CODEX_HISTORY_PATH)
        if os.path.exists(CODEX_HISTORY_PATH)
        else 0.0
    )
    state_mtime = os.path.getmtime(CODEX_STATE_DB)

    conn = sqlite3.connect(CODEX_STATE_DB)
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
        except sqlite3.OperationalError:
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
            except sqlite3.OperationalError:
                legacy_rows = conn.execute(
                    """
                    SELECT id, cwd, title, first_user_message, created_at_ms,
                           updated_at_ms, created_at, updated_at
                    FROM threads
                    WHERE COALESCE(thread_source, '') != 'subagent'
                    ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC
                    """
                ).fetchall()
                rows = [(
                    sid,
                    "",
                    cwd,
                    title,
                    first_user_message,
                    created_ms,
                    updated_ms,
                    created_s,
                    updated_s,
                ) for (
                    sid,
                    cwd,
                    title,
                    first_user_message,
                    created_ms,
                    updated_ms,
                    created_s,
                    updated_s,
                ) in legacy_rows]
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
        events = history.get(sid, [])
        event_texts = [str(event.get("text", "")) for event in events]
        turns = _load_session_turns(session_path or "")

        msg_count = len(turns) or len(events)
        first_msg = flatten_text(first_user_message or "").strip()
        if not first_msg:
            for role, text in turns:
                if role == "user":
                    first_msg = text
                    break
            if not first_msg:
                for text in event_texts:
                    cleaned = flatten_text(text)
                    if cleaned and not is_noise_text(cleaned):
                        first_msg = cleaned
                        break

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
                if not body:
                    continue
                user_parts.append(body)
                boilerplate_parts.extend(boilerplate)
                body_flat = flatten_text(body)
                if is_substantive_text(body_flat):
                    last_msg = body_flat

        created_at = epoch_ms_to_iso(created_ms) or epoch_s_to_iso(created_s)
        updated_at = epoch_ms_to_iso(updated_ms) or epoch_s_to_iso(updated_s)
        session_mtime = (
            os.path.getmtime(session_path)
            if session_path and os.path.exists(session_path)
            else 0.0
        )
        mtime = max(state_mtime, history_mtime, session_mtime)

        records.append({
            "path": session_path or f"codex://{sid}",
            "provider": "codex",
            "session_id": sid,
            "first_msg": first_msg[:200],
            "last_msg": last_msg[:200],
            "timestamp": created_at,
            "last_timestamp": updated_at,
            "cwd": canonicalize_path(cwd),
            "name": (title or "").strip() or first_msg[:200],
            "msg_count": msg_count,
            "mtime": mtime,
            "fields": {
                "cwd": (cwd or "").lower(),
                "title": (title or "").lower(),
                "first_msg": first_msg.lower(),
                "user_text": " ".join(user_parts).lower(),
                "asst_text": " ".join(asst_parts).lower(),
                "boilerplate": " ".join(boilerplate_parts).lower(),
            },
        })

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


def transcript_turns(path: str, session_id: str) -> list[tuple[str, str]]:
    turns = _load_session_turns(path)
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

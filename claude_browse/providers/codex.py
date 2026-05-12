"""CodeX session adapter."""

from __future__ import annotations

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

_CODEX_HISTORY_CACHE: dict[str, object] = {
    "mtime": None,
    "entries": {},
}


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
        rows = conn.execute(
            """
            SELECT id, cwd, title, first_user_message, created_at_ms,
                   updated_at_ms, created_at, updated_at
            FROM threads
            WHERE COALESCE(thread_source, '') != 'subagent'
            ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC
            """
        ).fetchall()
    finally:
        conn.close()

    records: list[dict[str, object]] = []
    for row in rows:
        sid, cwd, title, first_user_message, created_ms, updated_ms, created_s, updated_s = row
        events = history.get(sid, [])
        event_texts = [str(event.get("text", "")) for event in events]

        msg_count = len(events)
        first_msg = flatten_text(first_user_message or "").strip()
        if not first_msg:
            for text in event_texts:
                cleaned = flatten_text(text)
                if cleaned and not is_noise_text(cleaned):
                    first_msg = cleaned
                    break

        last_msg = ""
        user_parts: list[str] = []
        boilerplate_parts: list[str] = []
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
        mtime = max(state_mtime, history_mtime)

        records.append({
            "path": f"codex://{sid}",
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
                "asst_text": "",
                "boilerplate": " ".join(boilerplate_parts).lower(),
            },
        })

    return records


def preview_messages(path: str, session_id: str) -> list[tuple[int, str]]:
    del path
    messages: list[tuple[int, str]] = []
    for idx, entry in enumerate(load_history().get(session_id, []), 1):
        text = str(entry.get("text", ""))
        cleaned = flatten_text(text)
        if len(cleaned) <= 3 or is_noise_text(cleaned):
            continue
        messages.append((idx, cleaned[:140]))
    return messages


def transcript_turns(path: str, session_id: str) -> list[tuple[str, str]]:
    del path
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
    native_yolo_flag="--dangerously-bypass-approvals-and-sandbox",
    handoff_yolo_flag="--dangerously-bypass-approvals-and-sandbox",
    can_native_resume=True,
    assistant_turns_available=False,
)

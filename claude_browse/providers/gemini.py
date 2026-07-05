"""Gemini session adapter."""

from __future__ import annotations

import glob
import json
import os

from .base import ProviderSpec
from .common import (
    canonicalize_path,
    extract_text,
    flatten_text,
    is_noise_text,
    is_substantive_text,
    split_boilerplate,
)

GEMINI_HOME = os.path.expanduser("~/.gemini")
GEMINI_PROJECTS_PATH = os.path.join(GEMINI_HOME, "projects.json")
GEMINI_TMP_DIR = os.path.join(GEMINI_HOME, "tmp")


def has_local_state() -> bool:
    pattern = os.path.join(GEMINI_TMP_DIR, "*", "chats", "session-*.json")
    return any(glob.iglob(pattern))


def _project_roots_by_alias() -> dict[str, str]:
    if not os.path.exists(GEMINI_PROJECTS_PATH):
        return {}
    try:
        data = json.load(open(GEMINI_PROJECTS_PATH))
    except Exception:
        return {}

    projects = data.get("projects", {}) if isinstance(data, dict) else {}
    if not isinstance(projects, dict):
        return {}

    aliases: dict[str, str] = {}
    for root, alias in projects.items():
        if isinstance(root, str) and isinstance(alias, str):
            aliases[alias] = canonicalize_path(root) or root
    return aliases


def _session_files() -> list[str]:
    pattern = os.path.join(GEMINI_TMP_DIR, "*", "chats", "session-*.json")
    return sorted(glob.glob(pattern))


def _project_root_for_session(chat_path: str, alias_roots: dict[str, str]) -> str | None:
    project_dir = os.path.dirname(os.path.dirname(chat_path))
    root_path = os.path.join(project_dir, ".project_root")
    try:
        with open(root_path) as f:
            root = f.read().strip()
    except OSError:
        root = ""

    if root:
        return canonicalize_path(root)

    alias = os.path.basename(project_dir)
    return alias_roots.get(alias)


def _role_and_text(message: dict) -> tuple[str | None, str]:
    msg_type = message.get("type")
    if msg_type == "user":
        role = "user"
    elif msg_type in {"gemini", "assistant", "model"}:
        role = "assistant"
    else:
        return None, ""

    text = extract_text(message.get("content", ""))
    return role, text


def _parse_session(chat_path: str, alias_roots: dict[str, str]) -> dict[str, object] | None:
    try:
        with open(chat_path) as f:
            data = json.load(f)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    if data.get("kind") not in (None, "main"):
        return None

    session_id = data.get("sessionId")
    messages = data.get("messages", [])
    if not isinstance(session_id, str) or not isinstance(messages, list):
        return None

    cwd = _project_root_for_session(chat_path, alias_roots)
    first_msg = ""
    last_msg = ""
    msg_count = 0
    user_parts: list[str] = []
    asst_parts: list[str] = []
    boilerplate_parts: list[str] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role, text = _role_and_text(message)
        if role is None:
            continue
        msg_count += 1
        if not text:
            continue

        cleaned = flatten_text(text)
        if role == "user":
            if not cleaned or is_noise_text(cleaned):
                continue
            body, boilerplate = split_boilerplate(text)
            boilerplate_parts.extend(boilerplate)
            if not body:
                continue
            body_flat = flatten_text(body)
            if not first_msg:
                first_msg = body_flat
            else:
                user_parts.append(body)
            if is_substantive_text(body_flat):
                last_msg = body_flat
        else:
            if len(cleaned) <= 3:
                continue
            asst_parts.append(text)

    if not first_msg:
        return None

    return {
        "path": chat_path,
        "provider": "gemini",
        "session_id": session_id,
        "first_msg": first_msg[:200],
        "last_msg": last_msg[:200],
        "timestamp": data.get("startTime"),
        "last_timestamp": data.get("lastUpdated") or data.get("startTime"),
        "cwd": cwd,
        "name": first_msg[:200],
        "msg_count": msg_count,
        "mtime": os.path.getmtime(chat_path),
        "fields": {
            "cwd": (cwd or "").lower(),
            "title": "",
            "first_msg": first_msg.lower(),
            "user_text": " ".join(user_parts).lower(),
            "asst_text": " ".join(asst_parts).lower(),
            "boilerplate": " ".join(boilerplate_parts).lower(),
        },
    }


def list_index_records(
    known_sessions: dict[str, tuple[str, float]] | None = None,
) -> list[dict[str, object]]:
    alias_roots = _project_roots_by_alias()
    records: list[dict[str, object]] = []
    for chat_path in _session_files():
        if known_sessions is not None and chat_path in known_sessions:
            _sid, known_mtime = known_sessions[chat_path]
            try:
                mtime = os.path.getmtime(chat_path)
            except OSError:
                continue
            if abs(mtime - float(known_mtime)) <= 0.001:
                records.append({
                    "path": chat_path,
                    "provider": "gemini",
                    "mtime": known_mtime,
                    "unchanged": True,
                })
                continue
        record = _parse_session(chat_path, alias_roots)
        if record:
            records.append(record)
    return records


def preview_messages(path: str, session_id: str) -> list[tuple[int, str]]:
    del session_id
    alias_roots = _project_roots_by_alias()
    session = _parse_session(path, alias_roots)
    if not session:
        return []

    messages: list[tuple[int, str]] = []
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return []

    idx = 0
    for message in data.get("messages", []):
        if not isinstance(message, dict):
            continue
        role, text = _role_and_text(message)
        if role != "user":
            continue
        idx += 1
        cleaned = flatten_text(text)
        if len(cleaned) <= 3 or is_noise_text(cleaned):
            continue
        messages.append((idx, cleaned[:140]))
    return messages


def transcript_turns(path: str, session_id: str) -> list[tuple[str, str]]:
    del session_id
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return []

    turns: list[tuple[str, str]] = []
    for message in data.get("messages", []):
        if not isinstance(message, dict):
            continue
        role, text = _role_and_text(message)
        if role is None:
            continue
        cleaned = flatten_text(text)
        if len(cleaned) <= 3:
            continue
        if role == "user" and is_noise_text(cleaned):
            continue
        turns.append((role, cleaned))
    return turns


PROVIDER = ProviderSpec(
    provider_id="gemini",
    display_name="Gemini",
    binary="gemini",
    native_resume_prefix=("gemini", "--resume"),
    list_index_records_reader=list_index_records,
    preview_messages_reader=preview_messages,
    transcript_turns_reader=transcript_turns,
    has_local_state_reader=has_local_state,
    native_yolo_flag="--yolo",
    handoff_yolo_flag="--yolo",
    add_dir_flag="--include-directories",
    handoff_prompt_flag="--prompt-interactive",
    can_native_resume=True,
    assistant_turns_available=True,
)

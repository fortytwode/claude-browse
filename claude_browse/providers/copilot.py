"""GitHub Copilot CLI session adapter."""

from __future__ import annotations

import glob
import json
import os

from .base import ProviderSpec
from .common import (
    canonicalize_path,
    flatten_text,
    is_noise_text,
    is_substantive_text,
    split_boilerplate,
)

COPILOT_HOME = os.path.expanduser("~/.copilot")
SESSION_STATE_DIR = os.path.join(COPILOT_HOME, "session-state")


def has_local_state() -> bool:
    return os.path.isdir(SESSION_STATE_DIR)


def _session_dirs() -> list[str]:
    pattern = os.path.join(SESSION_STATE_DIR, "*")
    return sorted(
        path for path in glob.glob(pattern)
        if os.path.isdir(path)
    )


def _unquote_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_workspace_yaml(path: str) -> dict[str, str]:
    data: dict[str, str] = {}
    current_parent: str | None = None

    try:
        with open(path) as f:
            for raw_line in f:
                line = raw_line.rstrip("\n")
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or ":" not in stripped:
                    continue

                indent = len(line) - len(line.lstrip(" "))
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip()

                if indent == 0:
                    current_parent = key if not value else None
                    if value:
                        data[key] = _unquote_yaml_scalar(value)
                    continue

                if current_parent and value:
                    data[f"{current_parent}.{key}"] = _unquote_yaml_scalar(value)
    except OSError:
        return {}

    return data


def _events_path(path: str, session_id: str) -> str:
    if path:
        return os.path.join(path, "events.jsonl")
    return os.path.join(SESSION_STATE_DIR, session_id, "events.jsonl")


def _workspace_path(path: str, session_id: str) -> str:
    if path:
        return os.path.join(path, "workspace.yaml")
    return os.path.join(SESSION_STATE_DIR, session_id, "workspace.yaml")


def _load_events(path: str, session_id: str) -> list[dict[str, object]]:
    events_path = _events_path(path, session_id)
    events: list[dict[str, object]] = []
    try:
        with open(events_path) as f:
            for line in f:
                try:
                    event = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    return events


def _event_role_and_text(event: dict[str, object]) -> tuple[str | None, str]:
    event_type = event.get("type")
    data = event.get("data")
    if not isinstance(data, dict):
        return None, ""

    if event_type == "user.message":
        role = "user"
    elif event_type == "assistant.message":
        role = "assistant"
    else:
        return None, ""

    text = data.get("content")
    return role, text if isinstance(text, str) else ""


def _event_cwd(event: dict[str, object]) -> str | None:
    if event.get("type") != "session.context_changed":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    cwd = data.get("cwd")
    return cwd if isinstance(cwd, str) and cwd.strip() else None


def _parse_session(session_dir: str) -> dict[str, object] | None:
    session_id = os.path.basename(session_dir)
    workspace = _parse_workspace_yaml(_workspace_path(session_dir, session_id))
    events = _load_events(session_dir, session_id)
    if not events:
        return None

    cwd = canonicalize_path(workspace.get("context.cwd") or workspace.get("cwd"))
    first_msg = ""
    last_msg = ""
    timestamp = None
    last_timestamp = None
    msg_count = 0
    user_parts: list[str] = []
    asst_parts: list[str] = []
    boilerplate_parts: list[str] = []

    for event in events:
        event_ts = event.get("timestamp")
        if isinstance(event_ts, str):
            if not timestamp:
                timestamp = event_ts
            last_timestamp = event_ts

        context_cwd = _event_cwd(event)
        if context_cwd:
            cwd = canonicalize_path(context_cwd)

        role, text = _event_role_and_text(event)
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

    workspace_name = workspace.get("name") or workspace.get("title") or ""
    name = workspace_name.strip() or first_msg[:200]
    events_path = _events_path(session_dir, session_id)
    workspace_path = _workspace_path(session_dir, session_id)
    mtime_candidates = [os.path.getmtime(events_path)]
    if os.path.exists(workspace_path):
        mtime_candidates.append(os.path.getmtime(workspace_path))

    return {
        "path": session_dir,
        "provider": "copilot",
        "session_id": session_id,
        "first_msg": first_msg[:200],
        "last_msg": last_msg[:200],
        "timestamp": timestamp,
        "last_timestamp": last_timestamp or timestamp,
        "cwd": cwd,
        "name": name[:200],
        "msg_count": msg_count,
        "mtime": max(mtime_candidates),
        "fields": {
            "cwd": (cwd or "").lower(),
            "title": name.lower(),
            "first_msg": first_msg.lower(),
            "user_text": " ".join(user_parts).lower(),
            "asst_text": " ".join(asst_parts).lower(),
            "boilerplate": " ".join(boilerplate_parts).lower(),
        },
    }


def list_index_records() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for session_dir in _session_dirs():
        record = _parse_session(session_dir)
        if record:
            records.append(record)
    return records


def preview_messages(path: str, session_id: str) -> list[tuple[int, str]]:
    messages: list[tuple[int, str]] = []
    idx = 0
    for event in _load_events(path, session_id):
        role, text = _event_role_and_text(event)
        if role != "user":
            continue
        idx += 1
        cleaned = flatten_text(text)
        if len(cleaned) <= 3 or is_noise_text(cleaned):
            continue
        messages.append((idx, cleaned[:140]))
    return messages


def transcript_turns(path: str, session_id: str) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    for event in _load_events(path, session_id):
        role, text = _event_role_and_text(event)
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
    provider_id="copilot",
    display_name="Copilot",
    binary="copilot",
    native_resume_prefix=("copilot", "--resume"),
    list_index_records_reader=list_index_records,
    preview_messages_reader=preview_messages,
    transcript_turns_reader=transcript_turns,
    has_local_state_reader=has_local_state,
    native_yolo_flag="--yolo",
    handoff_yolo_flag="--yolo",
    add_dir_flag="--add-dir",
    can_native_resume=True,
    assistant_turns_available=True,
    source_capable=True,
    target_capable=True,
    install_hint="Install GitHub Copilot CLI and ensure `copilot` is on PATH.",
    login_hint="Run `copilot login` before launching Copilot sessions.",
)

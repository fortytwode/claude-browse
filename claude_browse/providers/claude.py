"""Claude session adapter."""

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

SESSIONS_DIR = os.path.expanduser("~/.claude/projects")
HISTORY_PATH = os.path.expanduser("~/.claude/history.jsonl")


def has_local_state() -> bool:
    return os.path.isdir(SESSIONS_DIR)


def get_session_info(jsonl_path: str) -> dict | None:
    first_user_msg = None
    last_user_msg = None
    session_id = None
    timestamp = None
    last_timestamp = None
    cwd = None
    ai_title = None
    custom_title = None
    legacy_name = None
    msg_count = 0

    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                msg = data.get("message", data)
                msg_type = data.get("type", "")

                if msg_type == "custom-title":
                    custom_title = data.get("customTitle") or custom_title
                elif msg_type == "ai-title":
                    ai_title = data.get("aiTitle") or ai_title
                elif msg_type == "summary":
                    legacy_name = data.get("sessionName") or legacy_name

                if not session_id and data.get("sessionId"):
                    session_id = data.get("sessionId")
                if not cwd and data.get("cwd"):
                    cwd = data.get("cwd")
                if data.get("timestamp"):
                    if not timestamp:
                        timestamp = data.get("timestamp")
                    last_timestamp = data.get("timestamp")

                if msg.get("role") == "user":
                    msg_count += 1
                    text = extract_text(msg.get("content", ""))
                    if text and len(text) > 3:
                        body, _boilerplate = split_boilerplate(text)
                        cleaned = flatten_text(body)
                        if cleaned and not is_noise_text(cleaned):
                            if not first_user_msg:
                                first_user_msg = cleaned
                            if is_substantive_text(cleaned):
                                last_user_msg = cleaned
                elif msg.get("role") == "assistant":
                    msg_count += 1
    except Exception:
        return None

    name = custom_title or ai_title or legacy_name

    return {
        "path": jsonl_path,
        "provider": "claude",
        "session_id": session_id,
        "first_msg": (first_user_msg or "").strip()[:200],
        "last_msg": (last_user_msg or "").strip()[:200],
        "timestamp": timestamp,
        "last_timestamp": last_timestamp,
        "cwd": cwd,
        "name": name,
        "msg_count": msg_count,
    }


def extract_fielded_corpus(jsonl_path: str) -> dict[str, str]:
    cwd = ""
    title_custom = ""
    title_ai = ""
    title_legacy = ""
    first_msg = ""
    first_user_seen = False
    user_parts: list[str] = []
    asst_parts: list[str] = []
    boilerplate_parts: list[str] = []

    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                if not cwd and data.get("cwd"):
                    cwd = data["cwd"]

                msg_type = data.get("type", "")
                if msg_type == "custom-title":
                    title_custom = data.get("customTitle") or title_custom
                elif msg_type == "ai-title":
                    title_ai = data.get("aiTitle") or title_ai
                elif msg_type == "summary":
                    title_legacy = data.get("sessionName") or title_legacy

                msg = data.get("message", data)
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue

                text = extract_text(msg.get("content", ""))
                if not text:
                    continue

                if role == "user":
                    cleaned = text.lstrip()
                    if is_noise_text(cleaned):
                        continue
                    body, boilerplate = split_boilerplate(text)
                    boilerplate_parts.extend(boilerplate)
                    if not body:
                        continue
                    if not first_user_seen:
                        first_msg = body
                        first_user_seen = True
                    else:
                        user_parts.append(body)
                else:
                    asst_parts.append(text)
    except Exception:
        return {
            "cwd": "", "title": "", "first_msg": "",
            "user_text": "", "asst_text": "", "boilerplate": "",
        }

    title = title_custom or title_ai or title_legacy or ""
    return {
        "cwd": cwd,
        "title": title.lower(),
        "first_msg": first_msg.lower(),
        "user_text": " ".join(user_parts).lower(),
        "asst_text": " ".join(asst_parts).lower(),
        "boilerplate": " ".join(boilerplate_parts).lower(),
    }


def extract_search_corpus(jsonl_path: str) -> str:
    fields = extract_fielded_corpus(jsonl_path)
    return " ".join(
        value for key, value in fields.items()
        if key in ("first_msg", "user_text", "asst_text") and value
    )


def list_session_files() -> list[str]:
    pattern = os.path.join(SESSIONS_DIR, "*", "*.jsonl")
    paths = set(glob.glob(pattern))
    paths.update(_list_history_session_files())
    return sorted(path for path in paths if "/subagents/" not in path)


def _list_history_session_files() -> list[str]:
    """Recover Claude sessions known to history but missed by directory walks."""
    files: list[str] = []
    if not os.path.exists(HISTORY_PATH):
        return files

    try:
        with open(HISTORY_PATH) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                session_id = data.get("sessionId")
                project = data.get("project")
                if not session_id or not project:
                    continue
                project_dir = str(project).replace("/", "-")
                path = os.path.join(SESSIONS_DIR, project_dir, f"{session_id}.jsonl")
                if os.path.isfile(path):
                    files.append(path)
    except OSError:
        return files

    return files


def list_index_records(
    known_sessions: dict[str, tuple[str, float]] | None = None,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for filepath in list_session_files():
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue
        if known_sessions is not None and filepath in known_sessions:
            _sid, known_mtime = known_sessions[filepath]
            if abs(mtime - float(known_mtime)) <= 0.001:
                # Already indexed and untouched: a stat is all it costs.
                records.append({
                    "path": filepath,
                    "provider": "claude",
                    "mtime": known_mtime,
                    "unchanged": True,
                })
                continue
        info = get_session_info(filepath)
        if not info or not info.get("session_id") or not info.get("first_msg"):
            continue
        records.append({
            **info,
            "provider": "claude",
            "cwd": canonicalize_path(info.get("cwd")),
            "mtime": mtime,
            "fields": extract_fielded_corpus(filepath),
        })
    return records


def preview_messages(path: str, session_id: str) -> list[tuple[int, str]]:
    del session_id
    messages: list[tuple[int, str]] = []
    msg_num = 0

    try:
        with open(path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                msg = data.get("message", data)
                if msg.get("role") != "user":
                    continue
                msg_num += 1
                text = extract_text(msg.get("content", ""))
                if not text:
                    continue
                cleaned = flatten_text(text)
                if len(cleaned) <= 3 or is_noise_text(cleaned):
                    continue
                messages.append((msg_num, cleaned[:140]))
    except Exception:
        return []

    return messages


def transcript_turns(path: str, session_id: str) -> list[tuple[str, str]]:
    del session_id
    excerpt: list[tuple[str, str]] = []

    try:
        with open(path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                msg = data.get("message", data)
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = extract_text(msg.get("content", ""))
                cleaned = flatten_text(text)
                if len(cleaned) <= 3:
                    continue
                if role == "user" and is_noise_text(cleaned):
                    continue
                excerpt.append((role, cleaned))
    except Exception:
        return []

    return excerpt


PROVIDER = ProviderSpec(
    provider_id="claude",
    display_name="Claude",
    binary="claude",
    native_resume_prefix=("claude", "--resume"),
    list_index_records_reader=list_index_records,
    preview_messages_reader=preview_messages,
    transcript_turns_reader=transcript_turns,
    has_local_state_reader=has_local_state,
    session_info_reader=get_session_info,
    fielded_corpus_reader=extract_fielded_corpus,
    session_files_reader=list_session_files,
    native_yolo_flag="--dangerously-skip-permissions",
    handoff_yolo_flag="--dangerously-skip-permissions",
    handoff_prompt_prefix=("--",),
    can_native_resume=True,
    assistant_turns_available=True,
)

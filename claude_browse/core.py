"""Shared session parsing and formatting utilities.

Both claude-browse and claude-resume use these. No I/O side effects beyond
reading session JSONL files from disk. No network calls ever — by design.
"""

from __future__ import annotations

import getpass
import glob
import json
import os
from collections.abc import Iterable
from datetime import datetime, timezone

SESSIONS_DIR = os.path.expanduser("~/.claude/projects")


def get_session_info(jsonl_path: str) -> dict | None:
    """Extract session metadata from a session JSONL file.

    Returns None if the file is unreadable. Missing fields are returned as
    empty strings or 0 — callers should check for truthiness, not None.
    """
    first_user_msg = None
    session_id = None
    timestamp = None
    cwd = None
    name = None
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

                if msg_type == "summary":
                    name = data.get("sessionName")

                if not session_id and data.get("sessionId"):
                    session_id = data.get("sessionId")
                if not cwd and data.get("cwd"):
                    cwd = data.get("cwd")
                if not timestamp and data.get("timestamp"):
                    timestamp = data.get("timestamp")

                if msg.get("role") == "user":
                    msg_count += 1
                    if not first_user_msg:
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("text"):
                                    first_user_msg = c["text"]
                                    break
                        elif isinstance(content, str) and len(content) > 3:
                            first_user_msg = content
                elif msg.get("role") == "assistant":
                    msg_count += 1
    except Exception:
        return None

    return {
        "path": jsonl_path,
        "session_id": session_id,
        "first_msg": (first_user_msg or "").replace("\n", " ").strip()[:200],
        "timestamp": timestamp,
        "cwd": cwd,
        "name": name,
        "msg_count": msg_count,
    }


def extract_user_text(jsonl_path: str) -> str:
    """Concatenate all user message text from a session, lowercased.

    Used for keyword search. Deliberately excludes assistant output, tool
    results, and system context so searches don't match on things like
    `cwd:` paths or CLAUDE.md contents.
    """
    parts: list[str] = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                msg = data.get("message", data)
                if msg.get("role") != "user":
                    continue
                content = msg.get("content", "")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("text"):
                            parts.append(c["text"])
    except Exception:
        return ""
    return " ".join(parts).lower()


def list_session_files() -> list[str]:
    """All session files on disk, excluding subagent-spawned ones."""
    pattern = os.path.join(SESSIONS_DIR, "*", "*.jsonl")
    return [f for f in glob.glob(pattern) if "/subagents/" not in f]


def format_date(ts: str | None) -> str:
    """Format an ISO timestamp compactly, relative to now."""
    if not ts:
        return "???"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = now - dt

        if diff.total_seconds() < 3600:
            return f"{int(diff.total_seconds() / 60)}m ago"
        elif diff.total_seconds() < 86400:
            return f"{int(diff.total_seconds() / 3600)}h ago"
        elif diff.days < 365:
            return dt.strftime("%-b %d")
        else:
            return dt.strftime("%b %Y")
    except Exception:
        return ts[:10]


def canonicalize_path(path: str | None) -> str | None:
    """Normalize a cwd across machines so the same project looks the same
    whether the session was recorded on Mac (/Users/<name>) or Linux
    (/home/<name>).

    Rules (applied in order):
      1. If path starts with /Users/<CURRENT_USER> or /home/<CURRENT_USER>,
         replace that prefix with the current $HOME.
      2. If path matches /Users/<any> or /home/<any> case-insensitively for
         the current user, same replacement.
      3. Otherwise return unchanged.

    This is the cross-machine sync feature: a Mac-recorded session cwd
    /Users/Shamanth/foo and a Linux-recorded /home/shamanth/foo both
    canonicalize to $HOME/foo, so claude-browse treats them as the same
    project.

    Honors $CLAUDE_BROWSE_PATH_ALIASES for custom mappings, formatted as:
        src1=dst1:src2=dst2
    Each alias rewrites any path starting with src1 to start with dst1.
    """
    if not path:
        return path

    home = os.path.expanduser("~")
    user = os.environ.get("USER") or getpass.getuser()

    # 1/2. Normalize Mac vs Linux home layouts to current $HOME
    for prefix in (f"/Users/{user}", f"/home/{user}"):
        if path == prefix:
            return home
        if path.startswith(prefix + "/"):
            return home + path[len(prefix):]
    # Case-insensitive match for Mac users (HFS+ often case-insensitive)
    lower = path.lower()
    for prefix in (f"/users/{user.lower()}", f"/home/{user.lower()}"):
        if lower == prefix:
            return home
        if lower.startswith(prefix + "/"):
            return home + path[len(prefix):]

    # 3. Custom aliases from env
    aliases = os.environ.get("CLAUDE_BROWSE_PATH_ALIASES", "")
    if aliases:
        for pair in aliases.split(":"):
            if "=" not in pair:
                continue
            src, dst = pair.split("=", 1)
            src, dst = src.strip(), dst.strip()
            if path == src:
                return dst
            if path.startswith(src + "/"):
                return dst + path[len(src):]

    return path


def folder_name(cwd: str | None, known_prefixes: Iterable[str] = ()) -> str:
    """Extract a short, meaningful folder name from a cwd for TUI columns.

    `known_prefixes` lets callers provide repo-specific strip rules (e.g.
    "team-operations/clients/" → show only the client name). The default
    empty tuple means: just return the last path component.
    """
    if not cwd:
        return "?"
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        rel = cwd[len(home):].strip("/")
    else:
        rel = cwd

    for prefix in known_prefixes:
        if rel.startswith(prefix):
            remainder = rel[len(prefix):].strip("/")
            if remainder:
                return remainder.split("/")[0]
            return prefix.rstrip("/").rsplit("/", 1)[-1]

    return rel.rstrip("/").rsplit("/", 1)[-1] if "/" in rel else (rel or "?")


def display_cwd(cwd: str | None) -> str:
    """Home-abbreviated full path for display. ~/foo/bar style."""
    if not cwd:
        return ""
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd

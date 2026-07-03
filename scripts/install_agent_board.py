#!/usr/bin/env python3
"""Idempotently wire agent-board into ~/.claude/settings.json.

Run by install.sh. Safe to re-run: appends our hook commands into whatever
groups already exist for each event (never wholesale-replaces a user's other
hooks for that event), skips any command that's already present, and backs
up settings.json before writing anything.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
AGENT_BOARD = str(REPO_DIR / "agent-board")
VENV_PYTHON = REPO_DIR / ".venv" / "bin" / "python"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Prefer the board-sync venv when it exists (has google-cloud-firestore,
# requests, anthropic); fall back to system python3 otherwise, so the async
# sync hook -- and with it, the Haiku namer in naming.py, which is only ever
# invoked from sync.push() -- is at least *reachable* on a fresh install
# before the optional venv is set up, rather than failing to launch at all
# (a hardcoded nonexistent .venv path would mean naming never runs even
# though naming itself needs neither Firestore nor Slack). Re-running
# install.sh after creating the venv upgrades this automatically, since
# _fully_wired() recomputes SYNC_CMD fresh each run.
_SYNC_PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else (sys.executable or "python3")

HOOK_CMD = f"{AGENT_BOARD} hook"
SYNC_CMD = f"{_SYNC_PYTHON} {AGENT_BOARD} sync push"
STATUSLINE_CMD = f"{AGENT_BOARD} statusline"

_SIMPLE_EVENTS = ("SessionStart", "UserPromptSubmit")
_SYNC_EVENTS = ("Stop", "Notification", "SessionEnd")


def _entry(command: str, *, timeout: int = 10, is_async: bool = False) -> dict:
    e: dict = {"type": "command", "command": command, "timeout": timeout}
    if is_async:
        e["async"] = True
    return e


def _existing_commands(hooks: dict, event: str) -> set[str]:
    groups = hooks.get(event, [])
    return {h.get("command") for g in groups for h in g.get("hooks", [])}


def _ensure_hook(hooks: dict, event: str, entries: list[dict]) -> bool:
    """Append missing entries into the first existing group for this event.
    Returns True if anything changed."""
    groups = hooks.setdefault(event, [])
    if not groups:
        groups.append({"hooks": []})
    existing = _existing_commands(hooks, event)
    to_add = [e for e in entries if e["command"] not in existing]
    if to_add:
        groups[0].setdefault("hooks", []).extend(to_add)
        return True
    return False


def _fully_wired(hooks: dict) -> bool:
    for event in _SIMPLE_EVENTS:
        if HOOK_CMD not in _existing_commands(hooks, event):
            return False
    for event in _SYNC_EVENTS:
        existing = _existing_commands(hooks, event)
        if HOOK_CMD not in existing or SYNC_CMD not in existing:
            return False
    return True


def _wire_hooks_and_statusline() -> dict:
    if SETTINGS_PATH.exists():
        settings = json.loads(SETTINGS_PATH.read_text())
    else:
        settings = {}

    hooks = settings.get("hooks", {})
    statusline = settings.get("statusLine", {})
    already_wired = (
        _fully_wired(hooks)
        and statusline.get("command") == STATUSLINE_CMD
        and settings.get("agentPushNotifEnabled") is False
    )

    if already_wired:
        print("  agent-board hooks + statusLine already wired in ~/.claude/settings.json (skipping)")
        return settings

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS_PATH.exists():
        backup = SETTINGS_PATH.with_name(
            f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}-agentboard-install"
        )
        shutil.copy2(SETTINGS_PATH, backup)
        print(f"  Backed up settings.json -> {backup}")

    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in _SIMPLE_EVENTS:
        changed = _ensure_hook(hooks, event, [_entry(HOOK_CMD)]) or changed
    for event in _SYNC_EVENTS:
        changed = (
            _ensure_hook(hooks, event, [_entry(HOOK_CMD), _entry(SYNC_CMD, timeout=15, is_async=True)])
            or changed
        )

    desired_statusline = {
        "type": "command",
        "command": STATUSLINE_CMD,
        "padding": 0,
        "refreshInterval": 5,
    }
    existing_statusline = settings.get("statusLine")
    if existing_statusline != desired_statusline:
        if existing_statusline and existing_statusline.get("command") != STATUSLINE_CMD:
            print(
                f"  NOTE: replacing existing statusLine command "
                f"({existing_statusline.get('command')!r}) with agent-board's"
            )
        settings["statusLine"] = desired_statusline
        changed = True

    if settings.get("agentPushNotifEnabled") is not False:
        settings["agentPushNotifEnabled"] = False
        changed = True
        print(
            "  Disabled Claude Code's built-in push notifications so "
            "agent-board's folder/model banners are the only local alerts"
        )

    if changed:
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
        print(f"  Wired agent-board hooks + statusLine into {SETTINGS_PATH}")

    return settings


def _overlap_check(settings: dict) -> None:
    print("\n  --- native-overlap check ---")
    push_enabled = settings.get("agentPushNotifEnabled")
    print(
        f"  agentPushNotifEnabled: {push_enabled!r} "
        "(kept false by agent-board to avoid duplicate folderless banners)"
    )

    try:
        sys.path.insert(0, str(REPO_DIR))
        from claude_browse.providers.claude import get_session_info, list_session_files

        files = list_session_files()
        recent = sorted(files, key=lambda p: os.path.getmtime(p), reverse=True)[:20]
        with_title = 0
        for f in recent:
            info = get_session_info(f)
            if info and info.get("name"):
                with_title += 1
        print(
            f"  existing ai-title coverage: {with_title}/{len(recent)} of your 20 most "
            "recent sessions already have a title (agent-board's Haiku namer only "
            "fires for the rest -- see board/naming.py)"
        )
    except Exception as exc:
        print(f"  ai-title coverage check skipped: {exc}")


def main() -> None:
    settings = _wire_hooks_and_statusline()
    _overlap_check(settings)


if __name__ == "__main__":
    main()

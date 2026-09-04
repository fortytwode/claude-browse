#!/usr/bin/env python3
"""Idempotently wire agent-board into ~/.claude/settings.json and
~/.codex/hooks.json.

Run by install.sh. Safe to re-run: reconciles our hook commands into whatever
groups already exist for each event (never wholesale-replaces a user's other
hooks for that event), removes STALE VARIANTS of our own commands (a sync
command that embeds an older python path) and EXACT DUPLICATES (the same
command registered twice, which double-posts every Slack alert), and backs up
each file before writing anything.

    python3 scripts/install_agent_board.py           # wire + report
    python3 scripts/install_agent_board.py --check   # report only, exit 1 on drift
    python3 scripts/install_agent_board.py --no-codex

Why the dedupe exists (found live, 2026-09-03): the sync hook command embeds
the interpreter path, which changes from system python3 to .venv/bin/python
once the board-sync venv is created. The old installer only ever APPENDED the
new command, so both fired on every Stop -- two Slack posts per event, and a
naming race between the two processes that made the same session alternate
between two names seconds apart.
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


def _settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "settings.json"


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _codex_hooks_path() -> Path:
    return _codex_home() / "hooks.json"


# Prefer the board-sync venv when it exists (has google-cloud-firestore,
# requests, anthropic); fall back to system python3 otherwise, so the async
# sync hook -- and with it, the Haiku namer in naming.py, which is only ever
# invoked from sync.push() -- is at least *reachable* on a fresh install
# before the optional venv is set up, rather than failing to launch at all.
# Re-running install.sh after creating the venv upgrades this automatically
# AND removes the previous variant (see _reconcile_event).
def _sync_python() -> str:
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else (sys.executable or "python3")


HOOK_CMD = f"{AGENT_BOARD} hook"
CODEX_HOOK_CMD = f"{AGENT_BOARD} hook --provider codex"
STATUSLINE_CMD = f"{AGENT_BOARD} statusline"


def sync_cmd() -> str:
    return f"{_sync_python()} {AGENT_BOARD} sync push"


_SIMPLE_EVENTS = ("SessionStart", "UserPromptSubmit")
_SYNC_EVENTS = ("Stop", "Notification", "SessionEnd")

# Codex has no Notification event; PermissionRequest is its blocked-on-you
# signal (hook.py maps it to needs-input). Event names are PascalCase in
# ~/.codex/hooks.json exactly as in Claude's settings.json (Codex's hooks
# engine is Claude-compatible: HooksFile{hooks: {Event: [MatcherGroup]}}).
_CODEX_SIMPLE_EVENTS = ("SessionStart", "UserPromptSubmit")
_CODEX_SYNC_EVENTS = ("Stop", "PermissionRequest", "SessionEnd")


def _is_our_command(command: str | None) -> bool:
    return bool(command) and "agent-board" in command and (
        command.endswith(" hook")
        or command.endswith(" hook --provider codex")
        or command.endswith(" sync push")
        or command.endswith(" statusline")
    )


def _is_sync_variant(command: str | None) -> bool:
    """Any '<python> <path>/agent-board sync push' from any install/venv."""
    return bool(command) and command.endswith(" sync push") and "agent-board" in command


def _entry(command: str, *, timeout: int = 10, is_async: bool = False, codex: bool = False) -> dict:
    e: dict = {"type": "command", "command": command}
    if codex:
        e["timeout_sec"] = timeout
    else:
        e["timeout"] = timeout
    if is_async:
        e["async"] = True
    return e


def _reconcile_event(hooks: dict, event: str, desired: list[dict]) -> dict:
    """Make `event`'s groups contain exactly one of each desired command.

    - Removes stale variants of the sync command (different interpreter path).
    - Removes exact duplicates of any of our commands (keeps the first).
    - Leaves every command that isn't ours untouched, in place.
    - Appends missing desired entries to the first group.
    Returns {"removed": [...], "added": [...]}.
    """
    desired_cmds = [d["command"] for d in desired]
    groups = hooks.setdefault(event, [])
    if not groups:
        groups.append({"hooks": []})

    removed: list[str] = []
    seen: set[str] = set()
    for group in groups:
        kept = []
        for entry in group.get("hooks", []):
            cmd = entry.get("command")
            stale_sync = _is_sync_variant(cmd) and cmd not in desired_cmds
            duplicate = _is_our_command(cmd) and cmd in seen
            if stale_sync or duplicate:
                removed.append(cmd)
                continue
            if _is_our_command(cmd):
                seen.add(cmd)
            kept.append(entry)
        group["hooks"] = kept

    # Drop groups we emptied entirely (but never a group that still carries
    # someone else's hooks), so a file doesn't accrete empty {"hooks": []}.
    hooks[event] = [g for g in groups if g.get("hooks") or g.get("matcher")] or [{"hooks": []}]
    groups = hooks[event]

    added: list[str] = []
    present = {h.get("command") for g in groups for h in g.get("hooks", [])}
    for d in desired:
        if d["command"] not in present:
            groups[0].setdefault("hooks", []).append(d)
            added.append(d["command"])
    return {"removed": removed, "added": added}


def _desired_claude(event: str) -> list[dict]:
    if event in _SIMPLE_EVENTS:
        return [_entry(HOOK_CMD)]
    return [_entry(HOOK_CMD), _entry(sync_cmd(), timeout=15, is_async=True)]


def _desired_codex(event: str) -> list[dict]:
    if event in _CODEX_SIMPLE_EVENTS:
        return [_entry(CODEX_HOOK_CMD, codex=True)]
    return [
        _entry(CODEX_HOOK_CMD, codex=True),
        _entry(sync_cmd(), timeout=15, is_async=True, codex=True),
    ]


def _drift(hooks: dict, events: tuple[str, ...], desired_fn) -> list[str]:
    """Human-readable list of problems, empty when fully wired and clean."""
    problems: list[str] = []
    for event in events:
        cmds = [h.get("command") for g in hooks.get(event, []) for h in g.get("hooks", [])]
        for d in desired_fn(event):
            if d["command"] not in cmds:
                problems.append(f"{event}: missing {d['command']!r}")
        ours = [c for c in cmds if _is_our_command(c)]
        for c in set(ours):
            if ours.count(c) > 1:
                problems.append(f"{event}: duplicate registration ({ours.count(c)}x) {c!r}")
        desired_cmds = {d["command"] for d in desired_fn(event)}
        for c in ours:
            if _is_sync_variant(c) and c not in desired_cmds:
                problems.append(f"{event}: stale sync variant {c!r}")
    return problems


def _backup(path: Path) -> None:
    backup = path.with_name(
        f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-agentboard-install"
    )
    shutil.copy2(path, backup)
    print(f"  Backed up {path.name} -> {backup}")


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text().strip()
    return json.loads(text) if text else {}


# ---------------------------------------------------------------------------
# Claude Code: ~/.claude/settings.json
# ---------------------------------------------------------------------------

def check_claude(settings: dict) -> list[str]:
    problems = _drift(settings.get("hooks", {}), _SIMPLE_EVENTS + _SYNC_EVENTS, _desired_claude)
    if (settings.get("statusLine") or {}).get("command") != STATUSLINE_CMD:
        problems.append("statusLine: not agent-board's")
    if settings.get("agentPushNotifEnabled") is not False:
        problems.append("agentPushNotifEnabled: not false (duplicate folderless banners)")
    return problems


def wire_claude(settings: dict) -> tuple[dict, bool]:
    """Returns (settings, changed)."""
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in _SIMPLE_EVENTS + _SYNC_EVENTS:
        result = _reconcile_event(hooks, event, _desired_claude(event))
        for cmd in result["removed"]:
            print(f"  Removed {event} hook: {cmd}")
        for cmd in result["added"]:
            print(f"  Added {event} hook: {cmd}")
        changed = changed or bool(result["removed"] or result["added"])

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
    return settings, changed


# ---------------------------------------------------------------------------
# Codex: ~/.codex/hooks.json
# ---------------------------------------------------------------------------

def check_codex(hooks_file: dict) -> list[str]:
    return _drift(
        hooks_file.get("hooks", {}), _CODEX_SIMPLE_EVENTS + _CODEX_SYNC_EVENTS, _desired_codex
    )


def wire_codex(hooks_file: dict) -> tuple[dict, bool]:
    hooks = hooks_file.setdefault("hooks", {})
    changed = False
    for event in _CODEX_SIMPLE_EVENTS + _CODEX_SYNC_EVENTS:
        result = _reconcile_event(hooks, event, _desired_codex(event))
        for cmd in result["removed"]:
            print(f"  Removed Codex {event} hook: {cmd}")
        for cmd in result["added"]:
            print(f"  Added Codex {event} hook: {cmd}")
        changed = changed or bool(result["removed"] or result["added"])
    return hooks_file, changed


def _codex_hooks_feature_note() -> str | None:
    """Codex's hooks engine is on by default in current releases; older ones
    needed `[features] hooks = true` in ~/.codex/config.toml. We never edit
    config.toml (no stdlib TOML writer, and it holds the user's model/auth
    settings) -- we only read it and say what to add if the flag is
    explicitly off."""
    config_path = _codex_home() / "config.toml"
    if not config_path.exists():
        return None
    try:
        import tomllib  # py3.11+
    except ImportError:  # pragma: no cover - py3.9/3.10
        return None
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception as exc:
        return f"could not parse {config_path}: {exc}"
    features = data.get("features") or {}
    if features.get("hooks") is False or features.get("codex_hooks") is False:
        return (
            f"{config_path} sets [features] hooks = false; Codex will ignore hooks.json. "
            "Set it to true (or delete the line) to enable."
        )
    return None


# ---------------------------------------------------------------------------

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


def run(*, check_only: bool = False, include_codex: bool = True) -> int:
    settings_path = _settings_path()
    settings = _load_json(settings_path)
    problems = check_claude(settings)

    codex_path = _codex_hooks_path()
    codex_present = _codex_home().exists()
    codex_file = _load_json(codex_path) if codex_present else {}
    codex_problems = check_codex(codex_file) if (include_codex and codex_present) else []

    if check_only:
        print(f"  {settings_path}:")
        for p in problems or ["    OK: hooks + statusLine wired once, no stale/duplicate commands"]:
            print(f"    {p}")
        if include_codex:
            if not codex_present:
                print(f"  {codex_path}: Codex home not found, skipped")
            else:
                print(f"  {codex_path}:")
                for p in codex_problems or ["    OK: Codex hooks wired once, no stale/duplicate commands"]:
                    print(f"    {p}")
        return 1 if (problems or codex_problems) else 0

    # --- Claude ---
    if not problems:
        print(f"  agent-board hooks + statusLine already wired cleanly in {settings_path} (skipping)")
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        if settings_path.exists():
            _backup(settings_path)
        settings, changed = wire_claude(settings)
        if changed:
            settings_path.write_text(json.dumps(settings, indent=2) + "\n")
            print(f"  Wired agent-board hooks + statusLine into {settings_path}")

    # --- Codex ---
    if include_codex:
        if not codex_present:
            print(f"  Codex home {_codex_home()} not found; skipping Codex hooks (install Codex, re-run)")
        elif not codex_problems:
            print(f"  agent-board Codex hooks already wired cleanly in {codex_path} (skipping)")
        else:
            if codex_path.exists():
                _backup(codex_path)
            codex_file, changed = wire_codex(codex_file)
            if changed:
                codex_path.write_text(json.dumps(codex_file, indent=2) + "\n")
                print(f"  Wired agent-board hooks into {codex_path}")
        note = _codex_hooks_feature_note()
        if note:
            print(f"  NOTE: {note}")

    _overlap_check(settings)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    return run(check_only="--check" in argv, include_codex="--no-codex" not in argv)


if __name__ == "__main__":
    sys.exit(main())

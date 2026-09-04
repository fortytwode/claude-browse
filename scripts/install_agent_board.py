#!/usr/bin/env python3
"""Idempotently wire Agent Board into Claude and Codex hook configuration.

Run by install.sh. Safe to re-run: canonicalizes every lifecycle event to one
dedicated, matcherless Agent Board hook while preserving all foreign handlers
and group metadata. Old install paths, sync siblings, duplicates, and entries
whose provider or options drifted are removed before the canonical hook is
appended. Each file is backed up before it is changed.

    python3 scripts/install_agent_board.py           # wire + report
    python3 scripts/install_agent_board.py --check   # report only, exit 1 on drift
    python3 scripts/install_agent_board.py --no-codex

The hook commits local state and then launches detached publication itself.
Registering sync as a sibling is both redundant and racy because hook runners
may launch matching commands concurrently.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time
from copy import deepcopy
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
AGENT_BOARD = str(REPO_DIR / "agent-board")


def _settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "settings.json"


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _codex_hooks_path() -> Path:
    return _codex_home() / "hooks.json"


HOOK_CMD = f"{AGENT_BOARD} hook"
CODEX_HOOK_CMD = f"{AGENT_BOARD} hook --provider codex"
STATUSLINE_CMD = f"{AGENT_BOARD} statusline"

_CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "Notification", "SessionEnd")

# Codex has no Notification event; PermissionRequest is its blocked-on-you
# signal (hook.py maps it to needs-input), while Interrupt returns an active
# turn to idle. Event names are PascalCase in
# ~/.codex/hooks.json exactly as in Claude's settings.json (Codex's hooks
# engine is Claude-compatible: HooksFile{hooks: {Event: [MatcherGroup]}}).
_CODEX_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "PermissionRequest",
    "Interrupt",
    "SessionEnd",
)
_CODEX_THREE_SECOND_EVENTS = {"Interrupt", "SessionEnd"}


def _is_our_command(command: str | None) -> bool:
    """Recognize every lifecycle handler managed by current or old installers."""
    if not isinstance(command, str):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for index, part in enumerate(parts):
        if Path(part).name != "agent-board":
            continue
        action = parts[index + 1 :]
        return bool(
            action
            and (
                action[0] in {"hook", "statusline"}
                or action[:2] == ["sync", "push"]
            )
        )
    return False


def _entry(command: str, *, timeout: int = 10) -> dict:
    return {"type": "command", "command": command, "timeout": timeout}


def _canonical_event(groups: object, desired: list[dict]) -> tuple[list, list[str]]:
    """Return matcher-safe canonical groups plus removed managed commands."""
    existing = groups if isinstance(groups, list) else []
    canonical: list = []
    removed: list[str] = []
    for raw_group in existing:
        if not isinstance(raw_group, dict):
            canonical.append(deepcopy(raw_group))
            continue
        group = deepcopy(raw_group)
        entries = group.get("hooks")
        if not isinstance(entries, list):
            canonical.append(group)
            continue
        kept = []
        for entry in entries:
            command = entry.get("command") if isinstance(entry, dict) else None
            if _is_our_command(command):
                removed.append(command)
            else:
                kept.append(entry)
        group["hooks"] = kept
        # A bare group containing only managed handlers is installer residue.
        # A matcher or any other metadata belongs to the user and is retained.
        if kept or set(group) != {"hooks"}:
            canonical.append(group)
    canonical.append({"hooks": deepcopy(desired)})
    return canonical, removed


def _reconcile_event(hooks: dict, event: str, desired: list[dict]) -> dict:
    """Replace all managed variants with one exact matcherless definition."""
    before = hooks.get(event, [])
    canonical, removed = _canonical_event(before, desired)
    changed = before != canonical
    if changed:
        hooks[event] = canonical
    return {
        "removed": removed if changed else [],
        "added": [entry["command"] for entry in desired] if changed else [],
        "changed": changed,
    }


def _desired_claude(event: str) -> list[dict]:
    return [_entry(HOOK_CMD)]


def _desired_codex(event: str) -> list[dict]:
    timeout = 3 if event in _CODEX_THREE_SECOND_EVENTS else 10
    return [_entry(CODEX_HOOK_CMD, timeout=timeout)]


def _drift(hooks: dict, events: tuple[str, ...], desired_fn) -> list[str]:
    """Compare full definitions and placement using the writer's canonical form."""
    problems: list[str] = []
    for event in events:
        current = hooks.get(event, [])
        canonical, removed = _canonical_event(current, desired_fn(event))
        if current != canonical:
            detail = f"; replacing {len(removed)} managed variant(s)" if removed else ""
            problems.append(
                f"{event}: Agent Board hook definition or placement differs from canonical{detail}"
            )
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
    problems = _drift(settings.get("hooks", {}), _CLAUDE_EVENTS, _desired_claude)
    desired_statusline = {
        "type": "command",
        "command": STATUSLINE_CMD,
        "padding": 0,
        "refreshInterval": 5,
    }
    if settings.get("statusLine") != desired_statusline:
        problems.append("statusLine: not agent-board's")
    if settings.get("agentPushNotifEnabled") is not False:
        problems.append("agentPushNotifEnabled: not false (duplicate folderless banners)")
    return problems


def wire_claude(settings: dict) -> tuple[dict, bool]:
    """Returns (settings, changed)."""
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in _CLAUDE_EVENTS:
        result = _reconcile_event(hooks, event, _desired_claude(event))
        for cmd in result["removed"]:
            print(f"  Removed {event} hook: {cmd}")
        for cmd in result["added"]:
            print(f"  Added {event} hook: {cmd}")
        changed = changed or result["changed"]

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
    return _drift(hooks_file.get("hooks", {}), _CODEX_EVENTS, _desired_codex)


def wire_codex(hooks_file: dict) -> tuple[dict, bool]:
    hooks = hooks_file.setdefault("hooks", {})
    changed = False
    for event in _CODEX_EVENTS:
        result = _reconcile_event(hooks, event, _desired_codex(event))
        for cmd in result["removed"]:
            print(f"  Removed Codex {event} hook: {cmd}")
        for cmd in result["added"]:
            print(f"  Added Codex {event} hook: {cmd}")
        changed = changed or result["changed"]
    return hooks_file, changed


def _codex_hooks_feature_problem() -> str | None:
    """Codex's hooks engine is on by default in current releases; older ones
    needed `[features] hooks = true` in ~/.codex/config.toml. We never edit
    config.toml (no stdlib TOML writer, and it holds the user's model/auth
    settings) -- we only read it and say what to add if the flag is off or
    the feature configuration cannot be verified."""
    config_path = _codex_home() / "config.toml"
    if not config_path.exists():
        return None
    text = config_path.read_text()
    try:
        import tomllib  # py3.11+
    except ImportError:  # pragma: no cover - exercised on supported py3.9/3.10
        # We only need two booleans from one table. Keep older supported
        # Pythons useful without adding a TOML dependency to this installer.
        in_features = False
        disabled = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line.startswith("[") and line.endswith("]"):
                in_features = line == "[features]"
                continue
            if in_features and "=" in line:
                key, value = (part.strip() for part in line.split("=", 1))
                if key in {"hooks", "codex_hooks"} and value.lower() == "false":
                    disabled = True
        data = {"features": {"hooks": False}} if disabled else {}
    else:
        try:
            data = tomllib.loads(text)
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
    feature_problem = (
        _codex_hooks_feature_problem() if include_codex and codex_present else None
    )

    if check_only:
        print(f"  {settings_path}:")
        for p in problems or ["    OK: hooks + statusLine wired once, no stale/duplicate commands"]:
            print(f"    {p}")
        if include_codex:
            if not codex_present:
                print(f"  {codex_path}: Codex home not found, skipped")
            else:
                print(f"  {codex_path}:")
                for p in codex_problems or ["    OK: Codex hook definitions match canonical configuration"]:
                    print(f"    {p}")
                if feature_problem:
                    print(f"    {feature_problem}")
                print(
                    "    NOTE: hook trust cannot be verified by --check. "
                    "Open Codex, run /hooks, and review/trust the Agent Board definitions."
                )
        return 1 if (problems or codex_problems or feature_problem) else 0

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
        codex_changed = False
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
                codex_changed = True
        if feature_problem:
            print(f"  WARNING: {feature_problem}")
        if codex_changed:
            print(
                "  ACTION REQUIRED: Open Codex, run /hooks, and review/trust the "
                "Agent Board definitions. Codex skips new or changed hooks until trusted."
            )

    _overlap_check(settings)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    return run(check_only="--check" in argv, include_codex="--no-codex" not in argv)


if __name__ == "__main__":
    sys.exit(main())

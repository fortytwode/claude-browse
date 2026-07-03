"""Claude Code hook dispatcher.

Entry point for SessionStart / UserPromptSubmit / Stop / Notification /
SessionEnd. Must never break a session: every path through main() exits 0,
and all local work is synchronous SQLite (fast). Network work (Haiku naming,
Firestore/Slack sync) is deliberately NOT triggered from here -- it runs from
a separate `agent-board sync` command registered as an async hook, so a slow
or failing network call can never block a turn (see board/sync.py, U6).
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

from claude_browse.board import notify, store

_NOTIFY_AFTER_S = 60

_NEEDS_INPUT_TYPES = {"permission_prompt", "agent_needs_input", "elicitation_dialog"}
# Explicitly ignored: idle_prompt (fires on ~60s of user inactivity -- mapping
# it to needs-input would notify on every unanswered turn), auth_success,
# elicitation_complete, elicitation_response, agent_completed (Stop already
# covers completion via the duration gate).


def _hostname() -> str:
    return socket.gethostname()


def _placeholder_name(cwd: str | None) -> str:
    if not cwd:
        return "(new session)"
    return Path(cwd).name or "(new session)"


def _name_from_prompt(prompt: str, *, max_words: int = 6, max_chars: int = 60) -> str:
    words = prompt.strip().split()
    name = " ".join(words[:max_words])
    return name[:max_chars]


def dispatch(payload: dict) -> None:
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if not session_id:
        return
    cwd = payload.get("cwd")

    if event == "SessionStart":
        if store.get(session_id) is None:
            store.upsert(
                session_id,
                host=_hostname(),
                cwd=cwd,
                state="idle",
                name=_placeholder_name(cwd),
                name_source="provisional",
            )

    elif event == "UserPromptSubmit":
        existing = store.get(session_id)
        fields: dict[str, object] = {
            "host": _hostname(),
            "cwd": cwd,
            "state": "working",
            "working_since": time.time(),
        }
        if existing is None or existing.get("name_source") != "haiku":
            prompt = payload.get("prompt", "")
            name = _name_from_prompt(prompt)
            if name:
                fields["name"] = name
                fields["name_source"] = "provisional"
        store.upsert(session_id, **fields)

    elif event == "Stop":
        row = store.get(session_id)
        working_since = row.get("working_since") if row else None
        store.set_state(session_id, "idle", cwd=cwd)
        if working_since and (time.time() - working_since) > _NOTIFY_AFTER_S:
            name = (row or {}).get("name") or _placeholder_name(cwd)
            notify.notify("✅ done", name)

    elif event == "Notification":
        notification_type = payload.get("notification_type")
        if notification_type in _NEEDS_INPUT_TYPES:
            row = store.get(session_id)
            store.set_state(session_id, "needs-input", cwd=cwd)
            name = (row or {}).get("name") or _placeholder_name(cwd)
            notify.notify("⏸️ needs your input", name)

    elif event == "SessionEnd":
        store.set_state(session_id, "ended", cwd=cwd)


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        dispatch(payload)
    except Exception:
        pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()

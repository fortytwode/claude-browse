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

# Fail-safe denylist, not an allowlist: any notification_type NOT in this set
# triggers needs-input by default, including one Claude Code introduces in a
# future version that this code doesn't know about yet. An allowlist would
# silently no-op on an unrecognized-but-genuinely-blocking future type --
# exactly the failure this feature exists to prevent.
_IGNORED_NOTIFICATION_TYPES = {
    "idle_prompt",  # fires on ~60s of user inactivity -- not "needs you", just "hasn't typed"
    "auth_success",
    "elicitation_complete",
    "elicitation_response",
    "agent_completed",  # Stop already covers completion via the duration gate
}


def _hostname() -> str:
    return socket.gethostname()


def _set_state(session_id: str, state: str, *, cwd: str | None) -> None:
    """store.set_state, always including host, and refreshing the heartbeat.

    Centralizes the host backfill here so a future new hook event handler
    can't reintroduce the host=None bug found in this session's own live
    data. Also bumps heartbeat_at: statusline.py's refreshInterval is the
    primary heartbeat source, but its actual invocation cadence during a
    long tool-heavy sequence turned out to be uncertain -- observed live in
    this session's own build (it showed 'gone' on the real Slack board
    while actively being worked on). Stop fires reliably on every turn per
    the verified hook contract, so it's a second, more dependable heartbeat
    source that doesn't depend on the statusline actually re-rendering.
    """
    store.set_state(session_id, state, cwd=cwd, host=_hostname())
    store.heartbeat(session_id)


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
        store.heartbeat(session_id)

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
        store.heartbeat(session_id)

    elif event == "Stop":
        row = store.get(session_id)
        working_since = row.get("working_since") if row else None
        _set_state(session_id, "idle", cwd=cwd)
        if working_since and (time.time() - working_since) > _NOTIFY_AFTER_S:
            name = (row or {}).get("name") or _placeholder_name(cwd)
            notify.notify("✅ done", name)
            # Same trigger as the local notification, so the async sync hook
            # knows to post a fresh Slack message too -- chat.update alone
            # doesn't re-notify Slack channel members (see sync.post_alert).
            store.set_pending_alert(session_id, "done")

    elif event == "Notification":
        notification_type = payload.get("notification_type")
        if notification_type not in _IGNORED_NOTIFICATION_TYPES:
            row = store.get(session_id)
            _set_state(session_id, "needs-input", cwd=cwd)
            name = (row or {}).get("name") or _placeholder_name(cwd)
            notify.notify("⏸️ needs your input", name)
            store.set_pending_alert(session_id, "needs-input")

    elif event == "SessionEnd":
        _set_state(session_id, "ended", cwd=cwd)


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

"""Claude Code / Codex hook dispatcher.

Entry point for SessionStart / UserPromptSubmit / Stop / Notification /
PermissionRequest / SessionEnd. Must never break a session: every path
through main() exits 0, and all local work is synchronous SQLite (fast).
Network work (Haiku naming, Firestore/Slack sync) is deliberately NOT
triggered from here -- it runs from a separate `agent-board sync` command
registered as an async hook, so a slow or failing network call can never
block a turn (see board/sync.py, U6).

Providers: Claude Code and Codex both deliver the same hook envelope on
stdin (`hook_event_name`, `session_id`, `cwd`, `model`, `transcript_path`,
`prompt`), so one dispatcher serves both. The only thing the payload does
NOT say is which CLI sent it, so the hook is registered as
`agent-board hook --provider codex` in ~/.codex/hooks.json and plain
`agent-board hook` (Claude) in ~/.claude/settings.json; the provider is
stored on the row and drives every resume command downstream. Codex has no
`Notification` event; its blocked-on-you signal is `PermissionRequest`,
which maps to the same needs-input state.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

from claude_browse.board import notify, store

_NOTIFY_AFTER_S = 60


def _unattended_min_turn_s() -> float:
    """Minimum turn length for a Stop to mark the session "finished, waiting
    for you" (store.mark_done). Default 0: EVERY completed turn counts.

    Duration is not the signal. Whether you came back is. A 10-second turn
    that ends with "which option do you want?" is more likely to be waiting
    on you than a 60-minute batch run, and the noise a length filter would
    guard against is already bounded downstream: the sweep only pings once a
    completion has sat unattended for 10 minutes, pings at most twice, and
    any prompt / ack / user-initiated exit clears it. Env-tunable (seconds)
    for anyone who does want a floor; unrelated to _NOTIFY_AFTER_S, which
    gates only the local banner."""
    raw = os.environ.get("AGENT_BOARD_UNATTENDED_MIN_TURN_S", "").strip()
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        value = 0.0
    return max(value, 0.0)


# SessionEnd reasons that mean the USER ended the session from the keyboard
# (Claude Code: /clear, /logout, Ctrl-D or /exit at the prompt; Codex: exit).
# Being there to end it is an implicit ack of whatever had finished. Anything
# else -- "other", a missing reason, a killed terminal that never fires
# SessionEnd at all -- keeps done_at: a finished run whose window vanished is
# the canonical forgotten thread.
_USER_ENDED_REASONS = {"clear", "logout", "prompt_input_exit", "exit", "user_exit", "quit"}


# Codex's equivalent of Claude's needs-input Notification. Codex has no
# Notification event at all, so this is the only blocked-on-you signal it
# emits; it carries tool_name/tool_input, none of which change the state.
_NEEDS_INPUT_EVENTS = {"PermissionRequest"}

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


def _set_state(
    session_id: str,
    state: str,
    *,
    cwd: str | None,
    model_label: str | None = None,
) -> None:
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
    store.set_state(session_id, state, cwd=cwd, host=_hostname(), model_label=model_label)
    store.heartbeat(session_id)


def _placeholder_name(cwd: str | None) -> str:
    if not cwd:
        return "(new session)"
    return Path(cwd).name or "(new session)"


def _folder_name(cwd: str | None) -> str:
    if not cwd:
        return ""
    return Path(cwd).name


def _raw_model_from_payload(payload: dict) -> str:
    model = payload.get("model")
    if isinstance(model, dict):
        for key in ("display_name", "name", "id", "model"):
            value = model.get(key)
            if value:
                return str(value)
    elif model:
        return str(model)

    for key in ("model_label", "model_name"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _model_from_transcript(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256_000))
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = obj.get("message")
        if isinstance(message, dict) and message.get("model"):
            return str(message["model"])
    return ""


def _compact_model_label(raw: str) -> str:
    cleaned = raw.strip()
    if not cleaned:
        return ""
    folded = cleaned.lower().replace("_", "-")
    if "codex" in folded:
        return "Codex"
    if "fable" in folded:
        return "Fable"
    if "opus" in folded:
        return "Opus"
    if "sonnet" in folded:
        return "Sonnet"
    if "haiku" in folded:
        return "Haiku"
    if "claude" in folded:
        return "Claude"
    return " ".join(part.capitalize() for part in cleaned.replace("-", " ").split()[:2])


def _model_label(payload: dict, row: dict | None = None) -> str:
    raw = (
        _raw_model_from_payload(payload)
        or _model_from_transcript(payload.get("transcript_path"))
        or str((row or {}).get("model_label") or "")
    )
    return _compact_model_label(raw)


def _notify_title(action: str, cwd: str | None, model_label: str = "") -> str:
    """Put the [folder] tag first in the title, the visible banner anchor.

    User feedback: a banner showing only a stale name like 'Continue CodeX
    session context' says nothing about WHICH project's thread needs you.
    macOS often emphasizes or only shows the notification title, so keep the
    repo and model before the action instead of at the truncated tail.
    """
    folder = _folder_name(cwd)
    prefix = f"[{folder}] " if folder else ""
    model = f"{model_label} " if model_label else ""
    return f"{prefix}{model}{action}"


def _notify_body(name: str, cwd: str | None) -> str:
    """Just the session name. The [folder] tag lives in the TITLE only
    (_notify_title) -- two authors added it to both surfaces independently,
    which shipped banners like 'title: [claude-browse] Opus done / body:
    final qa smoke test [claude-browse]' with the folder twice (user-caught
    bug). The title always carries the folder, so the body never needs it.
    """
    del cwd
    return name


def _name_from_prompt(prompt: str, *, max_words: int = 6, max_chars: int = 60) -> str:
    words = prompt.strip().split()
    name = " ".join(words[:max_words])
    return name[:max_chars]


def _parse_provider(argv: list[str]) -> str:
    """`agent-board hook [--provider NAME]` -- default claude. Tolerant of
    anything else on the line; a hook must never fail on argv parsing."""
    for i, arg in enumerate(argv):
        if arg == "--provider" and i + 1 < len(argv):
            candidate = argv[i + 1].strip().lower()
            if candidate:
                return candidate
        elif arg.startswith("--provider="):
            candidate = arg.split("=", 1)[1].strip().lower()
            if candidate:
                return candidate
    return store.DEFAULT_PROVIDER


def dispatch(payload: dict, provider: str = store.DEFAULT_PROVIDER) -> None:
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if not session_id:
        return
    cwd = payload.get("cwd")
    row = store.get(session_id)
    model_label = _model_label(payload, row)

    if event == "SessionStart":
        if row is None:
            store.upsert(
                session_id,
                host=_hostname(),
                cwd=cwd,
                state="idle",
                name=_placeholder_name(cwd),
                name_source="provisional",
                provider=provider,
                **({"model_label": model_label} if model_label else {}),
            )
        else:
            fields: dict[str, object] = {"provider": provider}
            if model_label:
                fields["model_label"] = model_label
            store.upsert(session_id, **fields)
        store.heartbeat(session_id)

    elif event == "UserPromptSubmit":
        fields = {
            "host": _hostname(),
            "cwd": cwd,
            "state": "working",
            "working_since": time.time(),
            "provider": provider,
            # A new prompt means you are back in this thread: whatever
            # finished before is no longer "waiting for you". This is the
            # implicit ack that keeps the unattended list honest without
            # the user ever having to press anything.
            "done_at": None,
            "done_turn_s": None,
        }
        if model_label:
            fields["model_label"] = model_label
        if row is None or row.get("name_source") != "haiku":
            prompt = payload.get("prompt", "")
            name = _name_from_prompt(prompt)
            if name:
                fields["name"] = name
                fields["name_source"] = "provisional"
        store.upsert(session_id, **fields)
        store.heartbeat(session_id)

    elif event == "Stop":
        working_since = row.get("working_since") if row else None
        _set_state(session_id, "idle", cwd=cwd, model_label=model_label or None)
        turn_s = (time.time() - working_since) if working_since else 0.0
        if working_since and turn_s > _NOTIFY_AFTER_S:
            name = (row or {}).get("name") or _placeholder_name(cwd)
            notify.notify(_notify_title("done", cwd, model_label), _notify_body(name, cwd))
            # Same trigger as the local notification, so the async sync hook
            # knows to post a fresh Slack message too -- chat.update alone
            # doesn't re-notify Slack channel members (see sync.post_alert).
            store.set_pending_alert(session_id, "done")
        if working_since and turn_s >= _unattended_min_turn_s():
            store.mark_done(session_id, turn_s)

    elif event == "Notification" or event in _NEEDS_INPUT_EVENTS:
        notification_type = payload.get("notification_type")
        if event in _NEEDS_INPUT_EVENTS or notification_type not in _IGNORED_NOTIFICATION_TYPES:
            _set_state(session_id, "needs-input", cwd=cwd, model_label=model_label or None)
            name = (row or {}).get("name") or _placeholder_name(cwd)
            notify.notify(_notify_title("needs input", cwd, model_label), _notify_body(name, cwd))
            store.set_pending_alert(session_id, "needs-input")

    elif event == "SessionEnd":
        _set_state(session_id, "ended", cwd=cwd, model_label=model_label or None)
        reason = str(payload.get("reason") or "").strip().lower()
        if reason in _USER_ENDED_REASONS:
            # You ended it on purpose, so you saw where it left off.
            store.clear_done(session_id)
        # Otherwise done_at deliberately survives: a run that finished and
        # whose window then vanished is the canonical forgotten thread.


def main() -> None:
    try:
        provider = _parse_provider(sys.argv[1:])
        raw = sys.stdin.read()
        payload = json.loads(raw)
        dispatch(payload, provider=provider)
    except Exception:
        pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()

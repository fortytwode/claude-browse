"""Claude Code / Codex hook dispatcher.

Entry point for SessionStart / UserPromptSubmit / Stop / Notification /
PermissionRequest / Interrupt / SessionEnd. Must never break a session: every path
through main() exits 0, and all local work is synchronous SQLite (fast).
After a recognized event commits locally, main launches a detached
`agent-board sync push <session-id>` worker. Network work never blocks the
hook, and sync.py serializes workers before reading SQLite so an older worker
cannot overwrite a newer local transition.

Providers: Claude Code and Codex both deliver the same hook envelope on
stdin (`hook_event_name`, `session_id`, `cwd`, `model`, `transcript_path`,
`prompt`), so one dispatcher serves both. The only thing the payload does
NOT say is which CLI sent it, so the hook is registered as
`agent-board hook --provider codex` in ~/.codex/hooks.json and plain
`agent-board hook` (Claude) in ~/.claude/settings.json; the provider is
stored on the row and drives every resume command downstream. Codex has no
`Notification` event; its blocked-on-you signal is `PermissionRequest`,
which maps to the same needs-input state. Its `Interrupt` event returns an
active turn to idle without treating the interrupted turn as completed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from claude_browse.board import notify, store

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENTRY_SCRIPT = _REPO_ROOT / "agent-board"
_REPO_VENV_PYTHON = _REPO_ROOT / ".venv" / "bin" / "python"
_SYNC_INTERPRETER = _REPO_VENV_PYTHON if _REPO_VENV_PYTHON.exists() else Path(sys.executable)


def _unattended_min_turn_s() -> float:
    """Minimum turn length for a Stop to mark the session "finished, waiting
    for you" (store.mark_done). Default 0: EVERY completed turn counts.

    Duration is not the signal. Whether you came back is. A 10-second turn
    that ends with "which option do you want?" is more likely to be waiting
    on you than a 60-minute batch run, and the noise a length filter would
    guard against is already bounded downstream: the sweep only pings once a
    completion has sat unattended for 10 minutes, pings at most twice, and
    a new prompt or an ack clears it. Env-tunable (seconds)
    for anyone who does want a floor."""
    raw = os.environ.get("AGENT_BOARD_UNATTENDED_MIN_TURN_S", "").strip()
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        value = 0.0
    return max(value, 0.0)


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
    # Claude emits this when a quota pause ends and work resumes by itself.
    # Treating it as needs-input would announce precisely the opposite state.
    "quota_auto_resume_fired",
    "auth_success",
    "elicitation_complete",
    "elicitation_response",
    "agent_completed",  # Stop already covers completion
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
    """Refresh host and heartbeat whenever a hook updates runtime state."""
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


def _notify_body(name: str) -> str:
    """Keep the folder tag in the notification title only."""
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


def _capture_work(session_id: str, *, reactivate_done: bool = False) -> dict | None:
    """Best-effort local overlay capture; runtime hooks must remain fail-open."""
    try:
        from claude_browse.board import work_items

        row = store.get(session_id)
        return work_items.ensure_for_session(
            row or {}, reactivate_done=reactivate_done
        )
    except Exception:
        return None


def dispatch(payload: dict, provider: str = store.DEFAULT_PROVIDER) -> bool:
    """Apply one recognized state transition and report whether it mutated."""
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if not session_id:
        return False
    cwd = payload.get("cwd")
    row = store.get(session_id)
    model_label = _model_label(payload, row)
    transcript_path = payload.get("transcript_path")
    transcript_fields = (
        {"transcript_path": transcript_path}
        if isinstance(transcript_path, str) and transcript_path
        else {}
    )
    if (
        transcript_fields
        and event in {"Stop", "Notification", "SessionEnd", *_NEEDS_INPUT_EVENTS}
    ):
        # Some provider hook streams begin mid-session. Preserve the source
        # path from whichever recognized event arrives first so guarded resume
        # does not depend on the FTS refresh having run already.
        store.upsert(session_id, **transcript_fields)
        row = store.get(session_id)

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
                **transcript_fields,
                **({"model_label": model_label} if model_label else {}),
            )
        else:
            fields: dict[str, object] = {"provider": provider}
            fields.update(transcript_fields)
            if cwd is not None:
                fields["cwd"] = cwd
            if model_label:
                fields["model_label"] = model_label
            store.upsert(session_id, **fields)
        store.heartbeat(session_id)
        _capture_work(session_id)
        return True

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
            "pending_alert": None,
            **transcript_fields,
        }
        if model_label:
            fields["model_label"] = model_label
        if row is None or row.get("name_source") not in {"haiku", "manual"}:
            prompt = payload.get("prompt", "")
            name = _name_from_prompt(prompt)
            if name:
                fields["name"] = name
                fields["name_source"] = "provisional"
        store.upsert(session_id, **fields)
        store.heartbeat(session_id)
        _capture_work(session_id, reactivate_done=True)
        return True

    elif event == "Stop":
        if row is None:
            _set_state(session_id, "idle", cwd=cwd, model_label=model_label or None)
            row = store.get(session_id)
        working_since = row.get("working_since") if row else None
        turn_s = (time.time() - working_since) if working_since else 0.0
        _capture_work(session_id)
        should_notify = False
        if working_since:
            from claude_browse.board import work_items

            finished, should_notify = work_items.finish_turn(
                session_id,
                working_since,
                turn_s,
                cwd=cwd,
                host=_hostname(),
                model_label=model_label or None,
                mark_unattended=turn_s >= _unattended_min_turn_s(),
            )
            if not finished:
                # A duplicate Stop or a Stop for a superseded turn must not
                # replay the banner or overwrite a newer prompt's state.
                return False
        else:
            _set_state(session_id, "idle", cwd=cwd, model_label=model_label or None)
        if working_since and should_notify:
            name = (row or {}).get("name") or _placeholder_name(cwd)
            notify.notify(_notify_title("done", cwd, model_label), _notify_body(name))
        return True

    elif event == "Notification" or event in _NEEDS_INPUT_EVENTS:
        notification_type = payload.get("notification_type")
        if event in _NEEDS_INPUT_EVENTS or notification_type not in _IGNORED_NOTIFICATION_TYPES:
            _set_state(session_id, "needs-input", cwd=cwd, model_label=model_label or None)
            name = (row or {}).get("name") or _placeholder_name(cwd)
            notify.notify(_notify_title("needs input", cwd, model_label), _notify_body(name))
            store.set_pending_alert(session_id, "needs-input")
            _capture_work(session_id)
            return True
        _capture_work(session_id)
        return False

    elif event == "Interrupt" and provider == "codex":
        # An interrupted turn did not complete, so it must not appear in the
        # unattended queue or emit the same alerts as Stop. Clear any older
        # completion metadata defensively in case a partial event sequence
        # arrives after a resumed session.
        store.upsert(
            session_id,
            host=_hostname(),
            cwd=cwd,
            state="idle",
            provider=provider,
            working_since=None,
            done_at=None,
            done_turn_s=None,
            pending_alert=None,
            **transcript_fields,
            **({"model_label": model_label} if model_label else {}),
        )
        store.heartbeat(session_id)
        _capture_work(session_id)
        return True

    elif event == "SessionEnd":
        # done_at deliberately survives SessionEnd, however the session ended
        # (/exit, /clear, a killed window): a finished turn you have not come
        # back to is still a thread you can resume, and the board's job is
        # to keep it visible until you do, or ack it.
        _set_state(session_id, "ended", cwd=cwd, model_label=model_label or None)
        _capture_work(session_id)
        return True

    return False


def _spawn_sync(session_id: str) -> None:
    """Launch best-effort publication without extending hook latency."""
    if os.environ.get("AGENT_BOARD_DISABLE_SYNC", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return
    try:
        subprocess.Popen(
            [
                str(_SYNC_INTERPRETER),
                str(_ENTRY_SCRIPT),
                "sync",
                "push",
                "--coalesce",
                session_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        pass


def main() -> None:
    try:
        provider = _parse_provider(sys.argv[1:])
        raw = sys.stdin.read()
        payload = json.loads(raw)
        if dispatch(payload, provider=provider):
            session_id = str(payload["session_id"])
            store.mark_sync_pending(session_id)
            _spawn_sync(session_id)
    except Exception:
        pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()

"""One-use local launch intents connecting a terminal restart to its task.

The shell receives only a random token. Provider, destination and permission
choices stay in the local database and are rechecked when Terminal executes.
No transcript is moved, rewritten or deleted by this module.
"""
from __future__ import annotations

import os
import re
import secrets
import shlex
import sqlite3
import time

from claude_browse.providers import get_provider

from . import commands, store, work_items, workspace

TOKEN_ENV = "AGENT_BOARD_LAUNCH_TOKEN"
_TTL_S = 15 * 60
_SCHEMA = """CREATE TABLE IF NOT EXISTS workspace_launch_intents (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    full_access INTEGER NOT NULL,
    source_session_id TEXT,
    list_key TEXT NOT NULL,
    working_directory TEXT NOT NULL,
    launch_revision TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    error TEXT,
    adopted_session_id TEXT
)"""


def _conn() -> sqlite3.Connection:
    conn = store.get_conn()
    conn.execute(_SCHEMA)
    return conn


def _available(provider: str) -> bool:
    return get_provider(provider).is_available()


def _token(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise ValueError("invalid launch token")
    return value


def _provider(value: str) -> str:
    if value not in ("claude", "codex"):
        raise ValueError("provider must be claude or codex")
    return value


def _same_directory(first: str | None, second: str | None) -> bool:
    return bool(first and second and os.path.realpath(first) == os.path.realpath(second))


def action_status(session: dict | None, context: dict, provider: str, *, availability_check=None) -> dict:
    """Destination-aware action; changed directory is a fresh handoff, not resume."""
    spec = get_provider(_provider(provider))
    native = bool(session and session.get("provider", "claude") == provider
                  and _same_directory(session.get("cwd"), context.get("working_directory")))
    mode = "native" if native else "handoff" if session else "new"
    label = f"Resume {spec.display_name}" if native else (
        f"Continue in {spec.display_name}" if session else f"Start {spec.display_name}"
    )
    cwd = context.get("working_directory")
    reason = None
    if not cwd:
        reason = "Link a working folder on this Mac before starting."
    elif not os.path.isdir(cwd):
        reason = "The linked working folder is missing on this Mac. Relink it before starting."
    elif not (availability_check or _available)(provider):
        reason = f"{spec.display_name} is not installed on this Mac."
    elif mode == "handoff" and not os.path.isfile(str(session.get("path") or "")):
        reason = "The original transcript is unavailable for a handoff. Your task and history are unchanged."
    return {"label": label, "available": reason is None, "reason": reason, "mode": mode}


def _resolve(kind: str, target_id: str) -> tuple[dict, dict | None]:
    if kind == "task":
        task = work_items.get(target_id)
        if task is None or not task.get("session_id"):
            raise ValueError("task not found")
        session = commands.session_for_launch(task["session_id"])
        if session is None:
            raise ValueError("The original session is unavailable; the task has been preserved.")
        return workspace.context_for_task(task), session
    if kind == "list":
        return workspace.context_for_list(target_id), None
    raise ValueError("launch kind must be task or list")


def prepare(kind: str, target_id: str, provider: str, *, full_access: bool, launch_revision: str) -> str:
    _provider(provider)
    if not isinstance(full_access, bool):
        raise ValueError("full_access must be a boolean")
    context, session = _resolve(kind, target_id)
    if launch_revision != context["launch_revision"]:
        raise ValueError("The task or working folder changed. Refresh and start again.")
    status = action_status(session, context, provider)
    if not status["available"]:
        raise ValueError(status["reason"])
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            """SELECT 1 FROM workspace_launch_intents WHERE kind = ? AND target_id = ?
               AND state IN ('prepared', 'claimed', 'adopting') AND expires_at > ?""",
            (kind, target_id, now),
        ).fetchone()
        if pending:
            raise ValueError("A launch is already pending. Check its Terminal window before retrying.")
        conn.execute(
            """INSERT INTO workspace_launch_intents
               (token,kind,target_id,provider,full_access,source_session_id,list_key,
                working_directory,launch_revision,state,created_at,expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,'prepared',?,?)""",
            (token, kind, target_id, provider, int(full_access), (session or {}).get("session_id"),
             context["list_key"], context["working_directory"], launch_revision, now, now + _TTL_S),
        )
    return token


def command(token: str) -> str:
    return shlex.join([commands._agent_board_executable(), "launch-intent", _token(token)])


def get(token: str) -> dict | None:
    row = _conn().execute("SELECT * FROM workspace_launch_intents WHERE token = ?", (_token(token),)).fetchone()
    return dict(row) if row is not None else None


def fail(token: str, error: str, *, expected_state: str | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            """UPDATE workspace_launch_intents SET state = 'failed', error = ?
               WHERE token = ? AND state != 'consumed' AND (? IS NULL OR state = ?)""",
            (str(error)[:1000], _token(token), expected_state, expected_state),
        )


def _check_current(intent: dict) -> tuple[dict, dict | None]:
    context, session = _resolve(intent["kind"], intent["target_id"])
    if context["launch_revision"] != intent["launch_revision"]:
        raise ValueError("The task or working folder changed. Refresh and start again.")
    return context, session


def claim(token: str) -> dict:
    intent = get(token)
    if intent is None or intent["expires_at"] <= time.time():
        raise ValueError("Launch request expired or was not found. Start again from the board.")
    if intent["state"] != "prepared":
        raise ValueError("Launch request has already been used.")
    context, session = _check_current(intent)
    status = action_status(session, context, intent["provider"])
    if not status["available"]:
        raise ValueError(status["reason"])
    with _conn() as conn:
        changed = conn.execute(
            "UPDATE workspace_launch_intents SET state = 'claimed' WHERE token = ? AND state = 'prepared' AND expires_at > ?",
            (token, time.time()),
        ).rowcount
    if changed != 1:
        raise ValueError("Launch request has already been used or expired.")
    return {**intent, "state": "claimed"}


def execute(token: str) -> None:
    """CLI only: revalidate, set cwd and then replace this process with the agent."""
    previous_token = os.environ.get(TOKEN_ENV)
    claimed = False
    try:
        intent = claim(token)
        claimed = True
        context, session = _check_current(intent)
        provider = intent["provider"]
        cwd = context["working_directory"]
        os.chdir(cwd)
        os.environ[TOKEN_ENV] = token
        if session:
            from claude_browse import browse

            browse._open_in_target_provider(
                session, session.get("provider", "claude"), provider,
                session["session_id"], cwd, (), bool(intent["full_access"]),
                fork=None, relocate=not _same_directory(session.get("cwd"), cwd),
            )
        else:
            spec = get_provider(provider)
            argv = [spec.binary]
            if intent["full_access"] and spec.handoff_yolo_flag:
                argv.append(spec.handoff_yolo_flag)
            os.execvp(spec.binary, argv)
    except (Exception, SystemExit):
        # A successful exec never returns here. Restore only the local
        # launcher's environment on failure; hooks must remain independent.
        if previous_token is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = previous_token
        try:
            fail(token, "Terminal could not start this request. Check its error and try again.",
                 expected_state="claimed" if claimed else "prepared")
        except (ValueError, sqlite3.Error):
            pass
        raise


def adopt_session(session_id: str, provider: str) -> bool:
    """SessionStart only. Unmatched or inherited/used tokens are harmless."""
    token = os.environ.get(TOKEN_ENV)
    if not token:
        return False
    intent = get(token)
    session = store.get(session_id)
    if (not intent or not session or intent["state"] != "claimed"
            or intent["expires_at"] <= time.time() or intent["provider"] != provider
            or not _same_directory(intent["working_directory"], session.get("cwd"))):
        return False
    try:
        _check_current(intent)
    except ValueError:
        fail(token, "The task or working folder changed before the conversation started.", expected_state="claimed")
        raise
    with _conn() as conn:
        changed = conn.execute(
            """UPDATE workspace_launch_intents SET state = 'adopting'
               WHERE token = ? AND state = 'claimed' AND expires_at > ?""", (token, time.time()),
        ).rowcount
    if changed != 1:
        return False
    try:
        if intent["kind"] == "task":
            if session_id != intent["source_session_id"]:
                work_items.attach_continuation(intent["target_id"], session, intent["source_session_id"])
        else:
            task = work_items.ensure_for_session(session)
            current = workspace.context_for_task(task)
            workspace.move_task(task["task_id"], intent["list_key"], current["list_key"])
        with _conn() as conn:
            conn.execute(
                """UPDATE workspace_launch_intents SET state = 'consumed', adopted_session_id = ?
                   WHERE token = ? AND state = 'adopting'""", (session_id, token),
            )
        return True
    except Exception:
        fail(token, "The new conversation could not be linked. Original history is unchanged.")
        raise

"""Cross-laptop sync: mirrors local session state to Firestore, and (U7)
renders the Slack #agent-status board from it.

Invoked out-of-band as an async hook sibling on Stop/Notification/SessionEnd
(see settings.json) -- never from the hot hook path in board/hook.py, so a
slow or failing network call can never block a turn.

Firestore auth: Application Default Credentials (already verified to
resolve to the team-projects-480520 project on this machine via
`gcloud auth application-default login`). Slack: SLACK_BOT_TOKEN, read from
the environment or falling back to team-operations/.env, since hooks run
with a minimal environment that usually won't have it exported.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from claude_browse.board import naming, store

PROJECT = "team-projects-480520"
DATABASE = "creative-dashboard"
COLLECTION = "agent_board_sessions"

_LOG_PATH = Path.home() / ".claude" / "agent-board" / "sync.log"
_DEFAULT_ENV_FILE = Path.home() / "team-operations" / ".env"


def _log(message: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a") as f:
            f.write(f"{time.time()} {message}\n")
    except Exception:
        pass


def _load_env_fallback() -> None:
    """Fill missing os.environ keys from team-operations/.env.

    Hooks run with a minimal inherited environment -- SLACK_BOT_TOKEN and
    friends usually live only in team-operations/.env, not in the shell
    that launched Claude Code. Never overwrites an already-set env var.
    """
    env_path = Path(os.environ.get("AGENT_BOARD_ENV_FILE") or _DEFAULT_ENV_FILE)
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


def _firestore_client():
    from google.cloud import firestore

    return firestore.Client(project=PROJECT, database=DATABASE)


def push(session_id: str) -> None:
    """Mirror this session's local state to Firestore. Best-effort, never raises."""
    try:
        row = store.get(session_id)
        if row is None:
            return

        naming.maybe_name(session_id)
        row = store.get(session_id) or row  # re-read in case the name just upgraded

        host = row.get("host") or "unknown-host"
        doc_id = f"{host}:{session_id}"

        client = _firestore_client()
        client.collection(COLLECTION).document(doc_id).set(
            {
                "session_id": session_id,
                "host": host,
                "name": row.get("name"),
                "state": row.get("state"),
                "cwd": row.get("cwd"),
                "updated_at": row.get("updated_at"),
                "heartbeat_at": row.get("heartbeat_at"),
            }
        )
    except Exception as exc:
        _log(f"push failed for session_id={session_id}: {exc}")


def check() -> str:
    """Report creds source + connectivity per backend. Used by `agent-board sync check`."""
    lines = []

    try:
        client = _firestore_client()
        list(client.collection(COLLECTION).limit(1).stream())
        lines.append(f"firestore: OK (project={PROJECT}, database={DATABASE})")
    except Exception as exc:
        lines.append(f"firestore: DEGRADED - {exc}")

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        lines.append("slack: DEGRADED - SLACK_BOT_TOKEN not found in env or team-operations/.env")
    else:
        try:
            import requests

            resp = requests.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            data = resp.json()
            if data.get("ok"):
                lines.append(f"slack: OK (team={data.get('team')})")
            else:
                lines.append(f"slack: DEGRADED - auth.test error: {data.get('error')}")
        except Exception as exc:
            lines.append(f"slack: DEGRADED - {exc}")

    return "\n".join(lines)


def main(argv: list[str]) -> None:
    if not argv:
        print("usage: agent-board sync <push|check>", file=sys.stderr)
        sys.exit(1)

    subcommand = argv[0]
    _load_env_fallback()

    if subcommand == "push":
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw)
            session_id = payload.get("session_id")
            if session_id:
                push(session_id)
        except Exception as exc:
            _log(f"sync push main() failed: {exc}")
        sys.exit(0)

    elif subcommand == "check":
        print(check())
        sys.exit(0)

    else:
        print(f"unknown sync subcommand: {subcommand}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])

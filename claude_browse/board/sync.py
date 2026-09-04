"""Cross-laptop sync: mirrors local session state to Firestore, and (U7)
renders the Slack #agent-status board from it.

Invoked out-of-band by a detached worker launched only after board/hook.py
commits the local transition. Workers serialize before reading SQLite, so a
late-starting process always publishes the newest committed local state.

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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from claude_browse.board import naming, store

# Board sync targets YOUR Firestore project -- override via env for any
# deployment that isn't the original author's fleet. Defaults preserve
# existing installs.
PROJECT = os.environ.get("CLAUDE_BROWSE_BOARD_PROJECT", "team-projects-480520")
DATABASE = os.environ.get("CLAUDE_BROWSE_BOARD_DATABASE", "creative-dashboard")
COLLECTION = os.environ.get(
    "CLAUDE_BROWSE_BOARD_COLLECTION", "agent_board_sessions"
)
META_COLLECTION = "agent_board_meta"
META_DOC = "slack"
# Channel ID, not name -- chat.postMessage/chat.update resolution by name is
# unreliable for private channels. #agent-status, private, bot is a member.
SLACK_CHANNEL = "C0BFW39EXBJ"

_LOG_PATH = Path.home() / ".claude" / "agent-board" / "sync.log"
_PUBLICATION_LOCK_PATH = _LOG_PATH.with_name("publication.lock")
_DEFAULT_ENV_FILE = Path.home() / "team-operations" / ".env"


def _log(message: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a") as f:
            f.write(f"{time.time()} {message}\n")
    except Exception:
        pass


def _strip_env_value(value: str) -> str:
    """Strip surrounding quotes, or an inline `# comment` on an unquoted value."""
    value = value.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value.strip(quote)
    idx = value.find("#")
    while idx != -1:
        if idx == 0 or value[idx - 1].isspace():
            return value[:idx].rstrip()
        idx = value.find("#", idx + 1)
    return value


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
            value = _strip_env_value(value)
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


_firestore_client_cache = None


def _firestore_client():
    """Cached per-process -- push() alone calls this up to 4 times
    (directly, plus via _fetch_all_session_docs/_get_stored_slack_ts/
    _store_slack_ts); constructing a fresh Client (ADC/gRPC setup) each
    time was pure waste within one invocation."""
    global _firestore_client_cache
    if _firestore_client_cache is None:
        from google.cloud import firestore

        _firestore_client_cache = firestore.Client(project=PROJECT, database=DATABASE)
    return _firestore_client_cache


def _immediate_done_alert_enabled() -> bool:
    """Whether a "done" transition posts a fresh Slack message the instant
    the turn ends. Default OFF since the unattended redesign: an immediate
    "done" cannot tell a run you walked away from apart from a turn you sat
    and watched, so on an interactive evening it produced ~15 alerts from 3
    threads and trained the channel to be ignored. Unattended completions
    are now surfaced by the team-operations sweep only once you have NOT
    come back for a while. "needs-input" is always immediate -- a blocked
    session is urgent regardless of whether you're watching.
    Set AGENT_BOARD_IMMEDIATE_DONE_ALERT=1 to restore the old behaviour."""
    return os.environ.get("AGENT_BOARD_IMMEDIATE_DONE_ALERT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def post_alert(
    session_id: str,
    kind: str,
    name: str,
    folder: str | None = None,
    model_label: str | None = None,
    provider: str = store.DEFAULT_PROVIDER,
) -> None:
    """Post a fresh Slack message (chat.postMessage, not update) for a
    transition that needs the user's attention. chat.update -- what the
    board itself uses -- edits a message in place, which Slack does not
    treat as a new notification for channel members; only an actual new
    message does. This is a second, distinct message alongside the board,
    not a replacement for it.

    `folder` (the session cwd's basename, e.g. "claude-browse") anchors the
    alert to a repo at a glance -- user feedback: the auto-name alone
    didn't say which project the thread belonged to.
    """
    # yolo=True per user request: alert resume commands include the
    # provider's skip-permissions flag so re-entry is one paste. Built via
    # the provider (not a hardcoded string) so each provider's own flag is
    # used automatically -- and via the ROW's provider, so a Codex thread
    # gets `codex resume`, never a `claude --resume` that cannot open it.
    resume_hint = resume_command(session_id, provider)
    tag = f" `[{folder}]`" if folder else ""
    model = f" · {model_label}" if model_label else ""
    if kind == "needs-input":
        body = f"⏸️ *{name}*{tag}{model} — needs your input\n`{resume_hint}`"
    else:
        body = f"✅ *{name}*{tag}{model} — done\n`{resume_hint}`"
    _slack_post_message(body)


def resume_command(session_id: str, provider: str | None) -> str:
    """One-paste resume command for a session, built from its provider."""
    from claude_browse.providers import get_provider

    provider_id = provider or store.DEFAULT_PROVIDER
    try:
        spec = get_provider(provider_id)
    except Exception:
        spec = get_provider(store.DEFAULT_PROVIDER)
    return " ".join(spec.native_resume_cmd(session_id, yolo=True))


def session_doc(row: dict) -> dict:
    """The Firestore projection of a local row. Every field the cross-machine
    consumers need (Slack board, team-operations unattended sweep, Mission
    Control web board, morning briefing) and nothing hot-path-only."""
    cwd = row.get("cwd")
    return {
        "session_id": row.get("session_id"),
        "host": row.get("host") or "unknown-host",
        "name": row.get("name"),
        "state": row.get("state"),
        "cwd": cwd,
        "folder": os.path.basename(cwd or "") or None,
        "updated_at": row.get("updated_at"),
        "heartbeat_at": row.get("heartbeat_at"),
        "model_label": row.get("model_label"),
        "provider": store.provider_of(row),
        "done_at": row.get("done_at"),
        "done_turn_s": row.get("done_turn_s"),
        "acked_at": row.get("acked_at"),
        "resume_command": resume_command(str(row.get("session_id")), store.provider_of(row)),
    }


@contextmanager
def _publication_lock() -> Iterator[None]:
    """Serialize this host's workers across the complete publication cycle."""
    import fcntl

    _PUBLICATION_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PUBLICATION_LOCK_PATH, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def push(session_id: str) -> bool:
    """Publish the newest local state. Best-effort, never raises.

    True means the Firestore document was merged. Slack alerts and the board
    remain independently best-effort, preserving the offline hook contract.
    """
    try:
        with _publication_lock():
            # Do not snapshot state until this worker owns publication order.
            row = store.get(session_id)
            if row is None:
                return False

            naming.maybe_name(session_id)
            row = store.get(session_id) or row  # re-read in case the name just upgraded

            host = row.get("host") or "unknown-host"
            doc_id = f"{host}:{session_id}"

            client = _firestore_client()
            # merge=True: the team-operations sweep owns its own fields on this
            # doc (alert_count, last_alert_at, alert_ts). A plain set() would
            # wipe them on every turn and the sweep would re-alert forever.
            client.collection(COLLECTION).document(doc_id).set(session_doc(row), merge=True)

            pending_alert = row.get("pending_alert")
            if pending_alert:
                try:
                    if pending_alert == "needs-input" or _immediate_done_alert_enabled():
                        post_alert(
                            session_id,
                            pending_alert,
                            row.get("name") or session_id,
                            folder=os.path.basename(row.get("cwd") or "") or None,
                            model_label=row.get("model_label") or None,
                            provider=store.provider_of(row),
                        )
                    else:
                        _log(
                            f"skipped immediate done alert for session_id={session_id} "
                            "(AGENT_BOARD_IMMEDIATE_DONE_ALERT off; sweep handles unattended)"
                        )
                except Exception as exc:
                    _log(f"post_alert failed for session_id={session_id}: {exc}")
                finally:
                    store.clear_pending_alert(session_id)

            try:
                post_or_update_slack(render_slack_body())
            except Exception as exc:
                _log(f"slack board update failed for session_id={session_id}: {exc}")
            return True
    except Exception as exc:
        _log(f"push failed for session_id={session_id}: {exc}")
        return False


def push_ack(session_id: str) -> bool:
    """Backward-compatible explicit-ack entry point; publish the full board."""
    return push(session_id)


def _fetch_all_session_docs():
    client = _firestore_client()
    return list(client.collection(COLLECTION).stream())


def _fmt_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h".replace(".0h", "h")
    return f"{seconds / 86400:.1f}d".replace(".0d", "d")


def render_slack_body() -> str:
    """Render the full cross-laptop board as one Slack message body.

    Includes a resume command per row (R6, plan scenario S4) -- built the
    same way cli.py's local board does, via the real provider, not
    reimplemented here. Leads with the "finished, not picked up" section:
    that is the list the user actually needs at a glance; everything else
    is context.
    """
    docs = _fetch_all_session_docs()
    rows = [d.to_dict() for d in docs]
    if not rows:
        return "*#agent-status* — all clear, no active sessions"

    now = time.time()
    lines = ["*#agent-status*"]

    waiting = [r for r in rows if store.is_unattended(r) and store.display_state(r) != "gone"]
    waiting.sort(key=lambda r: r.get("done_at") or 0)
    if waiting:
        lines.append(f"\n*⏳ finished, not picked up ({len(waiting)})*")
        for row in waiting:
            name = row.get("name") or row.get("cwd") or row.get("session_id")
            folder = f" `[{row['folder']}]`" if row.get("folder") else ""
            model = f" · {row.get('model_label')}" if row.get("model_label") else ""
            age = _fmt_age(now - float(row.get("done_at") or now))
            resume = row.get("resume_command") or resume_command(
                str(row.get("session_id")), row.get("provider")
            )
            lines.append(f"⏳ *{name}*{folder}{model} — {age} ago — `{resume}`")

    by_host: dict[str, list[dict]] = {}
    for row in rows:
        by_host.setdefault(row.get("host") or "unknown-host", []).append(row)

    for host in sorted(by_host):
        lines.append(f"\n*{host}*")
        host_rows = sorted(by_host[host], key=lambda r: store.STATE_ORDER.get(store.display_state(r), 5))
        for row in host_rows:
            state = store.display_state(row)
            name = row.get("name") or row.get("cwd") or row.get("session_id")
            model = f" · {row.get('model_label')}" if row.get("model_label") else ""
            icon = store.STATE_ICON.get(state, "?")
            resume = row.get("resume_command") or resume_command(
                str(row.get("session_id")), row.get("provider")
            )
            lines.append(f"{icon} {name}{model} — `{state}` — `{resume}`")

    return "\n".join(lines)


def _get_stored_slack_ts() -> str | None:
    client = _firestore_client()
    doc = client.collection(META_COLLECTION).document(META_DOC).get()
    if doc.exists:
        return doc.to_dict().get("ts")
    return None


def _store_slack_ts(ts: str) -> None:
    client = _firestore_client()
    client.collection(META_COLLECTION).document(META_DOC).set({"ts": ts})


def _slack_post_message(body: str) -> str:
    import requests

    token = os.environ["SLACK_BOT_TOKEN"]
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": SLACK_CHANNEL, "text": body},
        timeout=10,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"chat.postMessage failed: {data.get('error')}")
    return data["ts"]


def _slack_update_message(ts: str, body: str) -> None:
    import requests

    token = os.environ["SLACK_BOT_TOKEN"]
    resp = requests.post(
        "https://slack.com/api/chat.update",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": SLACK_CHANNEL, "ts": ts, "text": body},
        timeout=10,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"chat.update failed: {data.get('error')}")


def post_or_update_slack(body: str) -> None:
    """Post the board as a new Slack message, or update the existing one in place."""
    try:
        ts = _get_stored_slack_ts()
        if ts is None:
            new_ts = _slack_post_message(body)
            _store_slack_ts(new_ts)
        else:
            _slack_update_message(ts, body)
    except Exception as exc:
        _log(f"post_or_update_slack failed: {exc}")


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


def ack_main(argv: list[str]) -> int:
    """`agent-board ack <session-id-prefix | name-substring>`: mark a finished
    session as seen so no surface keeps re-surfacing it. Local first (so the
    statusline/aj reflect it instantly), then publish the full board."""
    if not argv:
        print("usage: agent-board ack <session-id-prefix | name-substring>", file=sys.stderr)
        return 1
    query = " ".join(argv)
    matches = store.find(query)
    if not matches:
        print(f"no session matches {query!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"{len(matches)} sessions match {query!r}; be more specific:", file=sys.stderr)
        for row in matches:
            print(f"  {row.get('session_id')}  {row.get('name')}", file=sys.stderr)
        return 1
    row = matches[0]
    session_id = str(row["session_id"])
    store.ack(session_id)
    push(session_id)
    print(f"acked {row.get('name') or session_id}")
    return 0


def main(argv: list[str]) -> None:
    if not argv:
        print("usage: agent-board sync <push|check|ack <id-or-name>>", file=sys.stderr)
        sys.exit(1)

    subcommand = argv[0]
    _load_env_fallback()

    if subcommand == "push":
        try:
            if len(argv) > 1:
                session_id = argv[1]
            else:
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

    elif subcommand == "ack":
        sys.exit(ack_main(argv[1:]))

    else:
        print(f"unknown sync subcommand: {subcommand}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])

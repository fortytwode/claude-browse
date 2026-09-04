"""Local-only web viewer: full-thread reading and session scanning in a browser.

An optional add-on alongside the fzf picker (`claude-browse --web`), not a
replacement -- the picker stays the fast "just resume it" path. This gives
a real scrollable, formatted read of a past conversation, which the fzf
preview pane (a condensed restart card, see `browse._write_preview_script`)
never has room to show.

Stdlib only, bound to 127.0.0.1 -- no accounts, no outbound network calls,
consistent with the project's zero-runtime-deps design (pyproject.toml).
v1 renders prose turns only (user/assistant text, exactly what each
provider's `transcript_turns_reader` already extracts) -- no tool-call/
edit/command detail, which no provider parser captures today.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlparse

from . import fts
from .board import commands, projects, store, work_items
from .core import display_cwd, folder_name, format_date, provider_display_name
from .providers import get_provider

_ASSET_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}

_SESSIONS_LIMIT = 200

# Hosts a browser may legitimately use to reach this server. Anything else in
# the Host header means the request came through a foreign origin -- the DNS
# rebinding pattern (attacker domain re-resolving to 127.0.0.1) -- and must be
# rejected or a malicious webpage could read private transcripts.
_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _host_allowed(host_header: str) -> bool:
    host = host_header.strip()
    if host.startswith("["):  # IPv6 literal: strip only a port after the ]
        host = host.split("]")[0] + "]"
    else:
        host = host.split(":")[0]
    return host.lower() in _ALLOWED_HOSTS


def _session_to_json(row: dict, prefixes: tuple[str, ...]) -> dict:
    last_ts = row.get("last_timestamp") or row.get("timestamp") or ""
    return {
        "session_id": row["session_id"],
        "provider": row.get("provider") or "",
        "provider_name": provider_display_name(row.get("provider")),
        "folder": folder_name(row.get("cwd"), prefixes),
        "cwd": display_cwd(row.get("cwd")),
        "title": row.get("name") or row.get("first_msg") or "(untitled)",
        "first_msg": row.get("first_msg") or "",
        "last_msg": row.get("last_msg") or "",
        "msg_count": row.get("msg_count") or 0,
        "timestamp": row.get("timestamp") or "",
        "last_timestamp": last_ts,
        # Preformatted server-side with the same formatter the TUI columns
        # use, so the two surfaces can't drift on relative-time rules.
        "when": format_date(last_ts),
        "actions": _launch_actions(row),
    }


def _provider_available(provider: str) -> bool:
    try:
        return get_provider(provider).is_available()
    except (ValueError, OSError):
        return False


def _launch_actions(session: dict) -> dict:
    actions = {}
    for target in work_items.PROVIDERS:
        actions[target] = commands.action_status(
            session, target, availability_check=_provider_available
        )
    return actions


class _Handler(BaseHTTPRequestHandler):
    server_version = "claude-browse-web/1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep the terminal quiet; failures are surfaced in the JSON body

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        if not _host_allowed(self.headers.get("Host", "")):
            self._send_json({"error": "forbidden host"}, status=403)
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._serve_asset("index.html")
            elif path == "/app.js":
                self._serve_asset("app.js")
            elif path == "/app.css":
                self._serve_asset("app.css")
            elif path == "/api/meta":
                project = projects.resolve_project(self.server.launch_cwd)  # type: ignore[attr-defined]
                self._send_json(
                    {
                        "here_only_forced": bool(self.server.cwd_filter),  # type: ignore[attr-defined]
                        "csrf_token": self.server.csrf_token,  # type: ignore[attr-defined]
                        "launch_project": project,
                    }
                )
            elif path == "/api/board":
                self._serve_board()
            elif path == "/api/sessions":
                self._serve_sessions(parse_qs(parsed.query))
            elif path.startswith("/api/session/"):
                sid = unquote(path[len("/api/session/") :])
                self._serve_session(sid)
            else:
                self._send_json({"error": "not found"}, status=404)
        except sqlite3.Error as exc:
            # Mirrors the fzf helper scripts' degrade-don't-traceback handling
            # of the mid-rebuild quarantine window (browse.py:_write_preview_script).
            self._send_json({"error": f"search index unavailable: {exc}"}, status=503)

    def do_POST(self) -> None:  # noqa: N802
        if not self._mutation_allowed():
            return
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/tasks/reorder":
                self._reorder_tasks(body)
            elif parsed.path == "/api/projects/reorder":
                self._reorder_projects(body)
            elif parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/launch"):
                task_id = unquote(parsed.path[len("/api/tasks/") : -len("/launch")])
                self._launch_task(task_id, body)
            elif parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/launch"):
                sid = unquote(parsed.path[len("/api/sessions/") : -len("/launch")])
                self._launch_session(sid, body)
            else:
                self._send_json({"error": "not found"}, status=404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except (OSError, sqlite3.Error) as exc:
            self._send_json({"error": str(exc)}, status=500)

    def do_PATCH(self) -> None:  # noqa: N802
        if not self._mutation_allowed():
            return
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path.startswith("/api/projects/"):
                project_key = unquote(parsed.path[len("/api/projects/") :])
                unknown = set(body) - {"description"}
                if unknown:
                    raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
                if "description" not in body:
                    raise ValueError("description is required")
                project = work_items.set_project_description(
                    project_key, body["description"]
                )
                self._send_json({"project": self._project_to_json(project, [])})
            elif parsed.path.startswith("/api/tasks/"):
                task_id = unquote(parsed.path[len("/api/tasks/") :])
                unknown = set(body) - {"title", "status", "due_date", "priority"}
                if unknown:
                    raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
                task, publish_session = work_items.mutate(task_id, **body)
                if not task:
                    self._send_json({"error": "task not found"}, status=404)
                    return
                if publish_session:
                    from .board.hook import _spawn_sync

                    _spawn_sync(publish_session)
                self._send_json({"task": self._task_to_json(task)})
            else:
                self._send_json({"error": "not found"}, status=404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except sqlite3.Error as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _mutation_allowed(self) -> bool:
        if not _host_allowed(self.headers.get("Host", "")):
            self._send_json({"error": "forbidden host"}, status=403)
            return False
        token = self.headers.get("X-Agent-Board-Token", "")
        expected = self.server.csrf_token  # type: ignore[attr-defined]
        if not secrets.compare_digest(token, expected):
            self._send_json({"error": "invalid request token"}, status=403)
            return False
        if self.headers.get_content_type() != "application/json":
            self._send_json({"error": "application/json required"}, status=415)
            return False
        return True

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0 or length > 64 * 1024:
            raise ValueError("request body is too large")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _serve_asset(self, name: str) -> None:
        content_type = _ASSET_CONTENT_TYPES[name]
        data = resources.files("claude_browse.webassets").joinpath(name).read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self._send_security_headers()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed the tab mid-response; not a server error

    def _serve_sessions(self, qs: dict[str, list[str]]) -> None:
        query = (qs.get("q") or [""])[0].strip()
        # server.cwd_filter is set when the CLI was launched with `--web --here`:
        # it forces folder-scoping regardless of the client's checkbox state,
        # mirroring how --here forces scoping for the fzf picker.
        forced_here = bool(self.server.cwd_filter)  # type: ignore[attr-defined]
        here_only = forced_here or (qs.get("here") or [""])[0] == "1"
        cwd = self.server.cwd_filter or self.server.launch_cwd  # type: ignore[attr-defined]
        prefixes = self.server.folder_prefixes  # type: ignore[attr-defined]
        limit = self.server.session_limit  # type: ignore[attr-defined]
        conn = fts.open_db(read_only=True)
        try:
            if query:
                if here_only:
                    # Folder-scoping is a post-filter over the ranked slice, so
                    # pull a much deeper candidate pool first -- otherwise a
                    # folder session ranked below the global cut vanishes from
                    # "this folder only" search (the same aged-out-of-slice
                    # class the empty-query path was fixed for). The residual
                    # cap only bites when >5x limit sessions outrank every
                    # folder match.
                    pool = fts.search_ranked(
                        conn, query, limit=limit * 5, current_cwd=cwd
                    )
                    base = cwd.rstrip("/")
                    rows = [
                        r
                        for r in pool
                        if (r.get("cwd") or "") == base
                        or (r.get("cwd") or "").startswith(base + "/")
                    ][:limit]
                else:
                    rows = fts.search_ranked(conn, query, limit=limit, current_cwd=cwd)
            elif here_only:
                rows = fts.sessions_for_cwd(conn, cwd, limit=limit)
            else:
                rows = fts.list_recent(conn, limit=limit, cwd=cwd)
        finally:
            conn.close()
        self._send_json({"sessions": [_session_to_json(r, prefixes) for r in rows]})

    def _serve_session(self, sid: str) -> None:
        conn = fts.open_db(read_only=True)
        try:
            row = fts.get_by_sid(conn, sid)
        finally:
            conn.close()
        if not row:
            self._send_json({"error": "session not found"}, status=404)
            return
        try:
            spec = get_provider(row.get("provider"))
            # flatten=False keeps newlines so code blocks and paragraphs
            # actually render; every other consumer gets flattened text.
            turns = spec.transcript_turns(row["path"], sid, flatten=False)
        except (ValueError, OSError, AttributeError, TypeError, KeyError) as exc:
            # Broad on purpose: provider parsers read arbitrary user files, and
            # a malformed one must degrade to a JSON error, not a dead thread.
            self._send_json(
                {"error": f"could not load transcript: {exc}"}, status=500
            )
            return
        self._send_json(
            {
                "meta": {
                    **_session_to_json(row, self.server.folder_prefixes),  # type: ignore[attr-defined]
                },
                "turns": [{"role": role, "text": text} for role, text in turns],
            }
        )

    def _task_to_json(self, task: dict, indexed_session: dict | None = None) -> dict:
        session_id = str(task.get("session_id") or "")
        provider = str(task.get("session_provider") or "claude")
        cwd = str(task.get("session_cwd") or "")
        runtime = store.get(session_id) if session_id else None
        terminal_state = store.display_state(runtime) if runtime else "gone"
        unattended = store.is_unattended(runtime)
        needs_attention = bool(runtime and runtime.get("state") == "needs-input")
        last_activity = (runtime or {}).get("updated_at") or task.get("updated_at") or 0
        work_status = str(task.get("status") or "active")
        due_date = task.get("due_date")
        in_today = bool(
            work_status == "active"
            and (
                needs_attention
                or unattended
                or (due_date and str(due_date) <= date.today().isoformat())
            )
        )
        cwd_available = bool(cwd and os.path.isdir(cwd))
        launch_session = commands.session_for_launch(session_id, indexed_session) or {
            "session_id": session_id,
            "provider": provider,
            "cwd": cwd,
        }
        actions = _launch_actions(launch_session)
        full = commands.direct_session_command(session_id, provider, full_access=True)
        safe = commands.direct_session_command(session_id, provider, full_access=False)
        public_task = {
            key: value
            for key, value in task.items()
            if key not in {"status", "notes", "title_override", "title_source"}
        }
        summary_source = ""
        if indexed_session:
            summary_source = str(
                indexed_session.get("last_msg")
                or indexed_session.get("first_msg")
                or ""
            )
        summary = " ".join(summary_source.split())[:200]
        if not summary:
            summary = "(no transcript preview)"
        sort_key = [
            1 if work_status in {"done", "archived"} else 0,
            0 if needs_attention or unattended else 1,
            str(due_date or "9999-12-31"),
            -float(last_activity),
            session_id,
        ]
        return {
            **public_task,
            "order": int(task.get("position") or 0),
            "summary": summary,
            "work_status": work_status,
            "terminal_state": terminal_state,
            "runtime_host": (runtime or {}).get("host") or "",
            "unattended": unattended,
            "in_today": in_today,
            "last_activity_at": last_activity,
            "project_available": cwd_available,
            "actions": actions,
            "sort_key": sort_key,
            "full_command": full,
            "safe_command": safe,
        }

    def _serve_board(self) -> None:
        raw_tasks = work_items.list_items(include_done=True)
        indexed: dict[str, dict] = {}
        try:
            conn = fts.open_db(read_only=True)
            try:
                for task in raw_tasks:
                    session_id = str(task.get("session_id") or "")
                    row = fts.get_by_sid(conn, session_id)
                    if row:
                        indexed[session_id] = row
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            pass
        tasks = [
            self._task_to_json(task, indexed.get(str(task.get("session_id") or "")))
            for task in raw_tasks
            if task.get("session_id")
        ]
        tasks.sort(key=lambda task: tuple(task["sort_key"]))
        projects_json = [
            self._project_to_json(project, tasks)
            for project in work_items.list_projects()
        ]
        self._send_json({"tasks": tasks, "projects": projects_json})

    def _project_to_json(self, project: dict, tasks: list[dict]) -> dict:
        project_tasks = [
            task for task in tasks if task.get("project_key") == project["project_key"]
        ]
        return {
            "project_key": project["project_key"],
            "name": project["name"],
            "path": project["path"],
            "description": project.get("description") or "",
            "order": int(project.get("position") or 0),
            "counts": {
                "active": sum(task["work_status"] == "active" for task in project_tasks),
                "today": sum(bool(task["in_today"]) for task in project_tasks),
                "needs_input": sum(
                    task["work_status"] == "active"
                    and task["terminal_state"] == "needs-input"
                    for task in project_tasks
                ),
            },
        }

    def _reorder_tasks(self, body: dict) -> None:
        unknown = set(body) - {"project_key", "task_ids", "priority"}
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        rows = work_items.reorder_tasks(
            body.get("project_key"),
            body.get("task_ids"),
            priority=body.get("priority") if "priority" in body else None,
        )
        self._send_json({"tasks": [self._task_to_json(row) for row in rows]})

    def _reorder_projects(self, body: dict) -> None:
        unknown = set(body) - {"project_keys"}
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        rows = work_items.reorder_projects(body.get("project_keys"))
        self._send_json(
            {"projects": [self._project_to_json(row, []) for row in rows]}
        )

    def _launch_task(self, task_id: str, body: dict) -> None:
        task = work_items.get(task_id)
        if not task or not task.get("session_id"):
            self._send_json({"error": "task not found"}, status=404)
            return
        provider = str(body.get("provider") or task.get("session_provider") or "claude")
        if provider not in work_items.PROVIDERS:
            raise ValueError("provider must be claude or codex")
        full_access = body.get("full_access")
        if not isinstance(full_access, bool):
            raise ValueError("full_access must be true or false")
        session_id = str(task.get("session_id") or "")
        indexed = None
        try:
            conn = fts.open_db(read_only=True)
            try:
                indexed = fts.get_by_sid(conn, session_id)
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            pass
        session = commands.session_for_launch(session_id, indexed)
        if session is None:
            raise ValueError("session not found")
        action = _launch_actions(session)[provider]
        if not action["available"]:
            raise ValueError(str(action["reason"]))
        command = commands.direct_session_command(
            session_id, provider, full_access=full_access
        )
        commands.open_in_terminal(command)
        self._send_json({"ok": True, "command": command})

    def _launch_session(self, session_id: str, body: dict) -> None:
        provider = str(body.get("provider") or "").strip().lower()
        if provider not in work_items.PROVIDERS:
            raise ValueError("provider must be claude or codex")
        conn = fts.open_db(read_only=True)
        try:
            session = fts.get_by_sid(conn, session_id)
        finally:
            conn.close()
        if not session:
            raise ValueError("session not found")
        full_access = body.get("full_access")
        if not isinstance(full_access, bool):
            raise ValueError("full_access must be true or false")
        action = _launch_actions(session)[provider]
        if not action["available"]:
            raise ValueError(str(action["reason"]))
        command = commands.direct_session_command(
            session_id, provider, full_access=full_access
        )
        commands.open_in_terminal(command)
        self._send_json({"ok": True, "command": command})

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_security_headers()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed the tab mid-response; not a server error

    def _send_security_headers(self) -> None:
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )


def run_server(
    cwd: str,
    prefixes: tuple[str, ...] = (),
    cwd_filter: str | None = None,
    limit: int = _SESSIONS_LIMIT,
) -> None:
    """Start the local viewer in the foreground until Ctrl+C.

    `cwd` is the directory claude-browse was launched from -- used to
    guarantee current-folder sessions surface first in the sidebar, the
    same guarantee the fzf picker's initial paint gets (see
    fts.list_recent's `cwd` param). `cwd_filter` mirrors the fzf picker's
    `--here` flag: when set, every session list this server returns is
    scoped to that folder, regardless of the client's checkbox state.
    `limit` caps every session list; the CLI passes its own resolved limit
    so `--web --all` widens the sidebar just like `--all` widens the picker.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.launch_cwd = cwd  # type: ignore[attr-defined]
    server.cwd_filter = cwd_filter  # type: ignore[attr-defined]
    server.folder_prefixes = prefixes  # type: ignore[attr-defined]
    server.session_limit = limit  # type: ignore[attr-defined]
    server.csrf_token = secrets.token_urlsafe(32)  # type: ignore[attr-defined]
    work_items.reconcile_sessions()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"claude-browse web viewer: {url}", file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)
    try:
        webbrowser.open(url)
    except Exception:
        pass  # headless/SSH sessions still have the URL printed above
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()

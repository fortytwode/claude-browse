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
    }


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
            if parsed.path == "/api/tasks":
                self._create_task(body)
            elif parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/launch"):
                task_id = unquote(parsed.path[len("/api/tasks/") : -len("/launch")])
                self._launch_task(task_id, body)
            elif parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/ack"):
                sid = unquote(parsed.path[len("/api/sessions/") : -len("/ack")])
                if not store.get(sid):
                    self._send_json({"error": "session not found"}, status=404)
                    return
                store.ack(sid)
                store.mark_sync_pending(sid)
                from .board.hook import _spawn_sync

                _spawn_sync(sid)
                self._send_json({"ok": True})
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
        if not parsed.path.startswith("/api/tasks/"):
            self._send_json({"error": "not found"}, status=404)
            return
        task_id = unquote(parsed.path[len("/api/tasks/") :])
        try:
            body = self._read_json()
            task = work_items.update(task_id, **body)
            if not task:
                self._send_json({"error": "task not found"}, status=404)
                return
            self._send_json({"task": self._task_to_json(task)})
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
                    "queued_task_id": (
                        (work_items.get_for_session(sid) or {}).get("task_id")
                    ),
                },
                "turns": [{"role": role, "text": text} for role, text in turns],
            }
        )

    def _attention_to_json(self, row: dict) -> dict:
        provider = store.provider_of(row)
        cwd = str(row.get("cwd") or "")
        sid = str(row.get("session_id") or "")
        return {
            "session_id": sid,
            "title": row.get("name") or os.path.basename(cwd) or sid,
            "project": projects.resolve_project(cwd or None),
            "provider": provider,
            "host": row.get("host") or "",
            "state": store.display_state(row),
            "unattended": store.is_unattended(row),
            "updated_at": row.get("updated_at"),
            "full_command": commands.resume_command(sid, provider, cwd, full_access=True),
            "safe_command": commands.resume_command(sid, provider, cwd, full_access=False),
            "queued_task_id": (work_items.get_for_session(sid) or {}).get("task_id"),
        }

    def _task_to_json(self, task: dict) -> dict:
        session_id = str(task.get("session_id") or "")
        provider = str(task.get("session_provider") or "claude")
        cwd = str(task.get("project_path") or "")
        runtime = store.get(session_id) if session_id else None
        command_builder = commands.resume_command if session_id else commands.start_command
        if session_id:
            full = command_builder(session_id, provider, cwd, full_access=True)
            safe = command_builder(session_id, provider, cwd, full_access=False)
        else:
            prompt = str(task.get("notes") or task.get("title") or "")
            full = command_builder(task["task_id"], prompt, provider, cwd, full_access=True)
            safe = command_builder(task["task_id"], prompt, provider, cwd, full_access=False)
        return {
            **task,
            "runtime_state": store.display_state(runtime) if runtime else "not-started",
            "runtime_host": (runtime or {}).get("host") or "",
            "project_available": bool(cwd and os.path.isdir(cwd)),
            "full_command": full,
            "safe_command": safe,
        }

    def _serve_board(self) -> None:
        attention_rows = []
        seen: set[str] = set()
        for row in store.active(max_age_hours=24 * 365):
            sid = str(row.get("session_id") or "")
            if sid in seen:
                continue
            if row.get("state") == "needs-input" or store.is_unattended(row):
                seen.add(sid)
                attention_rows.append(self._attention_to_json(row))
        tasks = [self._task_to_json(task) for task in work_items.list_items()]
        self._send_json({"attention": attention_rows, "tasks": tasks})

    def _create_task(self, body: dict) -> None:
        session_id = str(body.get("session_id") or "").strip()
        if session_id:
            conn = fts.open_db(read_only=True)
            try:
                session = fts.get_by_sid(conn, session_id)
            finally:
                conn.close()
            if not session:
                raise ValueError("session not found")
            body = {
                **body,
                "project_path": session.get("cwd") or body.get("project_path") or "",
                "provider": session.get("provider") or body.get("provider") or "claude",
                "title": body.get("title") or session.get("name") or session.get("first_msg"),
            }
        task = work_items.create(
            title=body.get("title"),
            project_path=body.get("project_path") or self.server.launch_cwd,  # type: ignore[attr-defined]
            status=body.get("status", "todo"),
            due_date=body.get("due_date"),
            session_id=session_id or None,
            provider=body.get("provider", "claude"),
            notes=body.get("notes", ""),
        )
        self._send_json({"task": self._task_to_json(task)}, status=201)

    def _launch_task(self, task_id: str, body: dict) -> None:
        if "title" in body:
            updated = work_items.update(task_id, title=body["title"])
            if not updated:
                self._send_json({"error": "task not found"}, status=404)
                return
        task = work_items.get(task_id)
        if not task:
            self._send_json({"error": "task not found"}, status=404)
            return
        cwd = str(task.get("project_path") or "")
        if not cwd or not os.path.isdir(cwd):
            raise ValueError("project folder is not available on this Mac")
        provider = str(body.get("provider") or task.get("session_provider") or "claude")
        if provider not in work_items.PROVIDERS:
            raise ValueError("provider must be claude or codex")
        full_access = body.get("full_access", True)
        if not isinstance(full_access, bool):
            raise ValueError("full_access must be true or false")
        session_id = str(task.get("session_id") or "")
        if session_id:
            conn = fts.open_db(read_only=True)
            try:
                session = fts.get_by_sid(conn, session_id)
            finally:
                conn.close()
            if session:
                command = commands.continue_command(
                    session,
                    provider,
                    full_access=full_access,
                    task_id=task_id if provider != task.get("session_provider") else None,
                )
            elif provider == task.get("session_provider"):
                command = commands.resume_command(
                    session_id, provider, cwd, full_access=full_access
                )
            else:
                raise ValueError("thread transcript is unavailable for provider handoff")
        else:
            prompt = str(task.get("notes") or task.get("title") or "")
            command = commands.start_command(
                task_id, prompt, provider, cwd, full_access=full_access
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
        cwd = str(session.get("cwd") or "")
        if not cwd or not os.path.isdir(cwd):
            raise ValueError("project folder is not available on this Mac")
        full_access = body.get("full_access", True)
        if not isinstance(full_access, bool):
            raise ValueError("full_access must be true or false")
        queued = work_items.get_for_session(session_id)
        command = commands.continue_command(
            session,
            provider,
            full_access=full_access,
            task_id=(queued or {}).get("task_id")
            if provider != session.get("provider")
            else None,
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

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
import math
import os
import secrets
import sqlite3
import sys
import threading
import webbrowser
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlparse

from . import fts
from .board import commands, discovery, launches, presence, projects, store, work_items, workspace
from .core import display_cwd, folder_name, format_date, provider_display_name
from .providers import get_provider

_ASSET_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}

_SESSIONS_LIMIT = 200
_RECONCILE_INTERVAL_S = 30

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


def _provider_availability() -> dict[str, bool]:
    """Snapshot local provider binaries once for one response."""
    return {provider: _provider_available(provider) for provider in work_items.PROVIDERS}


def _availability_check(availability: dict[str, bool] | None):
    if availability is None:
        return _provider_available
    return lambda provider: availability.get(provider, False)


def _launch_actions(session: dict, *, availability: dict[str, bool] | None = None) -> dict:
    availability_check = _availability_check(availability)
    actions = {}
    for target in work_items.PROVIDERS:
        actions[target] = commands.action_status(
            session, target, availability_check=availability_check
        )
    return actions


def _timestamp_seconds(value: object) -> float:
    """Normalize runtime seconds and indexed ISO dates at the API boundary."""
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            result = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        except (TypeError, ValueError, OverflowError, OSError):
            return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def _thread_counts(tasks: list[dict]) -> dict[str, int]:
    return {
        "total": len(tasks),
        "open_terminal": sum(task.get("terminal_presence") == "open" for task in tasks),
        "closed_terminal": sum(task.get("terminal_presence") == "closed" for task in tasks),
        "unknown_terminal": sum(task.get("terminal_presence") == "unknown" for task in tasks),
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
            elif path.startswith("/api/tasks/") and path.endswith("/history"):
                task_id = unquote(path[len("/api/tasks/") : -len("/history")])
                self._serve_task_history(task_id)
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
            elif parsed.path == "/api/workspace/spaces":
                self._workspace_create_space(body)
            elif parsed.path == "/api/workspace/folders":
                self._workspace_create_folder(body)
            elif parsed.path == "/api/workspace/lists":
                self._workspace_create_list(body)
            elif parsed.path == "/api/workspace/reorder":
                self._workspace_reorder_node(body)
            elif parsed.path.startswith("/api/workspace/lists/") and parsed.path.endswith("/launch"):
                list_key = unquote(parsed.path[len("/api/workspace/lists/") : -len("/launch")])
                self._launch_workspace("list", list_key, body)
            elif (
                parsed.path.startswith("/api/workspace/lists/")
                and parsed.path.endswith("/directory")
            ):
                list_key = unquote(
                    parsed.path[len("/api/workspace/lists/") : -len("/directory")]
                )
                self._workspace_create_directory(list_key, body)
            elif (
                parsed.path.startswith("/api/workspace/tasks/")
                and parsed.path.endswith("/move")
            ):
                task_id = unquote(
                    parsed.path[len("/api/workspace/tasks/") : -len("/move")]
                )
                self._workspace_move_task(task_id, body)
            elif parsed.path == "/api/workspace/tasks/reorder":
                self._workspace_reorder_tasks(body)
            elif parsed.path == "/api/projects/reorder":
                self._reorder_projects(body)
            elif parsed.path == "/api/folders":
                if set(body) != {"name"}:
                    raise ValueError("name is required; no other fields are accepted")
                self._send_json({"folder": work_items.create_folder(body["name"])})
            elif parsed.path == "/api/folders/reorder":
                if set(body) != {"folder_ids"}:
                    raise ValueError("folder_ids is required; no other fields are accepted")
                self._send_json({"folders": work_items.reorder_folders(body["folder_ids"])})
            elif parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/launch"):
                task_id = unquote(parsed.path[len("/api/tasks/") : -len("/launch")])
                self._launch_task(task_id, body)
            elif parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/start"):
                task_id = unquote(parsed.path[len("/api/tasks/") : -len("/start")])
                self._launch_task(task_id, body, fresh=True)
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
            if parsed.path.startswith("/api/workspace/spaces/"):
                space_id = unquote(parsed.path[len("/api/workspace/spaces/") :])
                self._workspace_update_space(space_id, body)
            elif parsed.path.startswith("/api/workspace/folders/"):
                folder_id = unquote(parsed.path[len("/api/workspace/folders/") :])
                self._workspace_update_folder(folder_id, body)
            elif parsed.path.startswith("/api/workspace/lists/"):
                list_key = unquote(parsed.path[len("/api/workspace/lists/") :])
                self._workspace_update_list(list_key, body)
            elif parsed.path.startswith("/api/projects/"):
                project_key = unquote(parsed.path[len("/api/projects/") :])
                project = work_items.update_project(project_key, **body)
                self._send_json({"project": self._project_to_json(project, [])})
            elif parsed.path.startswith("/api/folders/"):
                folder_id = unquote(parsed.path[len("/api/folders/") :])
                if set(body) != {"name"}:
                    raise ValueError("name is required; no other fields are accepted")
                self._send_json({"folder": work_items.update_folder(folder_id, body["name"])})
            elif parsed.path.startswith("/api/tasks/"):
                task_id = unquote(parsed.path[len("/api/tasks/") :])
                edit_client = body.pop("_edit_client", None)
                edit_revision = body.pop("_edit_revision", None)
                unknown = set(body) - {"title", "status", "due_date", "priority"}
                if unknown:
                    raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
                if (edit_client is None) != (edit_revision is None):
                    raise ValueError("edit client and revision must be provided together")
                if edit_client is not None:
                    if (
                        not isinstance(edit_client, str)
                        or not edit_client
                        or len(edit_client) > 100
                        or isinstance(edit_revision, bool)
                        or not isinstance(edit_revision, int)
                        or edit_revision < 0
                        or len(body) != 1
                    ):
                        raise ValueError("invalid edit revision")
                    field = next(iter(body))
                    revision_key = (edit_client, task_id, field)
                    with self.server.edit_revision_lock:  # type: ignore[attr-defined]
                        last_revision = self.server.edit_revisions.get(  # type: ignore[attr-defined]
                            revision_key, -1
                        )
                        if edit_revision < last_revision:
                            task, publish_session = work_items.get(task_id), None
                        else:
                            task, publish_session = work_items.mutate(task_id, **body)
                            if task:
                                self.server.edit_revisions[revision_key] = edit_revision  # type: ignore[attr-defined]
                else:
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
            self.send_header("Cache-Control", "no-store, max-age=0")
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
        # Runtime capture and search indexing have independent clocks. Reading
        # and launching must resolve the same local file, including hook-only
        # threads and valid fallbacks for stale cached paths.
        row = commands.session_for_launch(sid)
        if not row:
            self._send_json({"error": "session not found"}, status=404)
            return
        overlay = work_items.get_for_session(sid)
        if overlay:
            row["name"] = overlay["title"]
        meta = _session_to_json(row, self.server.folder_prefixes)  # type: ignore[attr-defined]
        if overlay:
            launch = self._task_to_json(overlay)
            meta["task_launch"] = {
                key: launch[key]
                for key in (
                    "task_id", "session_id", "launch_revision", "working_directory",
                    "actions", "start_actions",
                )
            }
        path = str(row.get("path") or "")
        if not path or not os.path.isfile(path):
            self._send_json({
                "meta": meta,
                "turns": [],
                "transcript_error": "The transcript is not available on this Mac. "
                "This task is still saved. Native resume may locate provider-managed "
                "history, but reading or handing off requires the original transcript.",
            })
            return
        try:
            spec = get_provider(row.get("provider"))
            # flatten=False keeps newlines so code blocks and paragraphs
            # actually render; every other consumer gets flattened text.
            turns = spec.transcript_turns(path, sid, flatten=False)
        except (ValueError, OSError, AttributeError, TypeError, KeyError) as exc:
            # Broad on purpose: provider parsers read arbitrary user files, and
            # a malformed one must degrade to a JSON error, not a dead thread.
            self._send_json({"meta": meta, "turns": [], "transcript_error": f"Could not read this transcript: {exc}"})
            return
        meta["msg_count"] = len(turns)
        self._send_json(
            {
                "meta": meta,
                "turns": [{"role": role, "text": text} for role, text in turns],
            }
        )

    def _task_to_json(
        self, task: dict, indexed_session: dict | None = None, *,
        workspace_seeded: bool = False, presence_state: str | None = None,
        runtime: dict | None | object = commands._UNSET,
        availability: dict[str, bool] | None = None,
    ) -> dict:
        session_id = str(task.get("session_id") or "")
        provider = str(task.get("session_provider") or "claude")
        cwd = str(task.get("session_cwd") or "")
        if runtime is commands._UNSET:
            runtime = store.get(session_id) if session_id else None
        if presence_state is None:
            presence_state = presence.snapshot([runtime] if runtime else []).get(session_id, "unknown")
        terminal_state = store.display_state(runtime) if runtime else "gone"
        unattended = store.is_unattended(runtime)
        needs_attention = bool(runtime and runtime.get("state") == "needs-input")
        last_activity = max(
            _timestamp_seconds((runtime or {}).get("updated_at")),
            _timestamp_seconds(task.get("updated_at")),
            _timestamp_seconds((indexed_session or {}).get("last_timestamp")),
            _timestamp_seconds((indexed_session or {}).get("timestamp")),
        )
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
        launch_session = commands.session_for_launch(
            session_id, indexed_session, runtime
        ) or {
            "session_id": session_id,
            "provider": provider,
            "cwd": cwd,
        }
        context = workspace.context_for_task(task, seeded=workspace_seeded)
        availability_check = _availability_check(availability)
        actions = {
            target: launches.action_status(
                launch_session, context, target, availability_check=availability_check
            ) for target in work_items.PROVIDERS
        }
        start_actions = {
            target: launches.action_status(
                None, context, target, availability_check=availability_check
            ) for target in work_items.PROVIDERS
        }
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
            "terminal_runtime_state": (runtime or {}).get("state") or "unknown",
            "terminal_presence": presence_state,
            "terminal_open": presence_state == "open",
            "runtime_host": (runtime or {}).get("host") or "",
            "unattended": unattended,
            "in_today": in_today,
            "last_activity_at": last_activity,
            "project_available": cwd_available,
            "actions": actions,
            "start_actions": start_actions,
            "sort_key": sort_key,
            **context,
        }

    def _serve_board(self) -> None:
        availability = _provider_availability()
        availability_check = _availability_check(availability)
        workspace_snapshot = workspace.snapshot()
        for listing in workspace_snapshot["lists"]:
            listing.update(workspace.context_for_list(listing["list_key"], seeded=True))
            listing["actions"] = {
                target: launches.action_status(
                    None, listing, target, availability_check=availability_check
                ) for target in work_items.PROVIDERS
            }
        raw_tasks = work_items.list_items(include_done=True)
        runtime_rows = [
            store.get(session_id) if (session_id := str(task.get("session_id") or "")) else None
            for task in raw_tasks
        ]
        presence_states = presence.snapshot([row for row in runtime_rows if row])
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
            self._task_to_json(
                task, indexed.get(str(task.get("session_id") or "")), workspace_seeded=True,
                presence_state=presence_states.get(str(task.get("session_id") or ""), "unknown"),
                runtime=runtime_rows[index], availability=availability,
            )
            for index, task in enumerate(raw_tasks)
            if task.get("session_id")
        ]
        tasks.sort(key=lambda task: tuple(task["sort_key"]))
        project_rows = work_items.list_projects()
        names = {project["project_key"]: project["name"] for project in project_rows}
        for task in tasks:
            task["project_name"] = names.get(task["project_key"], task["project_name"])
        projects_json = [
            self._project_to_json(project, tasks)
            for project in project_rows
        ]
        # Counts follow user-owned placement, not the original repository.
        # Completion and terminal presence are independent; count every task.
        for listing in workspace_snapshot["lists"]:
            listing["counts"] = _thread_counts([
                task for task in tasks if task["list_key"] == listing["list_key"]
            ])
        for folder in workspace_snapshot["folders"]:
            folder["counts"] = _thread_counts([
                task for task in tasks if task["folder_id"] == folder["folder_id"]
            ])
        for space in workspace_snapshot["spaces"]:
            space["counts"] = _thread_counts([
                task for task in tasks if task["space_id"] == space["space_id"]
            ])
        self._send_json({
            "tasks": tasks,
            "projects": projects_json,
            "folders": work_items.list_folders(),
            "workspace": workspace_snapshot,
            "counts": _thread_counts(tasks),
        })

    def _serve_task_history(self, task_id: str) -> None:
        if not work_items.get(task_id):
            self._send_json({"error": "task not found"}, status=404)
            return
        self._send_json({"sessions": work_items.get_session_history(task_id)})

    @staticmethod
    def _workspace_fields(body: dict, allowed: set[str], required: set[str]) -> None:
        unknown = set(body) - allowed
        missing = required - set(body)
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if missing:
            raise ValueError(f"missing required field(s): {', '.join(sorted(missing))}")
        if not body:
            raise ValueError("at least one field is required")

    def _workspace_create_space(self, body: dict) -> None:
        self._workspace_fields(body, {"name"}, {"name"})
        self._send_json({"space": workspace.create_space(body["name"])})

    def _workspace_update_space(self, space_id: str, body: dict) -> None:
        self._workspace_fields(body, {"name", "position"}, set())
        self._send_json({"space": workspace.update_space(space_id, **body)})

    def _workspace_create_folder(self, body: dict) -> None:
        self._workspace_fields(body, {"name", "space_id"}, {"name", "space_id"})
        self._send_json({"folder": workspace.create_folder(body["name"], body["space_id"])})

    def _workspace_update_folder(self, folder_id: str, body: dict) -> None:
        self._workspace_fields(body, {"name", "space_id", "position"}, set())
        self._send_json({"folder": workspace.update_folder(folder_id, **body)})

    def _workspace_create_list(self, body: dict) -> None:
        self._workspace_fields(
            body,
            {"name", "space_id", "folder_id", "working_directory"},
            {"name", "space_id"},
        )
        self._send_json({"list": workspace.create_list(**body)})

    def _workspace_update_list(self, list_key: str, body: dict) -> None:
        self._workspace_fields(
            body,
            {"name", "description", "space_id", "folder_id", "working_directory", "position"},
            set(),
        )
        self._send_json({"list": workspace.update_list(list_key, **body)})

    def _workspace_create_directory(self, list_key: str, body: dict) -> None:
        self._workspace_fields(body, {"path"}, {"path"})
        try:
            result = workspace.create_working_directory(list_key, body["path"])
        except OSError as exc:
            raise ValueError(f"could not create working directory: {exc}") from exc
        self._send_json({"list": result})

    def _workspace_move_task(self, task_id: str, body: dict) -> None:
        self._workspace_fields(body, {"list_key", "expected_list_key"}, {"list_key", "expected_list_key"})
        self._send_json({"context": workspace.move_task(task_id, **body)})

    def _workspace_reorder_tasks(self, body: dict) -> None:
        self._workspace_fields(body, {"list_key", "task_ids", "priority"}, {"list_key", "task_ids"})
        rows = workspace.reorder_tasks(
            body["list_key"], body["task_ids"], priority=body.get("priority")
        )
        self._send_json({"tasks": [
            self._task_to_json(row) for row in rows
        ]})

    def _workspace_reorder_node(self, body: dict) -> None:
        if "target_id" in body or "placement" in body:
            fields = {"kind", "node_id", "target_id", "placement"}
            self._workspace_fields(body, fields, fields)
            node = workspace.place_node(**body)
        else:
            fields = {"kind", "node_id", "direction"}
            self._workspace_fields(body, fields, fields)
            node = workspace.move_node(**body)
        self._send_json({"node": node})

    def _project_to_json(self, project: dict, tasks: list[dict]) -> dict:
        project_tasks = [
            task for task in tasks if task.get("project_key") == project["project_key"]
        ]
        return {
            "project_key": project["project_key"],
            "name": project["name"],
            "path": project["path"],
            "description": project.get("description") or "",
            "display_name": project.get("display_name") or "",
            "folder_id": project.get("folder_id"),
            "inherited_descriptions": project.get("inherited_descriptions") or [],
            "order": int(project.get("position") or 0),
            "counts": {
                **_thread_counts(project_tasks),
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

    def _launch_task(self, task_id: str, body: dict, *, fresh: bool = False) -> None:
        task = work_items.get(task_id)
        if not task or not task.get("session_id"):
            self._send_json({"error": "task not found"}, status=404)
            return
        self._launch_workspace("task-new" if fresh else "task", task_id, body)

    def _launch_workspace(self, kind: str, target_id: str, body: dict) -> None:
        fields = {"provider", "full_access", "launch_revision"}
        self._workspace_fields(body, fields, fields)
        token = launches.prepare(
            kind, target_id, body["provider"], full_access=body["full_access"],
            launch_revision=body["launch_revision"],
        )
        try:
            commands.open_in_terminal(launches.command(token))
        except OSError as exc:
            launches.fail(token, str(exc), expected_state="prepared")
            raise
        self._send_json({"ok": True})

    def _launch_session(self, session_id: str, body: dict) -> None:
        task = work_items.get_for_session(session_id)
        if task:
            self._launch_workspace("task", task["task_id"], body)
            return
        provider = str(body.get("provider") or "").strip().lower()
        if provider not in work_items.PROVIDERS:
            raise ValueError("provider must be claude or codex")
        session = commands.session_for_launch(session_id)
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


def _capture_and_reconcile() -> None:
    """Enroll live roots before reconciling their project placement."""
    try:
        discovery.capture_live_sessions()
    except (OSError, sqlite3.Error):
        pass
    try:
        work_items.reconcile_sessions()
    except (OSError, sqlite3.Error):
        pass


def _reconcile_periodically(stop: threading.Event, interval_s: float) -> None:
    while not stop.wait(interval_s):
        _capture_and_reconcile()


def run_server(
    cwd: str,
    prefixes: tuple[str, ...] = (),
    cwd_filter: str | None = None,
    limit: int = _SESSIONS_LIMIT,
    *,
    port: int = 0,
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
    `port` optionally preserves a local launcher's origin across restarts;
    zero keeps the default of choosing an available loopback port.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.launch_cwd = cwd  # type: ignore[attr-defined]
    server.cwd_filter = cwd_filter  # type: ignore[attr-defined]
    server.folder_prefixes = prefixes  # type: ignore[attr-defined]
    server.session_limit = limit  # type: ignore[attr-defined]
    server.csrf_token = secrets.token_urlsafe(32)  # type: ignore[attr-defined]
    server.edit_revision_lock = threading.Lock()  # type: ignore[attr-defined]
    server.edit_revisions = {}  # type: ignore[attr-defined]
    _capture_and_reconcile()
    reconcile_stop = threading.Event()
    reconcile_thread = threading.Thread(
        target=_reconcile_periodically,
        args=(reconcile_stop, _RECONCILE_INTERVAL_S),
        name="agent-board-project-reconciler",
        daemon=True,
    )
    reconcile_thread.start()
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
        reconcile_stop.set()
        reconcile_thread.join(timeout=2)
        server.shutdown()
        server.server_close()

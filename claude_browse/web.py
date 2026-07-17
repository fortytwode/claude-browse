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
import sqlite3
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlparse

from . import fts
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
                self._send_json({"here_only_forced": bool(self.server.cwd_filter)})  # type: ignore[attr-defined]
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

    def _serve_asset(self, name: str) -> None:
        content_type = _ASSET_CONTENT_TYPES[name]
        data = resources.files("claude_browse.webassets").joinpath(name).read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
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
                "meta": _session_to_json(row, self.server.folder_prefixes),  # type: ignore[attr-defined]
                "turns": [{"role": role, "text": text} for role, text in turns],
            }
        )

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed the tab mid-response; not a server error


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

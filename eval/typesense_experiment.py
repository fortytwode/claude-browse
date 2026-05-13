"""Local Typesense experiment for descriptive thread recall.

This is intentionally an experiment harness, not a product backend. It keeps
the current SQLite index as the source of truth, projects thread/exchange
windows into a temporary Typesense collection, and compares:

- current SQLite ranker
- raw Typesense lexical search over the full natural-language query
- Typesense search driven by the existing QueryPlan anchors / intent

Usage:
    python -m eval.typesense_experiment
    python -m eval.typesense_experiment --query "last closeout session for Musopia"
    python -m eval.typesense_experiment --output eval/reports/typesense.md
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_REPO_ROOT = Path(__file__).resolve().parent.parent

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from claude_browse import fts  # noqa: E402
from claude_browse.query import build_query_plan  # noqa: E402

DEFAULT_COLLECTION = "claude_browse_windows"
DEFAULT_TYPESENSE_VERSION = "30.2"
DEFAULT_QUERIES = [
    "last closeout session for Musopia",
    "final discussion i had about the last closeout session for Musopia",
    "where i was asking nevena about feedback",
    "Neil performance with Nevena",
    "that we discussed, please?",
]

_CLOSEOUT_CUES = (
    "closeout",
    "close out",
    "handoff",
    "hand off",
    "wrapup",
    "wrap up",
    "debrief",
    "recap",
    "summary",
    "final session",
    "final discussion",
    "finalize",
    "finalise",
)


@dataclass
class TypesenseServer:
    process: subprocess.Popen[str]
    host: str
    port: int
    api_key: str
    data_dir: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def _find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _typesense_platform_triplet() -> tuple[str, str]:
    if sys.platform == "darwin":
        os_name = "darwin"
    elif sys.platform.startswith("linux"):
        os_name = "linux"
    else:
        raise SystemExit(f"Unsupported platform for Typesense experiment: {sys.platform}")

    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine in {"x86_64", "amd64"}:
        arch = "amd64"
    else:
        raise SystemExit(f"Unsupported architecture for Typesense experiment: {machine}")
    return os_name, arch


def _typesense_download_url(version: str = DEFAULT_TYPESENSE_VERSION) -> str:
    os_name, arch = _typesense_platform_triplet()
    return (
        f"https://dl.typesense.org/releases/{version}/"
        f"typesense-server-{version}-{os_name}-{arch}.tar.gz"
    )


def _ensure_typesense_binary(version: str = DEFAULT_TYPESENSE_VERSION) -> str:
    existing = shutil.which("typesense-server")
    if existing:
        return existing

    os_name, arch = _typesense_platform_triplet()
    cache_dir = (
        Path.home()
        / ".claude"
        / "cache"
        / "claude-browse-experiments"
        / "typesense"
        / version
        / f"{os_name}-{arch}"
    )
    binary = cache_dir / "typesense-server"
    if binary.exists():
        return str(binary)

    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / "typesense-server.tar.gz"
    with urlopen(_typesense_download_url(version), timeout=120) as resp:
        archive_path.write_bytes(resp.read())
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(cache_dir)
    binary.chmod(0o755)
    return str(binary)


def _epoch_seconds(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _lifecycle_score(*texts: str) -> int:
    haystack = " ".join(text.lower() for text in texts if text)
    return sum(1 for cue in _CLOSEOUT_CUES if cue in haystack)


def _window_text(segments: list[dict[str, Any]], idx: int, radius: int = 2) -> str:
    start = max(0, idx - radius)
    end = min(len(segments), idx + radius + 1)
    parts = []
    for segment in segments[start:end]:
        role = str(segment["role"]).capitalize()
        text = " ".join(str(segment["text"]).split())
        if text:
            parts.append(f"{role}: {text}")
    return "\n".join(parts)


def _build_window_docs(conn) -> list[dict[str, Any]]:
    session_rows = conn.execute(
        """
        SELECT sid, provider, cwd, title, first_msg, last_msg, timestamp, last_timestamp
        FROM sessions
        ORDER BY COALESCE(last_timestamp, timestamp, '') DESC
        """
    ).fetchall()
    sessions = {
        row[0]: {
            "session_id": row[0],
            "provider": row[1] or "claude",
            "cwd": row[2] or "",
            "title": row[3] or "",
            "first_msg": row[4] or "",
            "last_msg": row[5] or "",
            "timestamp": row[6] or "",
            "last_timestamp": row[7] or "",
        }
        for row in session_rows
    }
    segment_rows = conn.execute(
        """
        SELECT sid, segment_idx, role, timestamp, text
        FROM segments
        ORDER BY sid, segment_idx
        """
    ).fetchall()

    by_session: dict[str, list[dict[str, Any]]] = {}
    for sid, segment_idx, role, timestamp, text in segment_rows:
        by_session.setdefault(sid, []).append(
            {
                "segment_idx": segment_idx,
                "role": role,
                "timestamp": timestamp or "",
                "text": text or "",
            }
        )

    docs: list[dict[str, Any]] = []
    for sid, segments in by_session.items():
        session = sessions.get(sid)
        if not session:
            continue
        for pos, segment in enumerate(segments):
            text_window = _window_text(segments, pos, radius=2)
            if not text_window.strip():
                continue
            docs.append(
                {
                    "id": f"{sid}:{segment['segment_idx']}",
                    "session_id": sid,
                    "provider": session["provider"],
                    "cwd": session["cwd"],
                    "title": session["title"],
                    "first_msg": session["first_msg"],
                    "last_msg": session["last_msg"],
                    "exchange_text": text_window,
                    "thread_timestamp": _epoch_seconds(session["timestamp"]),
                    "thread_last_timestamp": _epoch_seconds(session["last_timestamp"]),
                    "exchange_timestamp": _epoch_seconds(segment["timestamp"]),
                    "lifecycle_score": _lifecycle_score(
                        session["title"],
                        session["first_msg"],
                        session["last_msg"],
                        text_window,
                    ),
                }
            )
    return docs


def _typesense_collection_schema(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "fields": [
            {"name": "session_id", "type": "string", "facet": True},
            {"name": "provider", "type": "string", "facet": True},
            {"name": "cwd", "type": "string"},
            {"name": "title", "type": "string"},
            {"name": "first_msg", "type": "string"},
            {"name": "last_msg", "type": "string"},
            {"name": "exchange_text", "type": "string"},
            {"name": "thread_timestamp", "type": "int64"},
            {"name": "thread_last_timestamp", "type": "int64"},
            {"name": "exchange_timestamp", "type": "int64"},
            {"name": "lifecycle_score", "type": "int32"},
        ],
        "default_sorting_field": "thread_last_timestamp",
    }


def _project_typesense_search(query: str, mode: str) -> dict[str, Any]:
    plan = build_query_plan(query)
    if mode == "planned":
        q = " ".join(plan.anchor_terms).strip() or "*"
    else:
        q = query.strip() or "*"

    params: dict[str, Any] = {
        "q": q,
        "query_by": "title,cwd,first_msg,last_msg,exchange_text",
        "query_by_weights": "8,6,5,3,2",
        "text_match_type": "max_weight",
        "per_page": 50,
        "include_fields": (
            "id,session_id,provider,cwd,title,first_msg,last_msg,exchange_text,"
            "thread_last_timestamp,exchange_timestamp,lifecycle_score"
        ),
        "sort_by": "_text_match:desc,thread_last_timestamp:desc",
        "highlight_fields": "title,first_msg,last_msg,exchange_text",
    }

    if mode == "planned" and plan.wants_closeout:
        params["sort_by"] = "_text_match:desc,lifecycle_score:desc,thread_last_timestamp:desc"
    if mode == "planned" and plan.low_confidence:
        params["q"] = "*"
        params["sort_by"] = "thread_last_timestamp:desc"
    return params


def _typesense_request(
    server: TypesenseServer,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
) -> Any:
    url = f"{server.base_url}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = {"X-TYPESENSE-API-KEY": server.api_key}
    if content_type:
        headers["Content-Type"] = content_type
    req = Request(url, data=body, method=method, headers=headers)
    with urlopen(req, timeout=30) as resp:
        raw = resp.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _create_collection(server: TypesenseServer, name: str) -> None:
    try:
        _typesense_request(server, "DELETE", f"/collections/{name}")
    except HTTPError as exc:
        if exc.code != 404:
            raise
    _typesense_request(
        server,
        "POST",
        "/collections",
        body=json.dumps(_typesense_collection_schema(name)).encode("utf-8"),
        content_type="application/json",
    )


def _import_docs(server: TypesenseServer, collection: str, docs: list[dict[str, Any]]) -> None:
    payload = "\n".join(json.dumps(doc, ensure_ascii=True) for doc in docs).encode("utf-8")
    with urlopen(
        Request(
            f"{server.base_url}/collections/{collection}/documents/import?action=upsert",
            data=payload,
            method="POST",
            headers={
                "X-TYPESENSE-API-KEY": server.api_key,
                "Content-Type": "text/plain",
            },
        ),
        timeout=120,
    ) as resp:
        lines = resp.read().decode("utf-8").splitlines()
    failures = [line for line in lines if line and not json.loads(line).get("success")]
    if failures:
        raise RuntimeError(f"Typesense import failures: {failures[:3]}")


def _search_typesense(
    server: TypesenseServer,
    collection: str,
    query: str,
    *,
    mode: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    params = _project_typesense_search(query, mode)
    resp = _typesense_request(
        server,
        "GET",
        f"/collections/{collection}/documents/search",
        params=params,
    )
    hits = resp.get("hits", []) if isinstance(resp, dict) else []
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        doc = hit.get("document", {})
        sid = str(doc.get("session_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        deduped.append(
            {
                "session_id": sid,
                "provider": doc.get("provider") or "",
                "cwd": doc.get("cwd") or "",
                "title": doc.get("title") or "",
                "context": doc.get("exchange_text") or "",
                "thread_last_timestamp": doc.get("thread_last_timestamp") or 0,
                "text_match": hit.get("text_match") or 0,
                "mode": mode,
            }
        )
        if len(deduped) >= limit:
            break
    return deduped


def _search_sqlite(conn, query: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = fts.search_ranked(conn, query, limit=limit)
    result = []
    for row in rows[:limit]:
        result.append(
            {
                "session_id": row.get("session_id"),
                "provider": row.get("provider"),
                "cwd": row.get("cwd") or "",
                "title": row.get("name") or "",
                "context": row.get("context") or "",
                "thread_last_timestamp": _epoch_seconds(
                    row.get("last_timestamp") or row.get("timestamp")
                ),
                "text_match": 0,
                "mode": "sqlite",
            }
        )
    return result


def _format_hit(hit: dict[str, Any]) -> str:
    title = str(hit.get("title") or "")[:60]
    cwd = str(hit.get("cwd") or "")
    context = " ".join(str(hit.get("context") or "").split())[:220]
    return (
        f"- `{hit.get('provider')}` `{hit.get('session_id')}`"
        f" cwd=`{cwd}` title=`{title}`\n"
        f"  context: {context}"
    )


def render_report(
    queries: list[str],
    sqlite_results: dict[str, list[dict[str, Any]]],
    typesense_raw_results: dict[str, list[dict[str, Any]]],
    typesense_planned_results: dict[str, list[dict[str, Any]]],
    *,
    doc_count: int,
    session_count: int,
    server_version: str,
) -> str:
    lines = [
        "# Typesense Experiment Report",
        "",
        f"- Server version: `{server_version}`",
        f"- Indexed sessions: `{session_count}`",
        f"- Indexed exchange-window docs: `{doc_count}`",
        "",
        "This compares three retrieval paths:",
        "- `sqlite`: current shipped ranker",
        "- `typesense-raw`: raw natural-language query sent directly to Typesense",
        "- `typesense-planned`: current QueryPlan anchors + intent projected into Typesense search params",
        "",
    ]
    for query in queries:
        sqlite_lines = [_format_hit(hit) for hit in sqlite_results.get(query, [])] or ["- no hits"]
        raw_lines = [_format_hit(hit) for hit in typesense_raw_results.get(query, [])] or [
            "- no hits"
        ]
        planned_lines = [
            _format_hit(hit) for hit in typesense_planned_results.get(query, [])
        ] or ["- no hits"]
        lines.extend(
            [
                f"## Query: `{query}`",
                "",
                "### sqlite",
                *sqlite_lines,
                "",
                "### typesense-raw",
                *raw_lines,
                "",
                "### typesense-planned",
                *planned_lines,
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _wait_for_health(server: TypesenseServer, timeout_s: float = 20.0) -> str:
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            resp = _typesense_request(server, "GET", "/health")
            if isinstance(resp, dict) and resp.get("ok"):
                return str(resp.get("version", "unknown"))
        except (HTTPError, URLError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"Typesense failed health check: {last_error}")


def start_temp_typesense() -> TypesenseServer:
    binary = _ensure_typesense_binary()
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="claude_browse_typesense_")
    api_key = "claude-browse-experiment"
    process = subprocess.Popen(
        [
            binary,
            f"--data-dir={data_dir}",
            f"--api-key={api_key}",
            f"--listen-port={port}",
            "--enable-cors",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    server = TypesenseServer(
        process=process,
        host="127.0.0.1",
        port=port,
        api_key=api_key,
        data_dir=data_dir,
    )
    try:
        _wait_for_health(server)
    except Exception:
        server.close()
        shutil.rmtree(data_dir, ignore_errors=True)
        raise
    return server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--query",
        action="append",
        help="Specific query to run. Can be passed multiple times.",
    )
    parser.add_argument(
        "--output",
        help="Optional markdown output path.",
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help=f"Typesense collection name (default: {DEFAULT_COLLECTION})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    queries = args.query or DEFAULT_QUERIES

    conn = fts.open_db()
    fts.reindex(conn)

    docs = _build_window_docs(conn)
    session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    server = start_temp_typesense()
    try:
        server_version = _wait_for_health(server)
        _create_collection(server, args.collection)
        _import_docs(server, args.collection, docs)

        sqlite_results = {query: _search_sqlite(conn, query) for query in queries}
        typesense_raw_results = {
            query: _search_typesense(server, args.collection, query, mode="raw")
            for query in queries
        }
        typesense_planned_results = {
            query: _search_typesense(server, args.collection, query, mode="planned")
            for query in queries
        }
        report = render_report(
            queries,
            sqlite_results,
            typesense_raw_results,
            typesense_planned_results,
            doc_count=len(docs),
            session_count=session_count,
            server_version=server_version,
        )
    finally:
        server.close()
        shutil.rmtree(server.data_dir, ignore_errors=True)

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report)
    print(report)


if __name__ == "__main__":
    main()

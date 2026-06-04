"""Local JSONL diagnostics for claude-browse search sessions."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import fts
from .query import build_query_plan

DEFAULT_LOG_PATH = os.path.expanduser(
    "~/.claude/cache/claude-browse-search.log.jsonl"
)
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
MAX_TOP_RESULTS = 8
MAX_FIELD_CHARS = 320
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def log_path() -> str:
    return os.path.expanduser(
        os.environ.get("CLAUDE_BROWSE_LOG_PATH") or DEFAULT_LOG_PATH
    )


def is_enabled() -> bool:
    return os.environ.get("CLAUDE_BROWSE_LOG", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _max_bytes() -> int:
    raw = os.environ.get("CLAUDE_BROWSE_LOG_MAX_BYTES", "")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_MAX_BYTES


def _safe_text(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    text = str(value or "")
    text = text.replace("\x01", "").replace("\x02", "")
    text = _CONTROL_RE.sub(" ", text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _rotate_if_needed(path: str) -> None:
    max_bytes = _max_bytes()
    if max_bytes <= 0:
        return
    try:
        if os.path.getsize(path) <= max_bytes:
            return
    except OSError:
        return
    rotated = f"{path}.1"
    try:
        if os.path.exists(rotated):
            os.unlink(rotated)
        os.replace(path, rotated)
    except OSError:
        pass


def log_event(event: str, payload: dict[str, Any] | None = None) -> None:
    """Append a best-effort local diagnostic event.

    Logging must never affect the picker. All filesystem errors are swallowed.
    """
    if not is_enabled():
        return
    path = log_path()
    record = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event": event,
    }
    if payload:
        record.update(payload)
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    except OSError:
        return


def _summarize_result(row: dict[str, Any], rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "session_id": _safe_text(row.get("session_id"), 80),
        "provider": _safe_text(row.get("provider"), 32),
        "cwd": _safe_text(row.get("cwd"), 240),
        "title": _safe_text(row.get("name") or row.get("first_msg"), 160),
        "context": _safe_text(row.get("context"), 260),
        "match_label": _safe_text(row.get("match_label"), 64),
        "match_confidence": _safe_text(row.get("match_confidence"), 32),
        "match_timestamp": _safe_text(row.get("match_timestamp"), 40),
        "match_segment_idx": row.get("match_segment_idx"),
        "current_cwd_score": row.get("current_cwd_score"),
        "prefix_fallback": bool(row.get("prefix_fallback")),
        "phrase_fallback": bool(row.get("phrase_fallback")),
        "message_count": row.get("msg_count"),
    }


def log_search(
    query: str,
    results: list[dict[str, Any]],
    *,
    ranker: str,
    cwd_filter: str | None,
    limit: int,
    elapsed_ms: float,
    current_cwd: str | None = None,
) -> None:
    try:
        plan = build_query_plan(query)
        payload = {
            "query": _safe_text(query, 500),
            "ranker": ranker,
            "cwd_filter": _safe_text(cwd_filter, 240),
            "current_cwd": _safe_text(current_cwd, 240),
            "limit": limit,
            "result_count": len(results),
            "elapsed_ms": round(elapsed_ms, 2),
            "normalized_terms": list(plan.normalized_terms),
            "anchor_terms": list(plan.anchor_terms),
            "descriptive": plan.descriptive,
            "low_confidence": plan.low_confidence,
            "top": [
                _summarize_result(row, idx)
                for idx, row in enumerate(results[:MAX_TOP_RESULTS], 1)
            ],
        }
    except Exception:
        return
    log_event("search", payload)


def log_selection(
    query: str,
    *,
    action: str,
    target_provider: str,
    session: dict[str, Any],
) -> None:
    try:
        payload = {
            "query": _safe_text(query, 500),
            "action": action,
            "target_provider": _safe_text(target_provider, 32),
            "selected": _summarize_result(session, 1),
        }
    except Exception:
        return
    log_event("selection", payload)


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    """Read the tail of the diagnostics log for ad hoc debugging."""
    path = log_path()
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            events.append(loaded)
    return events


def live_search_snapshot(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Debug helper: run the current ranker and return summarized top results."""
    start = time.perf_counter()
    conn = fts.open_db()
    try:
        fts.reindex(conn)
        results = fts.search_ranked(conn, query, limit=limit)
    finally:
        conn.close()
    elapsed_ms = (time.perf_counter() - start) * 1000
    log_search(
        query,
        results,
        ranker="v1",
        cwd_filter=None,
        limit=limit,
        elapsed_ms=elapsed_ms,
    )
    return [_summarize_result(row, idx) for idx, row in enumerate(results, 1)]

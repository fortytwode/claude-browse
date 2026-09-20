"""Publish local dense-search windows to the optional shared Firestore index.

This module deliberately has no import-time Firestore dependency.  The local
index remains the source of truth; publication is an explicit, retry-safe
projection for cross-machine search.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import struct
import time
from dataclasses import dataclass
from typing import Any

PROJECT = os.environ.get("CLAUDE_BROWSE_BOARD_PROJECT", "team-projects-480520")
DATABASE = os.environ.get("CLAUDE_BROWSE_BOARD_DATABASE", "creative-dashboard")
COLLECTION = os.environ.get("CLAUDE_BROWSE_SHARED_SEARCH_COLLECTION", "shared_search_windows")
ENV_FLAG = "CLAUDE_BROWSE_SHARED_SEARCH_ENABLED"
DEFAULT_BATCH_SIZE = 400  # Firestore permits at most 500 writes per batch.
SNIPPET_LIMIT = 1_200


@dataclass(frozen=True)
class PublishReport:
    """Counts returned by :func:`publish`; safe to expose in a CLI/status UI."""

    scanned: int = 0
    published: int = 0
    unchanged: int = 0
    deleted: int = 0
    batches: int = 0
    skipped: bool = False


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def _firestore_client() -> Any:
    """Create a client only for an explicitly requested real publication."""
    from google.cloud import firestore  # optional ``board-sync`` extra

    return firestore.Client(project=PROJECT, database=DATABASE)


def _document_id(host: str, sid: str, window_id: int) -> str:
    """A Firestore-safe stable ID derived from host, session, and window."""
    value = f"{host}\0{sid}\0{window_id}".encode()
    return "window-" + hashlib.sha256(value).hexdigest()


def _vector_from_blob(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4])) if count else []


def _snippet(text: str) -> str:
    """Keep enough context for search results, never a whole transcript."""
    return " ".join(text.split())[:SNIPPET_LIMIT]


def _payload_hash(payload: dict[str, Any]) -> str:
    # JSON gives a portable checkpoint even when SQLite rowids change later.
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_checkpoint_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shared_search_publications (
            doc_id TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            published_at REAL NOT NULL
        )
        """
    )


def publish(
    conn: sqlite3.Connection,
    *,
    client: Any | None = None,
    host: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    force: bool = False,
    vector_factory: Any | None = None,
) -> PublishReport:
    """Publish local dense embeddings, deleting only this host's stale docs.

    ``client`` makes the operation testable without credentials or network
    access.  With no client, the environment flag must opt in before a
    Firestore client is constructed.
    """
    if batch_size < 1 or batch_size > 500:
        raise ValueError("batch_size must be between 1 and 500")
    if client is None and not enabled():
        return PublishReport(skipped=True)
    if client is None:
        client = _firestore_client()
        from google.cloud.firestore_v1.vector import Vector

        vector_factory = Vector
    host = host or socket.gethostname()
    _ensure_checkpoint_table(conn)

    rows = conn.execute(
        """
        SELECT w.rowid, w.sid, w.window_idx, w.timestamp, w.text,
               e.model, e.dimensions, e.vector, e.content_hash, e.indexed_at,
               s.provider, s.title, s.cwd, s.timestamp, s.last_timestamp
        FROM dense_embeddings e
        JOIN semantic_windows w ON w.rowid = e.window_id
        LEFT JOIN sessions s ON s.sid = w.sid
        ORDER BY w.rowid
        """
    )
    previous = {
        row[0]: row[1]
        for row in conn.execute("SELECT doc_id, payload_hash FROM shared_search_publications")
    }
    collection = client.collection(COLLECTION)
    seen: set[str] = set()
    scanned = published = deleted = batches = 0
    pending: list[tuple[str, str, dict[str, Any] | None, str | None]] = []

    def flush() -> None:
        nonlocal published, deleted, batches
        if not pending:
            return
        batch = client.batch()
        for action, doc_id, payload, _digest in pending:
            ref = collection.document(doc_id)
            if action == "set":
                body = dict(payload)
                if vector_factory is not None:
                    body["embedding"] = vector_factory(body["embedding"])
                batch.set(ref, body)
            else:
                batch.delete(ref)
        batch.commit()
        # A checkpoint follows each successful remote batch. Repeating a
        # partially completed run is therefore safe and cheap.
        now = time.time()
        for action, doc_id, _payload, digest in pending:
            if action == "set":
                conn.execute(
                    """INSERT INTO shared_search_publications(doc_id, payload_hash, published_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(doc_id) DO UPDATE SET
                         payload_hash = excluded.payload_hash,
                         published_at = excluded.published_at""",
                    (doc_id, digest, now),
                )
                published += 1
            else:
                conn.execute("DELETE FROM shared_search_publications WHERE doc_id = ?", (doc_id,))
                deleted += 1
        conn.commit()
        batches += 1
        pending.clear()

    for row in rows:
        scanned += 1
        (
            window_id,
            sid,
            window_idx,
            window_timestamp,
            text,
            model,
            dimensions,
            vector_blob,
            content_hash,
            indexed_at,
            provider,
            title,
            cwd,
            session_timestamp,
            last_timestamp,
        ) = row
        # semantic_windows.rowid is recreated on reindex; window_idx is stable
        # for the same session content and prevents needless delete/recreate.
        doc_id = _document_id(host, str(sid), int(window_idx))
        payload = {
            "host": host,
            "session_id": str(sid),
            "window_id": int(window_id),
            "window_index": int(window_idx),
            "provider": str(provider or "unknown"),
            "title": str(title or ""),
            "cwd": str(cwd or ""),
            "timestamp": window_timestamp or last_timestamp or session_timestamp,
            "session_timestamp": session_timestamp,
            "last_timestamp": last_timestamp,
            "model": str(model),
            "dimensions": int(dimensions),
            "content_hash": str(content_hash),
            "indexed_at": float(indexed_at),
            "text_snippet": _snippet(str(text)),
            "embedding": _vector_from_blob(bytes(vector_blob)),
        }
        seen.add(doc_id)
        digest = _payload_hash(payload)
        if force or previous.get(doc_id) != digest:
            pending.append(("set", doc_id, payload, digest))
            if len(pending) >= batch_size:
                flush()
    for doc_id in previous.keys() - seen:
        pending.append(("delete", doc_id, None, None))
        if len(pending) >= batch_size:
            flush()
    flush()
    return PublishReport(
        scanned=scanned,
        published=published,
        unchanged=scanned - published,
        deleted=deleted,
        batches=batches,
    )

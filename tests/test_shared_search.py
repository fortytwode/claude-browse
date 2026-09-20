from __future__ import annotations

import sqlite3
import struct

import pytest

from claude_browse import shared_search


class _Doc:
    def __init__(self, store, key):
        self.store, self.key = store, key


class _Collection:
    def __init__(self, store):
        self.store = store

    def document(self, key):
        return _Doc(self.store, key)


class _Batch:
    def __init__(self, store):
        self.store, self.ops = store, []

    def set(self, ref, body):
        self.ops.append(("set", ref, body))

    def delete(self, ref):
        self.ops.append(("delete", ref, None))

    def commit(self):
        for kind, ref, body in self.ops:
            if kind == "set":
                self.store[ref.key] = dict(body)
            else:
                self.store.pop(ref.key, None)


class _Client:
    def __init__(self):
        self.store = {}
        self.batches = 0

    def collection(self, _name):
        return _Collection(self.store)

    def batch(self):
        self.batches += 1
        return _Batch(self.store)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE sessions (sid TEXT PRIMARY KEY, provider TEXT, cwd TEXT,
        timestamp TEXT, last_timestamp TEXT, title TEXT);
      CREATE TABLE semantic_windows (rowid INTEGER PRIMARY KEY, sid TEXT,
        window_idx INTEGER, timestamp TEXT, text TEXT);
      CREATE TABLE dense_embeddings (window_id INTEGER PRIMARY KEY, sid TEXT,
        model TEXT, dimensions INTEGER, content_hash TEXT, vector BLOB,
        indexed_at REAL);
    """)
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        ("s/one", "codex", "/repo", "2026-01-01", "2026-01-02", "Anna 1:1"),
    )
    conn.execute(
        "INSERT INTO semantic_windows VALUES (?, ?, ?, ?, ?)",
        (7, "s/one", 0, "2026-01-02", "secret " + "detail " * 300),
    )
    conn.execute(
        "INSERT INTO dense_embeddings VALUES (?, ?, ?, ?, ?, ?, ?)",
        (7, "s/one", "embedding", 2, "hash", struct.pack("<2f", 1.0, 2.0), 4.0),
    )
    return conn


def _publish(conn, **kwargs):
    return shared_search.publish(conn, model="embedding", dimensions=2, **kwargs)


def test_publish_is_idempotent_and_never_needs_firestore_credentials():
    conn, client = _db(), _Client()
    first = _publish(conn, client=client, host="mac")
    assert first.published == 1 and first.batches == 1
    doc = next(iter(client.store.values()))
    assert doc["embedding"] == [1.0, 2.0]
    assert doc["text_snippet"] != "secret " + "detail " * 300
    assert doc["session_id"] == "s/one"
    second = _publish(conn, client=client, host="mac")
    assert second.unchanged == 1 and second.batches == 0


def test_publish_deletes_only_previously_published_stale_documents():
    conn, client = _db(), _Client()
    _publish(conn, client=client, host="mac")
    conn.execute("DELETE FROM dense_embeddings")
    report = _publish(conn, client=client, host="mac")
    assert report.deleted == 1
    assert client.store == {}


def test_publish_is_opt_in_when_client_is_not_injected(monkeypatch):
    monkeypatch.delenv(shared_search.ENV_FLAG, raising=False)
    assert _publish(_db()).skipped


def test_document_ids_are_stable_and_host_scoped():
    assert shared_search._document_id("a", "s", 1) == shared_search._document_id("a", "s", 1)
    assert shared_search._document_id("a", "s", 1) != shared_search._document_id("b", "s", 1)


def test_publish_wraps_vector_for_firestore_index():
    conn, client = _db(), _Client()
    _publish(conn, client=client, host="mac", vector_factory=tuple)
    assert next(iter(client.store.values()))["embedding"] == (1.0, 2.0)


def test_default_publication_rejects_incompatible_embeddings():
    conn, client = _db(), _Client()
    assert shared_search.publish(conn, client=client, host="mac").published == 0
    conn.execute(
        "UPDATE dense_embeddings SET model = ?, dimensions = ?",
        (shared_search.MODEL, shared_search.DIMENSIONS),
    )
    # Metadata alone is insufficient: a two-float blob cannot enter the
    # fixed 256-dimensional Firestore vector index.
    assert shared_search.publish(conn, client=client, host="mac").published == 0
    assert client.store == {}


def test_partial_publish_resumes_from_committed_batch():
    conn, client = _db(), _Client()
    conn.execute(
        "INSERT INTO semantic_windows VALUES (?, ?, ?, ?, ?)",
        (8, "s/one", 1, "2026-01-02", "another window"),
    )
    conn.execute(
        "INSERT INTO dense_embeddings VALUES (?, ?, ?, ?, ?, ?, ?)",
        (8, "s/one", "embedding", 2, "hash-2", struct.pack("<2f", 2.0, 3.0), 4.0),
    )
    original_batch = client.batch

    def failing_batch():
        batch = original_batch()
        if client.batches == 2:
            batch.commit = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
        return batch

    client.batch = failing_batch
    with pytest.raises(RuntimeError, match="offline"):
        _publish(conn, client=client, host="mac", batch_size=1)
    assert len(client.store) == 1
    client.batch = original_batch
    report = _publish(conn, client=client, host="mac", batch_size=1)
    assert report.published == 1 and report.unchanged == 1
    assert len(client.store) == 2

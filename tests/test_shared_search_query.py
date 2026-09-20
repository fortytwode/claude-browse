from __future__ import annotations

from claude_browse import shared_search_query


class _Doc:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _Collection:
    def __init__(self, docs):
        self.docs = docs
        self.options = None

    def find_nearest(self, **options):
        self.options = options
        return self

    def stream(self):
        return iter(_Doc(doc) for doc in self.docs)


class _Client:
    def __init__(self, docs):
        self.collection_name = None
        self.windows = _Collection(docs)

    def collection(self, name):
        self.collection_name = name
        return self.windows


def test_shared_query_deduplicates_windows_and_keeps_best_session_match():
    client = _Client(
        [
            {"host": "mac-a", "session_id": "one", "title": "First", "text_snippet": "weak", "vector_distance": 0.48, "model": "text-embedding-3-small", "dimensions": 256},
            {"host": "mac-b", "session_id": "two", "title": "Second", "text_snippet": "second", "vector_distance": 0.32, "model": "text-embedding-3-small", "dimensions": 256},
            {"host": "mac-a", "session_id": "one", "title": "First", "text_snippet": "best", "vector_distance": 0.21, "model": "text-embedding-3-small", "dimensions": 256},
            {"host": "mac-a", "session_id": "wrong-model", "vector_distance": 0.01, "model": "other", "dimensions": 256},
        ]
    )
    calls = []

    results = shared_search_query.search(
        "find my review of Anna",
        client=client,
        embed=lambda query, model, dimensions: calls.append(query) or [0.1] * dimensions,
        vector_factory=list,
        distance_measure="COSINE",
    )

    assert calls == ["find my review of Anna"]
    assert client.collection_name == "shared_search_windows"
    assert client.windows.options["limit"] == 200
    assert [(item["host"], item["session_id"], item["snippet"]) for item in results] == [
        ("mac-a", "one", "best"),
        ("mac-b", "two", "second"),
    ]
    assert results[0]["score"] > results[1]["score"]


def test_shared_query_skips_short_input_without_embedding_call():
    client = _Client([])
    assert shared_search_query.search(
        "Anna",
        client=client,
        embed=lambda *_: (_ for _ in ()).throw(AssertionError("unexpected embedding")),
        vector_factory=list,
        distance_measure="COSINE",
    ) == []

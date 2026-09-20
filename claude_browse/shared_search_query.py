"""Read semantic matches from the optional cross-machine Firestore index."""

from __future__ import annotations

import os
from typing import Any, Callable

from . import fts, shared_search


def _embed(query: str, model: str, dimensions: int) -> list[float]:
    if not os.environ.get("OPENAI_API_KEY"):
        from .board.sync import _load_env_fallback

        _load_env_fallback()
    vectors = fts._request_openai_embeddings([query], model=model, dimensions=dimensions)
    return vectors[0] if len(vectors) == 1 else []


def search(
    query: str,
    *,
    client: Any | None = None,
    embed: Callable[[str, str, int], list[float]] | None = None,
    vector_factory: Callable[[list[float]], Any] | None = None,
    distance_measure: Any | None = None,
) -> list[dict[str, Any]]:
    """Return the nearest unique sessions, independent of source Mac presence.

    A short query is kept on the local lexical path.  The injected arguments
    make the Firestore contract testable without credentials or network calls.
    """
    query = query.strip()
    if len(query) < 8:
        return []
    if client is None and not shared_search.enabled():
        return []
    model = fts._dense_embedding_model()
    dimensions = fts._dense_embedding_dimensions()
    embed = embed or _embed
    vector = embed(query, model, dimensions)
    if len(vector) != dimensions:
        return []
    if client is None:
        client = shared_search._firestore_client()
    if vector_factory is None or distance_measure is None:
        from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
        from google.cloud.firestore_v1.vector import Vector

        vector_factory = vector_factory or Vector
        distance_measure = distance_measure or DistanceMeasure.COSINE
    collection = client.collection(shared_search.COLLECTION)
    nearest = collection.find_nearest(
        vector_field="embedding",
        query_vector=vector_factory(vector),
        distance_measure=distance_measure,
        limit=200,
        distance_result_field="vector_distance",
    )
    matches: dict[tuple[str, str], dict[str, Any]] = {}
    for snapshot in nearest.stream():
        row = snapshot.to_dict() or {}
        if row.get("model") != model or row.get("dimensions") != dimensions:
            continue
        host, sid = str(row.get("host") or ""), str(row.get("session_id") or "")
        if not host or not sid:
            continue
        try:
            distance = float(row["vector_distance"])
        except (KeyError, TypeError, ValueError):
            continue
        candidate = {
            "host": host,
            "session_id": sid,
            "title": str(row.get("title") or ""),
            "snippet": str(row.get("text_snippet") or ""),
            "provider": str(row.get("provider") or ""),
            "cwd": str(row.get("cwd") or ""),
            "last_timestamp": row.get("last_timestamp"),
            "score": 1.0 - distance,
        }
        key = host, sid
        if key not in matches or candidate["score"] > matches[key]["score"]:
            matches[key] = candidate
    return sorted(matches.values(), key=lambda item: item["score"], reverse=True)[:50]

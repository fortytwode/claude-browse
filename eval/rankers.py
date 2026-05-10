"""Pluggable ranker registry.

Each ranker is a function (query, conn, limit) -> list[sid] in rank order.
Add new ranker variants here; the eval harness picks them up via the RANKERS
dict so a single eval run can compare baseline vs. candidate side-by-side.
"""

from __future__ import annotations

import sqlite3

from claude_browse import fts


def ranker_current(query: str, conn: sqlite3.Connection, limit: int = 50) -> list[str]:
    """Today's production ranker: FTS5 token match, then recency sort."""
    rows = fts.search(conn, query, limit=limit)
    return [r["session_id"] for r in rows]


def ranker_v1(query: str, conn: sqlite3.Connection, limit: int = 50) -> list[str]:
    """v1: multi-column BM25 with weights (cwd:10, title:8, first_msg:5,
    user:1, asst:0.3, boilerplate:0.05) blended with exp-decay recency
    (alpha=3, half_life=30d). See fts.search_ranked for the math."""
    rows = fts.search_ranked(conn, query, limit=limit)
    return [r["session_id"] for r in rows]


# Future ranker variants register here. Keep ranker_current as the baseline
# even after newer variants ship, so historical eval reports stay reproducible.
RANKERS: dict[str, callable] = {
    "current": ranker_current,
    "v1": ranker_v1,
}

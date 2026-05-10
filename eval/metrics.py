"""Ranking metrics over a labeled query set.

Grades convention (matches queries.json):
    3 = the session the user actually wanted; perfect hit
    2 = also acceptable; would satisfy this query
    1 = related but wouldn't satisfy
    0 = not relevant (default for any sid not in the labels)

Two thresholds are configurable per metric:
    binary_threshold: grade >= this counts as "relevant" for MRR / P@1 / Recall
    Default 2 (i.e. only 2s and 3s count); set to 1 if you want a looser bar.

NDCG uses the raw graded values directly (no threshold).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class QueryResult:
    """Per-query metric snapshot. Aggregates are computed across these."""

    query: str
    mrr: float            # 1/rank of first relevant; 0 if none in results
    p_at_1: int           # 1 if rank 1 is relevant, else 0
    ndcg_5: float | None  # None if query has no graded labels
    recall_10: float | None
    first_relevant_rank: int | None  # None if no labeled-relevant sid in results
    missing_relevant: list[str]      # labeled-relevant sids that didn't appear
    latency_ms: float


def mrr(ranked_sids: list[str], grades: dict[str, int], threshold: int = 2) -> tuple[float, int | None]:
    """Reciprocal rank of the first sid with grade >= threshold.

    Returns (rr, rank). rr is 0.0 and rank is None when no relevant sid appears.
    """
    for i, sid in enumerate(ranked_sids, start=1):
        if grades.get(sid, 0) >= threshold:
            return 1.0 / i, i
    return 0.0, None


def p_at_1(ranked_sids: list[str], grades: dict[str, int], threshold: int = 2) -> int:
    if not ranked_sids:
        return 0
    return 1 if grades.get(ranked_sids[0], 0) >= threshold else 0


def _dcg(grade_seq: list[int], k: int) -> float:
    return sum(
        (2 ** g - 1) / math.log2(i + 2)
        for i, g in enumerate(grade_seq[:k])
    )


def ndcg_at_k(ranked_sids: list[str], grades: dict[str, int], k: int = 5) -> float | None:
    """Standard graded NDCG. Returns None if no positive grades exist for the
    query (otherwise IDCG is 0 and the metric is undefined)."""
    if not grades or max(grades.values(), default=0) == 0:
        return None
    actual = [grades.get(sid, 0) for sid in ranked_sids[:k]]
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = _dcg(ideal, k)
    if idcg == 0:
        return None
    return _dcg(actual, k) / idcg


def recall_at_k(ranked_sids: list[str], grades: dict[str, int], k: int = 10, threshold: int = 1) -> float | None:
    """Fraction of labeled-relevant sids (grade >= threshold) that appear in
    the top k. Catches "we found them, just not at the top.\""""
    relevant = {sid for sid, g in grades.items() if g >= threshold}
    if not relevant:
        return None
    found = sum(1 for sid in ranked_sids[:k] if sid in relevant)
    return found / len(relevant)


def missing(ranked_sids: list[str], grades: dict[str, int], threshold: int = 1) -> list[str]:
    """Labeled-relevant sids that didn't appear anywhere in the ranked list.
    Visible failure mode: the ranker can't even retrieve them, never mind rank."""
    relevant = {sid for sid, g in grades.items() if g >= threshold}
    in_results = set(ranked_sids)
    return sorted(relevant - in_results)


def aggregate(results: list[QueryResult]) -> dict[str, float]:
    """Mean of each metric across queries. Skips None values per-metric so a
    query with no graded labels doesn't tank the NDCG average."""
    def mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else float("nan")

    return {
        "MRR":       mean(r.mrr for r in results),
        "P@1":       mean(r.p_at_1 for r in results),
        "NDCG@5":    mean(r.ndcg_5 for r in results),
        "Recall@10": mean(r.recall_10 for r in results),
        "p50_ms":    _percentile([r.latency_ms for r in results], 0.5),
        "p95_ms":    _percentile([r.latency_ms for r in results], 0.95),
        "n_queries": len(results),
    }


def _percentile(vals: list[float], q: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    i = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[i]

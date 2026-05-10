"""Eval harness for the claude-browse ranker.

Usage:
    python -m eval.run                       # baseline ranker, all queries
    python -m eval.run --ranker current,v1   # compare two rankers side-by-side
    python -m eval.run --query musopia       # single query, verbose breakdown
    python -m eval.run --threshold 1         # looser relevance bar (grade>=1)

Reports MRR, P@1, NDCG@5, Recall@10, p50/p95 latency. Per-query rows show
where each ranker actually puts the labeled-relevant sessions, so a regression
is attributable to specific queries, not just an aggregate movement.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Allow `python eval/run.py` (not just `python -m eval.run`) by putting the
# repo root on sys.path so `claude_browse` and `eval` both import.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from claude_browse import fts  # noqa: E402

from eval import metrics  # noqa: E402
from eval.rankers import RANKERS  # noqa: E402

DEFAULT_QUERIES_PATH = os.path.expanduser(
    os.environ.get(
        "CLAUDE_BROWSE_EVAL_QUERIES",
        "~/.claude/cache/claude-browse-eval/queries.json",
    )
)


def load_queries(path: str) -> list[dict]:
    """Load the labeled query set. Resolves sid prefixes against the live
    FTS index so labels can be written as 8-char prefixes for readability."""
    if not os.path.exists(path):
        raise SystemExit(
            f"No labeled query set at {path}.\n"
            "Run `python -m eval.label` to create one."
        )
    with open(path) as f:
        data = json.load(f)
    queries = data.get("queries", [])
    if not queries:
        raise SystemExit(f"{path} has no queries yet. Run `python -m eval.label`.")
    return queries


def resolve_sids(conn, queries: list[dict]) -> list[dict]:
    """Expand sid prefixes -> full sids. Errors loudly on ambiguous or unknown
    prefixes so a typo in the labels can never silently pass."""
    resolved = []
    for q in queries:
        rel = []
        for entry in q.get("relevant", []):
            sid_or_prefix = entry["sid"]
            full = _resolve_one(conn, sid_or_prefix)
            rel.append({**entry, "sid": full})
        resolved.append({**q, "relevant": rel})
    return resolved


def _resolve_one(conn, sid_or_prefix: str) -> str:
    if len(sid_or_prefix) >= 32:
        return sid_or_prefix
    rows = conn.execute(
        "SELECT sid FROM sessions WHERE sid LIKE ? || '%'", (sid_or_prefix,)
    ).fetchall()
    if not rows:
        raise SystemExit(f"Sid prefix not found in index: {sid_or_prefix!r}")
    if len(rows) > 1:
        ids = ", ".join(r[0][:12] for r in rows[:5])
        raise SystemExit(
            f"Sid prefix {sid_or_prefix!r} is ambiguous "
            f"({len(rows)} matches: {ids}...). Use a longer prefix."
        )
    return rows[0][0]


def run_ranker(ranker_name: str, conn, queries: list[dict], threshold: int) -> list[metrics.QueryResult]:
    ranker = RANKERS[ranker_name]
    results = []
    for q in queries:
        grades = {e["sid"]: e["grade"] for e in q["relevant"]}
        t0 = time.perf_counter()
        ranked = ranker(q["q"], conn, limit=50)
        latency_ms = (time.perf_counter() - t0) * 1000

        rr, rank = metrics.mrr(ranked, grades, threshold=threshold)
        # Recall uses the same threshold as MRR/P@1 so a ranker that correctly
        # demotes grade-1 ("tangentially related") sessions doesn't get charged
        # as a recall regression. `missing_relevant` keeps threshold=1 so any
        # labeled session that didn't appear at all stays visible.
        results.append(
            metrics.QueryResult(
                query=q["q"],
                mrr=rr,
                p_at_1=metrics.p_at_1(ranked, grades, threshold=threshold),
                ndcg_5=metrics.ndcg_at_k(ranked, grades, k=5),
                recall_10=metrics.recall_at_k(ranked, grades, k=10, threshold=threshold),
                first_relevant_rank=rank,
                missing_relevant=metrics.missing(ranked, grades, threshold=1),
                latency_ms=latency_ms,
            )
        )
    return results


def print_per_query(results_by_ranker: dict[str, list[metrics.QueryResult]]) -> None:
    """One row per query. When multiple rankers are passed, rank-of-first is
    shown for each so regressions jump out visually."""
    rankers = list(results_by_ranker.keys())
    n = len(results_by_ranker[rankers[0]])

    header = f"{'query':<32} " + " ".join(f"{r:>10}" for r in rankers) + "  notes"
    print(header)
    print("-" * len(header))
    for i in range(n):
        row = results_by_ranker[rankers[0]][i]
        cells = []
        for r in rankers:
            qr = results_by_ranker[r][i]
            rank_str = str(qr.first_relevant_rank) if qr.first_relevant_rank else "—"
            cells.append(f"{rank_str:>10}")
        miss = ""
        if row.missing_relevant:
            miss = f"  ({len(row.missing_relevant)} not retrieved)"
        print(f"{row.query[:32]:<32} " + " ".join(cells) + miss)


def print_aggregate(results_by_ranker: dict[str, list[metrics.QueryResult]]) -> None:
    print()
    print(f"{'metric':<12}" + "".join(f"{r:>12}" for r in results_by_ranker))
    print("-" * (12 + 12 * len(results_by_ranker)))
    aggs = {r: metrics.aggregate(res) for r, res in results_by_ranker.items()}
    sample = next(iter(aggs.values()))
    for k in sample:
        if k == "n_queries":
            continue
        row = f"{k:<12}"
        for r, agg in aggs.items():
            v = agg[k]
            row += f"{v:>12.3f}" if isinstance(v, float) else f"{v:>12}"
        print(row)
    print()
    print(f"n queries: {sample['n_queries']}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--ranker", default="current",
                   help="comma-separated ranker names (see eval/rankers.py)")
    p.add_argument("--query", help="run only queries containing this substring")
    p.add_argument("--threshold", type=int, default=2,
                   help="grade >= threshold counts as relevant for MRR/P@1 (default 2)")
    p.add_argument("--queries", default=DEFAULT_QUERIES_PATH,
                   help="path to labeled queries JSON")
    args = p.parse_args()

    ranker_names = [r.strip() for r in args.ranker.split(",") if r.strip()]
    for r in ranker_names:
        if r not in RANKERS:
            raise SystemExit(f"Unknown ranker: {r}. Available: {list(RANKERS)}")

    conn = fts.open_db()
    fts.reindex(conn)  # cheap if already up-to-date; ensures labeled sids resolve

    queries = load_queries(args.queries)
    queries = resolve_sids(conn, queries)
    if args.query:
        queries = [q for q in queries if args.query.lower() in q["q"].lower()]
        if not queries:
            raise SystemExit(f"No queries matched substring {args.query!r}")

    results_by_ranker = {
        r: run_ranker(r, conn, queries, threshold=args.threshold)
        for r in ranker_names
    }
    print_per_query(results_by_ranker)
    print_aggregate(results_by_ranker)


if __name__ == "__main__":
    main()

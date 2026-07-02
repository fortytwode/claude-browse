"""Convert search-log selections into the eval harness's labeled query set.

Every query-driven selection in the diagnostics log is a (query, known-good
session) pair: the user typed the query, saw the ranked list, and picked that
session. Grade it 3 and merge into queries.json so `python -m eval.run` has
real queries to score rankers against.

Empty-query selections (recency browsing) and coach-row picks carry no
ranking signal and are skipped. Selected sessions that no longer resolve in
the live index are skipped too, because eval.run errors loudly on unknown
sids.

Usage:
    python -m eval.from_log            # merge new pairs into queries.json
    python -m eval.from_log --dry-run  # show what would be added
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from claude_browse import fts, search_log  # noqa: E402

COACH_SESSION_ID = "__coach__"

DEFAULT_QUERIES_PATH = os.path.expanduser(
    os.environ.get(
        "CLAUDE_BROWSE_EVAL_QUERIES",
        "~/.claude/cache/claude-browse-eval/queries.json",
    )
)


def read_log_events(path: str) -> list[dict]:
    """Read log events oldest-first, including the rotated predecessor."""
    events: list[dict] = []
    for candidate in (f"{path}.1", path):
        try:
            with open(candidate, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict):
                events.append(loaded)
    return events


def selection_pairs(events: list[dict]) -> list[dict]:
    """Extract (query, selected sid) pairs worth labeling."""
    pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        if event.get("event") != "selection":
            continue
        query = (event.get("query") or "").strip()
        selected = event.get("selected") or {}
        sid = (selected.get("session_id") or "").strip()
        if not query or not sid or sid == COACH_SESSION_ID:
            continue
        key = (query, sid)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(
            {
                "query": query,
                "sid": sid,
                "ts": event.get("ts", ""),
                "action": event.get("action", ""),
            }
        )
    return pairs


def sid_resolves(conn, sid: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE sid LIKE ? || '%' LIMIT 1", (sid,)
    ).fetchone()
    return row is not None


def load_queries(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"queries": []}
    if not isinstance(data, dict) or not isinstance(data.get("queries"), list):
        return {"queries": []}
    return data


def merge_pairs(data: dict, pairs: list[dict], conn) -> tuple[int, int, int]:
    """Merge selection pairs into the labeled set in place.

    Returns (added_queries, added_sids, skipped_unresolved). Existing labels
    always win: a hand-graded entry for the same query/sid is never touched.
    """
    by_query = {q.get("q", ""): q for q in data["queries"]}
    added_queries = 0
    added_sids = 0
    skipped = 0
    for pair in pairs:
        sid12 = pair["sid"][:12]
        if not sid_resolves(conn, sid12):
            skipped += 1
            continue
        entry = by_query.get(pair["query"])
        if entry is None:
            date = pair["ts"][:10]
            entry = {
                "q": pair["query"],
                "relevant": [],
                "note": f"from search log {date}, action={pair['action']}",
                "preferred_action": "enter",
            }
            data["queries"].append(entry)
            by_query[pair["query"]] = entry
            added_queries += 1
        labeled_sids = {
            (rel.get("sid") or "")[:12] for rel in entry.get("relevant", [])
        }
        if sid12 in labeled_sids:
            continue
        entry.setdefault("relevant", []).append(
            {"sid": sid12, "grade": 3, "from_log": True}
        )
        added_sids += 1
    return added_queries, added_sids, skipped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=search_log.log_path(),
                        help="path to the search diagnostics log")
    parser.add_argument("--queries", default=DEFAULT_QUERIES_PATH,
                        help="path to the labeled queries JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()

    events = read_log_events(args.log)
    pairs = selection_pairs(events)
    if not pairs:
        print(f"No query-driven selections found in {args.log}.")
        return

    data = load_queries(args.queries)
    conn = fts.open_db()
    try:
        added_queries, added_sids, skipped = merge_pairs(data, pairs, conn)
    finally:
        conn.close()

    print(f"{len(pairs)} query-driven selections in the log")
    print(f"  {added_queries} new queries, {added_sids} new labeled sids")
    if skipped:
        print(f"  {skipped} skipped (session no longer in the index)")
    if args.dry_run:
        print("Dry run — nothing written.")
        return
    if added_queries == 0 and added_sids == 0:
        print("Nothing new to write.")
        return
    queries_path = Path(os.path.expanduser(args.queries))
    queries_path.parent.mkdir(parents=True, exist_ok=True)
    with open(queries_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    print(f"Wrote {len(data['queries'])} queries to {queries_path}")


if __name__ == "__main__":
    main()

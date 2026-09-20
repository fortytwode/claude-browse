"""Bounded, resumable publication of local browse history to shared search.

Run with ``python -m claude_browse.shared_search_cli`` on each Mac.  Every
invocation indexes a limited dense batch, then publishes all completed windows.
An hourly LaunchAgent can invoke it repeatedly until caught up.
"""

from __future__ import annotations

import os
import sys

from . import fts, shared_search
from .board.sync import _load_env_fallback


def run_once() -> shared_search.PublishReport:
    _load_env_fallback()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for shared semantic search")
    # This worker is an explicit opt-in.  The normal browse refresh keeps its
    # local-only behavior unless configured separately.
    os.environ["CLAUDE_BROWSE_DENSE_EMBEDDINGS"] = "1"
    os.environ[shared_search.ENV_FLAG] = "1"
    conn = fts.open_db()
    try:
        fts.reindex(conn)
        return shared_search.publish(conn)
    finally:
        conn.close()


def main() -> None:
    try:
        report = run_once()
    except Exception as exc:
        print(f"shared search sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(
        "shared search: "
        f"{report.scanned} dense windows, {report.published} published, "
        f"{report.unchanged} unchanged, {report.deleted} removed"
    )


if __name__ == "__main__":
    main()

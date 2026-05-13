"""Interactive labeler for the eval query set.

Workflow:
    $ python -m eval.label
    > query: musopia
    [shows top 20 from current ranker, with sid prefixes]
    > grades> 1:3 4:2 7:1
    > anything missing? (paste sid prefixes, blank to finish):
    [optionally add sids the ranker didn't return at all]
    > note (optional): looking for the MMP discussion
    [appends to ~/.claude/cache/claude-browse-eval/queries.json]

Grade scale:
    3 = the session I actually wanted; perfect hit
    2 = also acceptable; would satisfy this query
    1 = related but wouldn't satisfy
    (omit any sid that's not relevant; default grade is 0)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from claude_browse import fts  # noqa: E402
from claude_browse.core import folder_name, format_date  # noqa: E402

DEFAULT_PATH = os.path.expanduser(
    os.environ.get(
        "CLAUDE_BROWSE_EVAL_QUERIES",
        "~/.claude/cache/claude-browse-eval/queries.json",
    )
)


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {"queries": []}
    with open(path) as f:
        return json.load(f)


def save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def show_top(conn, query: str, limit: int = 20) -> list[dict]:
    """Run today's ranker; print a numbered candidate list for grading."""
    rows = fts.search(conn, query, limit=limit)
    print()
    if not rows:
        print(f"  (no results for {query!r})")
        return []
    for i, r in enumerate(rows, start=1):
        sid_prefix = (r["session_id"] or "?")[:8]
        date = format_date(r.get("last_timestamp") or r.get("timestamp"))
        fname = folder_name(r.get("cwd"), ())
        title = (r.get("name") or r.get("first_msg") or "")[:60].replace("\n", " ")
        msgs = f"{r.get('msg_count', 0)}msg"
        print(f"  {i:>2}. {sid_prefix}  {date:<8} {fname:<18} {msgs:<7} {title}")
    print()
    return rows


def parse_grades(text: str, n_results: int) -> dict[int, int]:
    """Parse '1:3 4:2 7:1' into {rank: grade}. Tolerant of extra whitespace
    and trailing commas; rejects out-of-range ranks loudly."""
    out: dict[int, int] = {}
    for token in text.replace(",", " ").split():
        if ":" not in token:
            print(f"  ! ignoring {token!r} (expected rank:grade)")
            continue
        rank_s, grade_s = token.split(":", 1)
        try:
            rank, grade = int(rank_s), int(grade_s)
        except ValueError:
            print(f"  ! ignoring {token!r} (non-integer)")
            continue
        if not (1 <= rank <= n_results):
            print(f"  ! rank {rank} out of range 1..{n_results}, skipping")
            continue
        if not (1 <= grade <= 3):
            print(f"  ! grade {grade} out of range 1..3, skipping")
            continue
        out[rank] = grade
    return out


def resolve_extra_sid(conn, prefix: str) -> str | None:
    rows = conn.execute(
        "SELECT sid FROM sessions WHERE sid LIKE ? || '%' LIMIT 5",
        (prefix,),
    ).fetchall()
    if not rows:
        print(f"  ! no session with prefix {prefix!r}")
        return None
    if len(rows) > 1:
        print(f"  ! prefix {prefix!r} is ambiguous ({len(rows)} matches)")
        return None
    return rows[0][0]


def label_one(conn, existing_qs: list[dict]) -> dict | None:
    query = input("> query (blank to quit): ").strip()
    if not query:
        return None

    rows = show_top(conn, query)
    if not rows:
        return None

    grades_input = input("> grades (e.g. '1:3 4:2 7:1', blank to skip): ").strip()
    rank_grades = parse_grades(grades_input, len(rows)) if grades_input else {}

    relevant = [
        {
            "sid": rows[rank - 1]["session_id"][:12],
            "grade": grade,
            "from_top20": True,
        }
        for rank, grade in sorted(rank_grades.items())
    ]

    extras = input("> anything missing? (sid prefixes space-separated, blank=skip): ").strip()
    if extras:
        for prefix in extras.split():
            full = resolve_extra_sid(conn, prefix)
            if full:
                grade_s = input(f"    grade for {prefix} (1-3): ").strip()
                try:
                    grade = int(grade_s)
                    if 1 <= grade <= 3:
                        relevant.append({"sid": full[:12], "grade": grade, "from_top20": False})
                except ValueError:
                    print("    ! skipping (non-integer grade)")

    note = input("> note (optional): ").strip()
    cwd = os.environ.get("PWD") or os.getcwd()

    entry = {"q": query, "cwd": cwd, "note": note, "relevant": relevant}

    existing_idx = next((i for i, q in enumerate(existing_qs) if q["q"] == query), None)
    if existing_idx is not None:
        ans = input(f"> query {query!r} already labeled. (r)eplace / (a)ppend / (s)kip? ").strip().lower()
        if ans == "s":
            return None
        if ans == "r":
            existing_qs[existing_idx] = entry
            return "REPLACED"  # signal so caller doesn't double-append
    return entry


def main() -> None:
    path = DEFAULT_PATH
    data = load(path)
    qs = data["queries"]
    print(f"Labeling into {path}")
    print(f"Currently {len(qs)} queries labeled.")
    print()

    conn = fts.open_db()
    fts.reindex(conn)

    while True:
        try:
            entry = label_one(conn, qs)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if entry is None:
            break
        if entry == "REPLACED":
            save(path, data)
            print(f"  ✓ replaced; {len(qs)} queries total")
            continue
        qs.append(entry)
        save(path, data)
        print(f"  ✓ saved; {len(qs)} queries total")
        print()

    print(f"Done. {len(qs)} queries in {path}.")


if __name__ == "__main__":
    main()

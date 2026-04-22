"""Keyword-based quick resume. Non-interactive selection when possible."""

from __future__ import annotations

import json
import os
import sys

from .core import (
    canonicalize_path,
    display_cwd,
    extract_user_text,
    format_date,
    get_session_info,
    list_session_files,
)


def _find_by_id(session_id: str) -> dict | None:
    """Find a session by its exact ID. Scans only the first 50 lines per file
    to stay fast across thousands of sessions.
    """
    for filepath in list_session_files():
        try:
            with open(filepath) as f:
                for i, line in enumerate(f):
                    if i > 50:
                        break
                    try:
                        data = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    if data.get("sessionId") == session_id:
                        return get_session_info(filepath)
        except Exception:
            continue
    return None


def _search(keywords: list[str], cwd_filter: str | None = None) -> list[dict]:
    keywords_lower = [k.lower() for k in keywords]

    results: list[dict] = []
    seen_ids: set[str] = set()
    for filepath in list_session_files():
        try:
            user_text = extract_user_text(filepath)
        except Exception:
            continue

        if not all(kw in user_text for kw in keywords_lower):
            continue

        info = get_session_info(filepath)
        if not info or not info["first_msg"]:
            continue
        info["cwd"] = canonicalize_path(info.get("cwd"))

        if cwd_filter and not (info.get("cwd") or "").startswith(cwd_filter):
            continue

        sid = info.get("session_id")
        if sid:
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
        results.append(info)

    results.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return results


def _recent(count: int = 10, cwd_filter: str | None = None) -> list[dict]:
    files = list_session_files()
    files.sort(key=os.path.getmtime, reverse=True)

    results: list[dict] = []
    seen_ids: set[str] = set()
    for filepath in files[: count * 4]:
        info = get_session_info(filepath)
        if not info or not info["first_msg"]:
            continue
        info["cwd"] = canonicalize_path(info.get("cwd"))

        if cwd_filter and not (info.get("cwd") or "").startswith(cwd_filter):
            continue

        sid = info.get("session_id")
        if sid:
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
        results.append(info)
        if len(results) >= count:
            break

    results.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return results


def _pick(results: list[dict]) -> dict:
    if not results:
        print("No sessions found.")
        sys.exit(1)

    if len(results) == 1:
        r = results[0]
        print(
            f"Found 1 session: [{format_date(r['timestamp'])}] {r['first_msg']}"
        )
        print(f"  Folder: {display_cwd(r['cwd'])}")
        print()
        return r

    print(f"Found {len(results)} session(s):\n")
    for i, r in enumerate(results, 1):
        date = format_date(r["timestamp"])
        folder = display_cwd(r["cwd"])
        name = f' "{r["name"]}"' if r.get("name") else ""
        print(f"  {i}. [{date}]{name} ({r['msg_count']} msgs)")
        print(f"     {folder}")
        print(f"     {r['first_msg']}")
        print()

    while True:
        try:
            choice = input(
                f"Pick a session (1-{len(results)}), or q to quit: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if choice.lower() == "q":
            sys.exit(0)

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(results):
                return results[idx]
        except ValueError:
            pass
        print(f"  Please enter 1-{len(results)}")


def _resume(session: dict, extra_flags: list[str] | None = None) -> None:
    cwd = session.get("cwd")
    sid = session.get("session_id")

    if not cwd or not os.path.isdir(cwd):
        print(f"Error: original folder no longer exists: {cwd}", file=sys.stderr)
        print(f"You can try: claude --resume {sid}", file=sys.stderr)
        sys.exit(1)

    cmd = ["claude", "--resume", sid] + (extra_flags or [])
    print(f"Resuming in {display_cwd(cwd)}...")
    os.chdir(cwd)
    os.execvp("claude", cmd)


def _is_session_id(s: str) -> bool:
    """UUID format check (8-4-4-4-12)."""
    parts = s.split("-")
    if len(parts) != 5:
        return False
    if [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    return all(c in "0123456789abcdef" for p in parts for c in p.lower())


def _print_usage() -> None:
    print(
        "Usage:\n"
        "  claude-resume <session-id>          Resume by exact session ID\n"
        "  claude-resume <keyword> [keyword2]  Search, pick one, resume\n"
        "  claude-resume --last [N]            Pick from N most recent (default 10)\n"
        "  claude-resume --yolo <...>          Resume with --dangerously-skip-permissions\n"
        "  claude-resume --here <keyword>      Restrict to sessions from current dir\n"
        "  claude-resume <...> -- <flags>      Pass remaining flags through to claude\n"
        "\n"
        "Examples:\n"
        "  claude-resume 95bc9af0-bc05-4940-bf1b-20777bc5c64c\n"
        "  claude-resume rummy\n"
        "  claude-resume --yolo aditi\n"
        '  claude-resume "modelo 720"\n'
        "  claude-resume --last\n"
        "  claude-resume aditi -- --model sonnet"
    )


def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        _print_usage()
        sys.exit(0 if args else 1)

    extra_flags: list[str] = []
    if "--" in args:
        split_idx = args.index("--")
        extra_flags = args[split_idx + 1 :]
        args = args[:split_idx]

    if "--yolo" in args:
        args.remove("--yolo")
        extra_flags.append("--dangerously-skip-permissions")

    cwd_filter: str | None = None
    if "--here" in args:
        args.remove("--here")
        cwd_filter = os.getcwd()

    if not args and not extra_flags:
        print("Error: no session ID or keywords provided.", file=sys.stderr)
        sys.exit(2)

    if args and args[0] == "--last":
        count = int(args[1]) if len(args) > 1 and args[1].isdigit() else 10
        _resume(_pick(_recent(count, cwd_filter)), extra_flags)
        return

    if len(args) == 1 and _is_session_id(args[0]):
        info = _find_by_id(args[0])
        if info:
            _resume(info, extra_flags)
            return
        print(f"Session not found: {args[0]}", file=sys.stderr)
        sys.exit(1)

    _resume(_pick(_search(args, cwd_filter)), extra_flags)


if __name__ == "__main__":
    main()

"""Interactive session browser. SQLite FTS5 search + preview pane over fzf.

fzf is used purely as a picker (--disabled). All query matching is done by
SQLite FTS5 via a keystroke-driven reload binding. This gives us proper
token-level search with phrase queries, instead of fzf's character-level
fuzzy match (which floods short queries like "sca2" with false positives).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from . import fts
from .core import (
    SESSIONS_DIR,
    display_cwd,
    folder_name,
    format_date,
)

DEFAULT_LIMIT = 100


def _folder_prefixes() -> tuple[str, ...]:
    """Optional prefix list for shortening folder display.

    Example: if CLAUDE_BROWSE_FOLDER_PREFIXES="monorepo/apps/:monorepo/lib/",
    a cwd of ~/monorepo/apps/checkout shows as "checkout" not "monorepo".
    """
    raw = os.environ.get("CLAUDE_BROWSE_FOLDER_PREFIXES", "")
    return tuple(p.strip() for p in raw.split(":") if p.strip())


def format_row(
    info: dict, query: str = "", prefixes: tuple[str, ...] = ()
) -> str:
    """Render one session as a fzf row line.

    Layout: `{date} {fname} {msgs} {title}{suffix}  ###{sid}###{cwd}`. The
    suffix is FTS5's matched-context snippet when a query is active, or a
    topic-drift hint (latest user message) when the title looks stale.

    The trailing `###{sid}###{cwd}` fields are hidden from display via
    fzf's --with-nth=1 but remain on the line for selection-time parsing.
    """
    date = format_date(info.get("last_timestamp") or info.get("timestamp"))
    cwd = info.get("cwd")
    fname = folder_name(cwd, prefixes)
    msgs = f"{info.get('msg_count', 0)}msg"
    title = ((info.get("name") or info.get("first_msg") or "")[:60]).replace(
        "\n", " "
    )
    sid = info.get("session_id") or "?"
    ffolder = display_cwd(cwd)

    suffix = ""
    if query.strip() and info.get("context"):
        # FTS5 snippet: \x01 wraps matched terms, \x02 ends the wrap. Render
        # them as bold yellow (\033[1;33m / \033[0m) inside dim grey text so
        # the matched terms pop out of the otherwise quiet snippet.
        snippet = (
            info["context"]
            .replace("\x01", "\033[0m\033[1;33m")
            .replace("\x02", "\033[0m\033[2m")
        )
        suffix = f"  \033[2m→ {snippet}\033[0m"
    else:
        last = (info.get("last_msg") or "").strip()
        title_words = {w for w in title.lower().split() if len(w) >= 4}
        last_words = {w for w in last.lower().split() if len(w) >= 4}
        if last and last_words and len(title_words & last_words) <= 1:
            suffix = f"  \033[2m→ {last[:70]}\033[0m"

    return f"{date:<8} {fname:<15} {msgs:<7} {title}{suffix}  ###{sid}###{ffolder}"


def _write_preview_script(
    script_path: str, db_path: str, package_dir: str
) -> None:
    """Write a helper script fzf calls to render session previews.

    The script looks up the session's path in the SQLite index (so it works
    for any session the FTS search surfaces, not just the initial set), and
    when fzf passes a non-empty query as argv[2] it highlights occurrences
    of each query term in the printed messages.
    """
    script = f"""#!/usr/bin/env python3
import sys
sys.path.insert(0, {package_dir!r})

import json
import os
import sqlite3

from claude_browse.core import extract_query_terms, highlight_terms

DB_PATH = {db_path!r}
MAX_PREVIEW = 20


def _lookup_path(session_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT path FROM sessions WHERE sid = ?", (session_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_preview(session_id, query=""):
    path = _lookup_path(session_id)
    if not path or not os.path.exists(path):
        print("Session file not found.")
        return

    all_messages = []
    msg_num = 0
    cwd = None
    timestamp = None
    last_timestamp = None
    name = None
    total_user = 0

    with open(path, "r") as f:
        for line in f:
            try:
                data = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            msg = data.get("message", data)
            msg_type = data.get("type", "")

            # Read modern title events (custom + ai), with summary fallback
            # so older sessions still show their name in the preview header.
            if msg_type == "custom-title" and data.get("customTitle"):
                name = data.get("customTitle")
            elif msg_type == "ai-title" and data.get("aiTitle") and not name:
                name = data.get("aiTitle")
            elif msg_type == "summary" and data.get("sessionName") and not name:
                name = data.get("sessionName")
            if not cwd and data.get("cwd"):
                cwd = data.get("cwd")
            if data.get("timestamp"):
                if not timestamp:
                    timestamp = data.get("timestamp")
                last_timestamp = data.get("timestamp")

            if msg.get("role") == "user":
                msg_num += 1
                total_user += 1
                content = msg.get("content", "")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("text"):
                            parts.append(c["text"])
                        elif isinstance(c, dict) and c.get("type") == "image":
                            parts.append("[image]")
                    text = " ".join(parts)

                text = text.replace("\\n", " ").strip()
                if text.startswith("<local-command") or text.startswith("<command"):
                    continue
                if len(text) > 3:
                    wrapped = text[:140]
                    all_messages.append((msg_num, wrapped))

    terms = extract_query_terms(query)

    def hl(s):
        return highlight_terms(s, terms) if terms else s

    if name:
        print(f"Session: {{hl(name)}}")
    if cwd:
        home = os.path.expanduser("~")
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]
        print(f"Folder:  {{cwd}}")
    if timestamp:
        print(f"Started: {{timestamp[:19].replace('T', ' ')}}")
    if last_timestamp and last_timestamp != timestamp:
        print(f"Last activity: {{last_timestamp[:19].replace('T', ' ')}}")
    print(f"Total user messages: {{total_user}}")
    print()

    # If a query is active and at least one of the latest MAX_PREVIEW user
    # messages contains a match, show those (highlighted). If a query is
    # active but no match landed in the recent window, prefer matched
    # messages from earlier in the conversation so the user actually sees
    # *why* this session matched.
    recent = all_messages[-MAX_PREVIEW:]
    if terms and not any(
        any(t.lower() in m.lower() for t in terms) for _, m in recent
    ):
        matched = [
            (n, m) for n, m in all_messages
            if any(t.lower() in m.lower() for t in terms)
        ]
        if matched:
            recent = matched[-MAX_PREVIEW:]
    recent.reverse()

    label = "Messages (matches first):" if terms else "Messages (latest first):"
    print(label)
    print()
    for num, text in recent:
        print(f"  {{num}}. {{hl(text)}}")


if __name__ == "__main__":
    line = sys.argv[1] if len(sys.argv) > 1 else ""
    query = sys.argv[2] if len(sys.argv) > 2 else ""
    if "###" in line:
        parts = line.split("###")
        sid = parts[1].strip() if len(parts) >= 2 else ""
        if sid:
            get_preview(sid, query)
"""

    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)


def _write_search_script(
    script_path: str,
    db_path: str,
    package_dir: str,
    cwd_filter: str | None,
    limit: int,
) -> None:
    """Write the keystroke-driven search helper invoked by fzf change:reload.

    fzf passes the current query string as argv[1] (already shell-quoted).
    The script runs an FTS5 query and prints one row per match, formatted
    identically to the initial input fzf was started with.
    """
    script = f"""#!/usr/bin/env python3
import sys
sys.path.insert(0, {package_dir!r})

from claude_browse import fts
from claude_browse.browse import format_row

DB_PATH = {db_path!r}
CWD_FILTER = {cwd_filter!r}
LIMIT = {limit}

q = sys.argv[1] if len(sys.argv) > 1 else ""
conn = fts.open_db(DB_PATH)
if q.strip():
    results = fts.search(conn, q, limit=LIMIT)
else:
    results = fts.list_recent(conn, limit=LIMIT)

if CWD_FILTER:
    results = [r for r in results if (r.get("cwd") or "").startswith(CWD_FILTER)]

for r in results:
    print(format_row(r, q))
"""
    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)


def _check_fzf() -> None:
    if shutil.which("fzf"):
        return
    print("Error: fzf is required but not installed.", file=sys.stderr)
    print(file=sys.stderr)
    print("Install it with:", file=sys.stderr)
    if sys.platform == "darwin":
        print("  brew install fzf", file=sys.stderr)
    elif sys.platform.startswith("linux"):
        print("  apt install fzf        # Debian / Ubuntu", file=sys.stderr)
        print("  dnf install fzf        # Fedora / RHEL", file=sys.stderr)
        print("  pacman -S fzf          # Arch", file=sys.stderr)
    else:
        print("  https://github.com/junegunn/fzf#installation", file=sys.stderr)
    sys.exit(1)


def _print_usage() -> None:
    print(
        "Usage: claude-browse [options]\n"
        "\n"
        "Options:\n"
        "  --all                 Include every session, not just the most recent 100\n"
        "  --here                Only sessions started in the current directory\n"
        "  -h, --help            Show this help\n"
        "\n"
        "Search syntax (typed inside the picker):\n"
        "  runna                 Sessions containing the token 'runna'\n"
        "  runna sca2            Sessions containing both tokens (any order)\n"
        '  "runna sca2"          Sessions where the two words appear adjacent\n'
        "  runna*                Prefix match: runna, runnathon, runna2026, ...\n"
        "\n"
        "Keys while browsing:\n"
        "  Enter                 Resume the selected session (yolo)\n"
        "  Ctrl-S                Resume in safe mode (prompt for permissions)\n"
        "  Shift-Up / Shift-Down Scroll the preview pane\n"
        "  Esc                   Quit\n"
        "\n"
        "Environment:\n"
        "  CLAUDE_BROWSE_PATH_ALIASES      src=dst[:src2=dst2...] custom cwd aliases\n"
        "  CLAUDE_BROWSE_FOLDER_PREFIXES   colon-separated prefixes for short folder names"
    )


def main() -> None:
    args = sys.argv[1:]

    if "-h" in args or "--help" in args:
        _print_usage()
        return

    show_all = "--all" in args
    if show_all:
        args.remove("--all")

    cwd_filter: str | None = None
    if "--here" in args:
        args.remove("--here")
        cwd_filter = os.getcwd()

    # --no-canonicalize is a legacy flag; canonicalization now happens at
    # index time, not at display time. Accept and ignore for compat.
    if "--no-canonicalize" in args:
        args.remove("--no-canonicalize")

    if args:
        print(f"Unknown argument: {args[0]}", file=sys.stderr)
        _print_usage()
        sys.exit(2)

    _check_fzf()

    if not os.path.isdir(SESSIONS_DIR):
        print(f"No Claude Code sessions found — {SESSIONS_DIR} doesn't exist.")
        print("Run `claude` at least once to create it.")
        sys.exit(1)

    # Build / refresh the FTS index. First run on a populated ~/.claude is
    # several seconds; steady-state is a stat() per file (~tens of ms).
    conn = fts.open_db()
    total_pre = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if total_pre == 0:
        print("Indexing sessions for the first time...", file=sys.stderr)
    added, updated, removed = fts.reindex(conn)
    if added + updated + removed > 0 and total_pre == 0:
        print(
            f"  indexed {added} sessions",
            file=sys.stderr,
        )
    conn.close()

    prefixes = _folder_prefixes()
    limit = 999 if show_all else DEFAULT_LIMIT

    # Initial display: recent sessions, no query active.
    conn = fts.open_db()
    initial = fts.list_recent(conn, limit=limit)
    if cwd_filter:
        initial = [r for r in initial if (r.get("cwd") or "").startswith(cwd_filter)]
    conn.close()

    if not initial:
        print("No sessions found.")
        sys.exit(1)

    initial_lines = [format_row(r, "", prefixes) for r in initial]

    # Both helper scripts get the package directory baked in so they import
    # claude_browse correctly whether we were started from a pip entry point
    # or the direct-script shim.
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    preview_script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="claude_browse_preview_"
    )
    preview_script.close()
    _write_preview_script(preview_script.name, fts.DB_PATH, package_dir)

    search_script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="claude_browse_search_"
    )
    search_script.close()
    _write_search_script(
        search_script.name, fts.DB_PATH, package_dir, cwd_filter, limit
    )

    try:
        fzf_cmd = [
            "fzf",
            "--ansi",
            "--no-sort",
            "--reverse",
            "--height=90%",
            "--border=rounded",
            "--prompt=Sessions > ",
            "--header=Enter: resume (yolo) | Ctrl-S: resume (safe) | Esc: quit | Shift-Up/Down: scroll preview",
            "--header-first",
            "--delimiter=###",
            "--with-nth=1",
            # fzf is a picker only; SQLite FTS5 does the matching. Each
            # keystroke re-runs the search script, which prints fresh rows.
            "--disabled",
            f"--bind=change:reload(python3 {search_script.name} {{q}})",
            f"--preview=python3 {preview_script.name} {{}} {{q}}",
            "--preview-window=right:45%:wrap",
            "--bind=shift-up:preview-up,shift-down:preview-down",
            "--bind=ctrl-s:print(SAFE:)+accept",
        ]

        result = subprocess.run(
            fzf_cmd,
            input="\n".join(initial_lines),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            sys.exit(0)

        output = result.stdout.strip()
        if not output or "###" not in output:
            sys.exit(0)

        # Default is yolo. Ctrl-S prepends "SAFE:" to opt into safe mode.
        yolo = True
        if output.startswith("SAFE:"):
            yolo = False
            output = output[5:]

        output_lines = output.strip().split("\n")
        if len(output_lines) > 1:
            if "SAFE:" in output_lines[0] and "###" not in output_lines[0]:
                yolo = False
                output = output_lines[-1]
            else:
                output = output_lines[-1]

        parts = output.split("###")
        session_id = parts[1].strip() if len(parts) >= 2 else ""

        conn = fts.open_db()
        session = fts.get_by_sid(conn, session_id)
        conn.close()
        if not session:
            print(f"Session not found: {session_id}", file=sys.stderr)
            sys.exit(1)

        cwd = session.get("cwd")
        if not cwd or not os.path.isdir(cwd):
            print(f"Original folder no longer exists: {cwd}", file=sys.stderr)
            print(f"Try: claude --resume {session_id}", file=sys.stderr)
            sys.exit(1)

        cmd = ["claude", "--resume", session_id]
        if yolo:
            cmd.append("--dangerously-skip-permissions")

        mode = " (yolo)" if yolo else ""
        print(f"Resuming{mode} in {folder_name(cwd, prefixes)}...")
        os.chdir(cwd)
        os.execvp("claude", cmd)

    finally:
        for path in (preview_script.name, search_script.name):
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()

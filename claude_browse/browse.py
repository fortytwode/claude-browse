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
    CODEX_STATE_DB,
    SESSIONS_DIR,
    display_cwd,
    folder_name,
    format_date,
    provider_display_name,
    write_import_file,
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

    Layout:
      `{date} {provider} {fname} {msgs} {title}{suffix}  ###{sid}###{cwd}###{provider}`

    The suffix is FTS5's matched-context snippet when a query is active, or a
    topic-drift hint (latest user message) when the title looks stale.
    """
    date = format_date(info.get("last_timestamp") or info.get("timestamp"))
    provider = (info.get("provider") or "claude").lower()
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
        # them as bold yellow inside dim grey text so matched terms pop.
        snippet = (
            info["context"]
            .replace("\x01", "\033[0m\033[1;33m")
            .replace("\x02", "\033[0m\033[2m")
            .replace("\n", " ")
            .replace("\r", " ")
            .replace("\t", " ")
        )
        suffix = f"  \033[2m→ {snippet}\033[0m"
    else:
        last = (
            (info.get("last_msg") or "")
            .strip()
            .replace("\n", " ")
            .replace("\r", " ")
            .replace("\t", " ")
        )
        title_words = {w for w in title.lower().split() if len(w) >= 4}
        last_words = {w for w in last.lower().split() if len(w) >= 4}
        if last and last_words and len(title_words & last_words) <= 1:
            suffix = f"  \033[2m→ {last[:70]}\033[0m"

    return (
        f"{date:<8} {provider:<6} {fname:<15} {msgs:<7} {title}{suffix}  "
        f"###{sid}###{ffolder}###{provider}"
    )


def _write_preview_script(
    script_path: str, db_path: str, package_dir: str
) -> None:
    """Write a helper script fzf calls to render session previews."""
    script = f"""#!/usr/bin/env python3
import os
import sqlite3
import sys

sys.path.insert(0, {package_dir!r})

from claude_browse.core import (
    extract_query_terms,
    get_preview_messages,
    highlight_terms,
    provider_display_name,
)

DB_PATH = {db_path!r}
MAX_PREVIEW = 20


def _lookup_session(session_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            '''
            SELECT provider, path, cwd, timestamp, last_timestamp, title
            FROM sessions
            WHERE sid = ?
            ''',
            (session_id,),
        ).fetchone()
    finally:
        conn.close()


def get_preview(session_id, query=""):
    session = _lookup_session(session_id)
    if not session:
        print("Session not found.")
        return

    provider, path, cwd, timestamp, last_timestamp, name = session
    all_messages = get_preview_messages(provider, path, session_id)
    total_user = len(all_messages)
    terms = extract_query_terms(query)

    def hl(text):
        return highlight_terms(text, terms) if terms else text

    print(f"Source:  {{provider_display_name(provider)}}")
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

    recent = all_messages[-MAX_PREVIEW:]
    if terms and not any(
        any(term.lower() in msg.lower() for term in terms) for _, msg in recent
    ):
        matched = [
            (n, msg) for n, msg in all_messages
            if any(term.lower() in msg.lower() for term in terms)
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
    """Write the keystroke-driven search helper invoked by fzf change:reload."""
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
    # ranker_v1: multi-column BM25 + exp-decay recency. See fts.search_ranked.
    # Set CLAUDE_BROWSE_RANKER=current to fall back to recency-only.
    import os as _os
    if _os.environ.get("CLAUDE_BROWSE_RANKER") == "current":
        results = fts.search(conn, q, limit=LIMIT)
    else:
        results = fts.search_ranked(conn, q, limit=LIMIT)
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
        "  Enter                 Native resume in the source app (yolo)\n"
        "  Ctrl-S                Native resume in the source app (safe)\n"
        "  Ctrl-X                Continue the selected thread in the other app\n"
        "  Shift-Up / Shift-Down Scroll the preview pane\n"
        "  Esc                   Quit\n"
        "\n"
        "Environment:\n"
        "  CLAUDE_BROWSE_PATH_ALIASES      src=dst[:src2=dst2...] custom cwd aliases\n"
        "  CLAUDE_BROWSE_FOLDER_PREFIXES   colon-separated prefixes for short folder names"
    )


def _native_resume(
    session: dict,
    provider: str,
    session_id: str,
    cwd: str,
    prefixes: tuple[str, ...],
    yolo: bool,
) -> None:
    if provider == "codex":
        cmd = ["codex", "resume", session_id]
        if yolo:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        mode = " (yolo)" if yolo else ""
        print(f"Resuming{mode} in CodeX ({folder_name(cwd, prefixes)})...")
        os.execvp("codex", cmd)

    cmd = ["claude", "--resume", session_id]
    if yolo:
        cmd.append("--dangerously-skip-permissions")
    mode = " (yolo)" if yolo else ""
    print(f"Resuming{mode} in {folder_name(cwd, prefixes)}...")
    os.execvp("claude", cmd)


def _continue_in_other_app(
    session: dict,
    provider: str,
    session_id: str,
    cwd: str,
    prefixes: tuple[str, ...],
) -> None:
    target_provider = "claude" if provider == "codex" else "codex"
    target_name = provider_display_name(target_provider)

    if not shutil.which(target_provider):
        print(
            f"Target app not found on PATH: {target_provider}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        import_path = write_import_file(session, target_provider)
    except OSError as exc:
        print(f"Could not write import brief: {exc}", file=sys.stderr)
        sys.exit(1)

    import_dir = os.path.dirname(import_path) or cwd
    prompt = (
        f"Continue the imported {provider_display_name(provider)} session "
        f"context from {import_path}. Treat it as prior conversation state, "
        "read that file first, then continue the work in this directory."
    )
    if target_provider == "claude":
        cmd = ["claude", "--add-dir", import_dir, prompt]
    else:
        cmd = ["codex", "--add-dir", import_dir, prompt]
    print(f"Continuing in {target_name} from {folder_name(cwd, prefixes)}...")
    os.execvp(target_provider, cmd)


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

    if not os.path.isdir(SESSIONS_DIR) and not os.path.exists(CODEX_STATE_DB):
        print("No local Claude Code or CodeX sessions found.")
        print("Run `claude` or `codex` at least once to create session history.")
        sys.exit(1)

    conn = fts.open_db()
    total_pre = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if total_pre == 0:
        print("Indexing sessions for the first time...", file=sys.stderr)
    added, updated, removed = fts.reindex(conn)
    if added + updated + removed > 0 and total_pre == 0:
        print(f"  indexed {added} sessions", file=sys.stderr)
    conn.close()

    prefixes = _folder_prefixes()
    limit = 999 if show_all else DEFAULT_LIMIT

    conn = fts.open_db()
    initial = fts.list_recent(conn, limit=limit)
    if cwd_filter:
        initial = [r for r in initial if (r.get("cwd") or "").startswith(cwd_filter)]
    conn.close()

    if not initial:
        print("No sessions found.")
        sys.exit(1)

    initial_lines = [format_row(r, "", prefixes) for r in initial]
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
            "--header=Enter: native resume (yolo) | Ctrl-S: native resume (safe) | Ctrl-X: continue in the other app | Esc: quit | Shift-Up/Down: scroll preview",
            "--header-first",
            "--delimiter=###",
            "--with-nth=1",
            "--disabled",
            f"--bind=change:reload(python3 {search_script.name} {{q}})",
            f"--preview=python3 {preview_script.name} {{}} {{q}}",
            "--preview-window=right:45%:wrap",
            "--bind=shift-up:preview-up,shift-down:preview-down",
            "--bind=ctrl-s:print(SAFE:)+accept",
            "--bind=ctrl-x:print(XAPP:)+accept",
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

        action = "native"
        yolo = True
        if output.startswith("SAFE:"):
            action = "native"
            yolo = False
            output = output[5:]
        elif output.startswith("XAPP:"):
            action = "handoff"
            yolo = False
            output = output[5:]

        output_lines = output.strip().split("\n")
        if len(output_lines) > 1:
            if "###" in output_lines[-1]:
                output = output_lines[-1]
            else:
                output = next((line for line in reversed(output_lines) if "###" in line), "")

        parts = output.split("###")
        session_id = parts[1].strip() if len(parts) >= 2 else ""
        provider = parts[3].strip() if len(parts) >= 4 else "claude"

        conn = fts.open_db()
        session = fts.get_by_sid(conn, session_id)
        conn.close()
        if not session:
            print(f"Session not found: {session_id}", file=sys.stderr)
            sys.exit(1)

        cwd = session.get("cwd")
        if not cwd or not os.path.isdir(cwd):
            print(f"Original folder no longer exists: {cwd}", file=sys.stderr)
            if provider == "codex":
                print(f"Try: codex resume {session_id}", file=sys.stderr)
            else:
                print(f"Try: claude --resume {session_id}", file=sys.stderr)
            sys.exit(1)

        os.chdir(cwd)
        if action == "handoff":
            _continue_in_other_app(session, provider, session_id, cwd, prefixes)
        _native_resume(session, provider, session_id, cwd, prefixes, yolo)

    finally:
        for path in (preview_script.name, search_script.name):
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()

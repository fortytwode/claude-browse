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
    build_import_markdown,
    display_cwd,
    folder_name,
    format_date,
    provider_display_name,
    write_import_file,
)
from .providers import get_provider, provider_entries, provider_ids
from .work_state import build_work_state, render_restart_card_terminal

DEFAULT_LIMIT = 100
ROW_META_SEP = "\x1f"


def _encode_row_metadata(
    visible: str,
    sid: str,
    cwd: str,
    provider: str,
) -> str:
    return f"{visible}{ROW_META_SEP}{sid}{ROW_META_SEP}{cwd}{ROW_META_SEP}{provider}"


def _split_row_metadata(line: str) -> tuple[str, str, str, str] | None:
    if ROW_META_SEP not in line:
        return None
    parts = line.rsplit(ROW_META_SEP, 3)
    if len(parts) != 4:
        return None
    visible, sid, cwd, provider = parts
    return visible, sid.strip(), cwd.strip(), provider.strip()


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
      `{visible}{ROW_META_SEP}{sid}{ROW_META_SEP}{cwd}{ROW_META_SEP}{provider}`

    The suffix is FTS5's matched-context snippet when a query is active, or a
    topic-drift hint (latest user message) when the title looks stale.
    """
    query_active = bool(query.strip())
    thread_date = format_date(info.get("last_timestamp") or info.get("timestamp"))
    date = thread_date
    if query_active and info.get("match_timestamp"):
        date = format_date(info.get("match_timestamp"))
    provider = (info.get("provider") or "claude").lower()
    cwd = info.get("cwd")
    fname = folder_name(cwd, prefixes)
    title = (
        ((info.get("name") or info.get("first_msg") or "")[:60])
        .replace("\n", " ")
        .replace(ROW_META_SEP, " ")
    )
    sid = info.get("session_id") or "?"
    ffolder = display_cwd(cwd)

    suffix_parts: list[str] = []
    if query_active and info.get("match_timestamp") and date != thread_date:
        suffix_parts.append(f"\033[2mactive {thread_date}\033[0m")

    if query_active and info.get("context"):
        # FTS5 snippet: \x01 wraps matched terms, \x02 ends the wrap. Render
        # them as bold yellow inside dim grey text so matched terms pop.
        snippet = (
            info["context"]
            .replace("\x01", "\033[0m\033[1;33m")
            .replace("\x02", "\033[0m\033[2m")
            .replace("\n", " ")
            .replace("\r", " ")
            .replace("\t", " ")
            .replace(ROW_META_SEP, " ")
        )
        body = f"\033[2m→ {snippet}\033[0m"
        meta_parts: list[str] = []
        if title:
            meta_parts.append(title[:30])
        if query_active and info.get("match_timestamp") and date != thread_date:
            meta_parts.append(f"active {thread_date}")
        meta = (
            f"  \033[2m[{ ' · '.join(meta_parts) }]\033[0m"
            if meta_parts
            else ""
        )
        visible = f"{date:<8} {provider:<6} {fname:<15} {body}{meta}  "
        return _encode_row_metadata(
            visible,
            sid,
            ffolder,
            provider,
        )
    else:
        msgs = f"{info.get('msg_count', 0)}msg"
        last = (
            (info.get("last_msg") or "")
            .strip()
            .replace("\n", " ")
            .replace("\r", " ")
            .replace("\t", " ")
            .replace(ROW_META_SEP, " ")
        )
        title_words = {w for w in title.lower().split() if len(w) >= 4}
        last_words = {w for w in last.lower().split() if len(w) >= 4}
        if last and last_words and len(title_words & last_words) <= 1:
            suffix_parts.append(f"\033[2m→ {last[:70]}\033[0m")

    suffix = f"  {' · '.join(suffix_parts)}" if suffix_parts else ""

    visible = f"{date:<8} {provider:<6} {fname:<15} {msgs:<7} {title}{suffix}  "
    return _encode_row_metadata(
        visible,
        sid,
        ffolder,
        provider,
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
    highlight_terms,
    provider_display_name,
)
from claude_browse.work_state import build_work_state, render_restart_card_terminal

DB_PATH = {db_path!r}
ROW_META_SEP = {ROW_META_SEP!r}


def _lookup_session(session_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            '''
            SELECT provider, path, cwd, timestamp, last_timestamp, title,
                   first_msg, last_msg, msg_count
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

    (
        provider,
        path,
        cwd,
        timestamp,
        last_timestamp,
        name,
        first_msg,
        last_msg,
        msg_count,
    ) = session
    state = build_work_state(
        {{
            "provider": provider,
            "path": path,
            "cwd": cwd,
            "timestamp": timestamp,
            "last_timestamp": last_timestamp,
            "name": name,
            "first_msg": first_msg,
            "last_msg": last_msg,
            "msg_count": msg_count,
            "session_id": session_id,
        }},
        query,
    )
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
    print(f"Messages: {{msg_count or 0}}")
    print()
    print(hl(render_restart_card_terminal(state)))


if __name__ == "__main__":
    line = sys.argv[1] if len(sys.argv) > 1 else ""
    query = sys.argv[2] if len(sys.argv) > 2 else ""
    if ROW_META_SEP in line:
        parts = line.rsplit(ROW_META_SEP, 3)
        sid = parts[1].strip() if len(parts) == 4 else ""
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


def _default_target_provider(argv0: str) -> str:
    program = os.path.basename(argv0 or "claude-browse")
    if program.endswith("-browse"):
        provider = program[:-len("-browse")].lower()
        if provider in provider_ids(target_capable=True):
            return provider
    return "claude"


def _parse_target_provider(args: list[str], argv0: str) -> tuple[str, list[str]]:
    target_provider = _default_target_provider(argv0)
    valid_targets = provider_ids(target_capable=True)
    remaining: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--target":
            if i + 1 >= len(args):
                print("Missing value for --target", file=sys.stderr)
                sys.exit(2)
            target_provider = args[i + 1].strip().lower()
            i += 2
            continue
        if arg.startswith("--target="):
            target_provider = arg.split("=", 1)[1].strip().lower()
            i += 1
            continue
        remaining.append(arg)
        i += 1

    if target_provider not in valid_targets:
        print(
            f"Unknown target provider: {target_provider}. "
            f"Expected one of: {', '.join(valid_targets)}",
            file=sys.stderr,
        )
        sys.exit(2)
    return target_provider, remaining


def _print_usage(argv0: str, target_provider: str) -> None:
    program = os.path.basename(argv0 or "claude-browse")
    target_name = provider_display_name(target_provider)
    valid_targets = "`, `".join(provider_ids(target_capable=True))
    print(
        f"Usage: {program} [options]\n"
        "\n"
        "Options:\n"
        "  --all                 Include every session, not just the most recent 100\n"
        "  --here                Only sessions started in the current directory\n"
        "  --list-providers      Show built-in and external provider availability\n"
        f"  --target PROVIDER     Override launch target (`{valid_targets}`)\n"
        "  -h, --help            Show this help\n"
        "\n"
        "Describe the thread inside the picker:\n"
        "  pokpok brief where we questioned the opportunities\n"
        "  where i was asking nevena about feedback\n"
        '  "runna sca2"          Exact phrase when you know the words already\n'
        "  runna*                Prefix match: runna, runnathon, runna2026, ...\n"
        "  Longer descriptive queries are reduced to the most specific anchors.\n"
        "\n"
        "Keys while browsing:\n"
        f"  Enter                 Resume the selected thread in {target_name} (yolo)\n"
        f"  Ctrl-T                Re-enter the matched topic in a new {target_name} session (yolo)\n"
        f"  Ctrl-S                Open in {target_name} (safe)\n"
        "  Ctrl-Y                Print the suggested next prompt for the selection\n"
        "  Ctrl-B                Print the restart card for the selection\n"
        "  Shift-Up / Shift-Down Scroll the preview pane\n"
        "  Esc                   Quit\n"
        "\n"
        "Environment:\n"
        "  CLAUDE_BROWSE_PATH_ALIASES      src=dst[:src2=dst2...] custom cwd aliases\n"
        "  CLAUDE_BROWSE_FOLDER_PREFIXES   colon-separated prefixes for short folder names"
    )


def _join_with_or(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} or {values[1]}"
    return f"{', '.join(values[:-1])}, or {values[-1]}"


def _require_binary(provider: str) -> None:
    binary = get_provider(provider).binary
    if shutil.which(binary):
        return
    print(f"Target app not found on PATH: {binary}", file=sys.stderr)
    sys.exit(1)


def _native_resume(
    session: dict,
    provider: str,
    session_id: str,
    cwd: str,
    prefixes: tuple[str, ...],
    yolo: bool,
) -> None:
    _require_binary(provider)
    spec = get_provider(provider)
    cmd = spec.native_resume_cmd(session_id, yolo)
    mode = " (yolo)" if yolo else ""
    print(f"Resuming{mode} in {spec.display_name} ({folder_name(cwd, prefixes)})...")
    os.execvp(spec.binary, cmd)


def _continue_in_provider(
    session: dict,
    source_provider: str,
    target_provider: str,
    cwd: str,
    prefixes: tuple[str, ...],
    yolo: bool = True,
    selection_query: str = "",
    *,
    reenter_topic: bool = False,
) -> None:
    target_spec = get_provider(target_provider)
    target_name = target_spec.display_name

    _require_binary(target_provider)

    if target_spec.handoff_via_file:
        try:
            import_path = write_import_file(
                session,
                target_provider,
                selection_query,
                reenter_topic=reenter_topic,
            )
        except OSError as exc:
            print(f"Could not write import brief: {exc}", file=sys.stderr)
            sys.exit(1)

        import_dir = os.path.dirname(import_path) or cwd
        if reenter_topic:
            prompt = (
                f"Continue the imported {provider_display_name(source_provider)} session "
                f"context from {import_path}. Treat it as prior conversation state, "
                "read that file first, use the Reopen Intent section as the reason "
                "this thread was selected, re-enter the earlier matched topic "
                "instead of resuming the full thread from the end, use later turns "
                "only as context about what happened afterward, then continue the "
                "work in this directory."
            )
        else:
            prompt = (
                f"Continue the imported {provider_display_name(source_provider)} session "
                f"context from {import_path}. Treat it as prior conversation state, "
                "read that file first, use the Reopen Intent section as the reason "
                "this thread was selected, prioritize the end-of-thread state and "
                "most recent turns over the original opening prompt, then continue "
                "the work in this directory."
            )
    else:
        import_dir = None
        import_markdown = build_import_markdown(
            session,
            target_provider,
            selection_query,
            reenter_topic=reenter_topic,
        )
        if reenter_topic:
            prompt = (
                f"Continue the imported {provider_display_name(source_provider)} session "
                "context below. Treat it as prior conversation state, use the "
                "Reopen Intent section as the reason this thread was selected, "
                "re-enter the earlier matched topic instead of resuming the "
                "full thread from the end, use later turns only as context "
                "about what happened afterward, then continue the work in "
                f"this directory.\n\n{import_markdown}"
            )
        else:
            prompt = (
                f"Continue the imported {provider_display_name(source_provider)} session "
                "context below. Treat it as prior conversation state, use the "
                "Reopen Intent section as the reason this thread was selected, "
                "prioritize the end-of-thread state and most recent turns over "
                "the original opening prompt, then continue the work in this "
                f"directory.\n\n{import_markdown}"
            )
    cmd = target_spec.handoff_cmd(import_dir, prompt, yolo)
    mode = " (yolo)" if yolo else ""
    action = "Re-entering topic" if reenter_topic else "Continuing"
    print(f"{action}{mode} in {target_name} from {folder_name(cwd, prefixes)}...")
    os.execvp(target_spec.binary, cmd)


def _open_in_target_provider(
    session: dict,
    source_provider: str,
    target_provider: str,
    session_id: str,
    cwd: str,
    prefixes: tuple[str, ...],
    yolo: bool,
    selection_query: str = "",
    *,
    reenter_topic: bool = False,
) -> None:
    if source_provider == target_provider and not reenter_topic:
        _native_resume(session, source_provider, session_id, cwd, prefixes, yolo)
        return
    _continue_in_provider(
        session,
        source_provider,
        target_provider,
        cwd,
        prefixes,
        yolo,
        selection_query,
        reenter_topic=reenter_topic,
    )


def _parse_fzf_output(
    output: str,
    target_provider: str,
) -> tuple[str, str, str, str] | None:
    text = output.strip()
    if not text:
        return None

    lines = [line for line in text.split("\n") if line]
    if not lines:
        return None

    # fzf --print-query prints the query first. Control-key actions add a
    # separate marker line via print(...)+accept before the selected row.
    selection_query = lines[0]
    marker = ""
    row_lines = lines[1:]

    if row_lines and row_lines[0] == "SAFE:":
        marker = row_lines.pop(0)
    elif row_lines and row_lines[0] == "TOPIC:":
        marker = row_lines.pop(0)
    elif row_lines and row_lines[0] == "PROMPT:":
        marker = row_lines.pop(0)
    elif row_lines and row_lines[0] == "BRIEF:":
        marker = row_lines.pop(0)
    elif selection_query.startswith("SAFE:"):
        original = selection_query
        marker = selection_query[:5]
        selection_query = ""
        row_lines = [original[5:]] + row_lines

    selected_target = target_provider
    action = "open_yolo"
    if marker == "SAFE:":
        action = "open_safe"
    elif marker == "TOPIC:":
        action = "reenter_topic"
    elif marker == "PROMPT:":
        action = "print_prompt"
    elif marker == "BRIEF:":
        action = "print_brief"

    row = next((line for line in reversed(row_lines) if ROW_META_SEP in line), "")
    if not row and ROW_META_SEP in selection_query:
        row = selection_query
        selection_query = ""

    if not row or ROW_META_SEP not in row:
        return None
    return row, selected_target, action, selection_query


def _providers_with_local_state() -> list[str]:
    return [
        provider
        for provider in provider_ids(source_capable=True)
        if get_provider(provider).has_local_state()
    ]


def _source_provider_descriptions() -> tuple[str, str]:
    provider_names = [
        get_provider(provider).display_name
        for provider in provider_ids(source_capable=True)
    ]
    provider_binaries = [
        f"`{get_provider(provider).binary}`"
        for provider in provider_ids(source_capable=True)
    ]
    return _join_with_or(provider_names), _join_with_or(provider_binaries)


def _print_provider_list() -> None:
    print(
        f"{'provider':<10} {'type':<8} {'src':<3} {'dst':<3} "
        f"{'avail':<5} {'exp':<3} {'binary':<16} auth"
    )
    for entry in provider_entries():
        spec = entry.spec
        auth = spec.auth_status() or "-"
        print(
            f"{spec.provider_id:<10} {entry.source_type:<8} "
            f"{'yes' if spec.source_capable else 'no':<3} "
            f"{'yes' if spec.target_capable else 'no':<3} "
            f"{'yes' if spec.is_available() else 'no':<5} "
            f"{'yes' if spec.experimental else 'no':<3} "
            f"{spec.binary:<16} {auth}"
        )
        if entry.source_type != "builtin":
            print(f"  origin: {entry.origin}")


def _load_session_by_id(session_id: str) -> dict | None:
    conn = fts.open_db()
    try:
        return fts.get_by_sid(conn, session_id)
    finally:
        conn.close()


def main() -> None:
    target_provider, args = _parse_target_provider(sys.argv[1:], sys.argv[0])

    if "-h" in args or "--help" in args:
        _print_usage(sys.argv[0], target_provider)
        return

    show_all = "--all" in args
    if show_all:
        args.remove("--all")

    list_providers = "--list-providers" in args
    if list_providers:
        args.remove("--list-providers")

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
        _print_usage(sys.argv[0], target_provider)
        sys.exit(2)

    if list_providers:
        _print_provider_list()
        return

    _check_fzf()

    available_providers = _providers_with_local_state()
    if not available_providers:
        provider_names, provider_binaries = _source_provider_descriptions()
        print(f"No local {provider_names} sessions found.")
        print(
            f"Run {provider_binaries} at least once to create session history."
        )
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
        target_name = provider_display_name(target_provider)
        fzf_cmd = [
            "fzf",
            "--ansi",
            "--no-sort",
            "--reverse",
            "--height=90%",
            "--border=rounded",
            "--prompt=Find thread > ",
            (
                "--header="
                'Describe the thread, person, client, or folder you want. '
                f"Enter: resume in {target_name} (yolo) | "
                f"Ctrl-T: re-enter matched topic in {target_name} | "
                f"Ctrl-S: open in {target_name} (safe) | "
                "Ctrl-Y: next prompt | Ctrl-B: restart card | "
                "Esc: quit | Shift-Up/Down: scroll preview"
            ),
            "--header-first",
            f"--delimiter={ROW_META_SEP}",
            "--with-nth=1",
            "--print-query",
            "--disabled",
            f"--bind=change:reload(python3 {search_script.name} {{q}})",
            f"--preview=python3 {preview_script.name} {{}} {{q}}",
            "--preview-window=right:45%:wrap",
            "--bind=shift-up:preview-up,shift-down:preview-down",
            "--bind=ctrl-s:print(SAFE:)+accept",
            "--bind=ctrl-t:print(TOPIC:)+accept",
            "--bind=ctrl-y:print(PROMPT:)+accept",
            "--bind=ctrl-b:print(BRIEF:)+accept",
        ]

        result = subprocess.run(
            fzf_cmd,
            input="\n".join(initial_lines),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            sys.exit(0)

        parsed = _parse_fzf_output(result.stdout, target_provider)
        if not parsed:
            sys.exit(0)

        output, selected_target, action, selection_query = parsed

        row_meta = _split_row_metadata(output)
        if not row_meta:
            print("Could not parse selected session.", file=sys.stderr)
            sys.exit(1)
        _visible, session_id, _cwd_meta, provider = row_meta

        session = _load_session_by_id(session_id)
        if not session:
            print(f"Session not found: {session_id}", file=sys.stderr)
            sys.exit(1)

        state = None
        if action in {"print_prompt", "print_brief", "reenter_topic"}:
            state = build_work_state(session, selection_query)

        if action == "print_prompt":
            print(state["suggested_next_prompt"])
            return

        if action == "print_brief":
            print(render_restart_card_terminal(state))
            return

        if action == "reenter_topic":
            if not selection_query.strip():
                print(
                    "Topic re-entry needs a descriptive query. Search for the earlier topic first, then use Ctrl-T.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not state or not state.get("matched_exchange"):
                print(
                    "No matching exchange found for this query in the selected thread. Refine the query or resume the thread normally.",
                    file=sys.stderr,
                )
                sys.exit(1)

        cwd = session.get("cwd")
        if not cwd or not os.path.isdir(cwd):
            print(f"Original folder no longer exists: {cwd}", file=sys.stderr)
            suggestion = get_provider(provider).native_resume_cmd(session_id, False)
            print(f"Try: {' '.join(suggestion)}", file=sys.stderr)
            sys.exit(1)

        os.chdir(cwd)
        _open_in_target_provider(
            session,
            provider,
            selected_target,
            session_id,
            cwd,
            prefixes,
            action == "open_yolo",
            selection_query,
            reenter_topic=(action == "reenter_topic"),
        )

    finally:
        for path in (preview_script.name, search_script.name):
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()

"""Interactive session browser. Fuzzy search + preview pane over fzf."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from .core import (
    SESSIONS_DIR,
    canonicalize_path,
    display_cwd,
    folder_name,
    format_date,
    get_session_info,
    list_session_files,
)

DEFAULT_LIMIT = 100


def _folder_prefixes() -> tuple[str, ...]:
    """Optional prefix list for shortening folder display.

    Example: if CLAUDE_BROWSE_FOLDER_PREFIXES="monorepo/apps/:monorepo/lib/",
    a cwd of ~/monorepo/apps/checkout shows as "checkout" not "monorepo".
    """
    raw = os.environ.get("CLAUDE_BROWSE_FOLDER_PREFIXES", "")
    return tuple(p.strip() for p in raw.split(":") if p.strip())


def get_sessions(
    limit: int = DEFAULT_LIMIT,
    cwd_filter: str | None = None,
    canonicalize: bool = True,
) -> list[dict]:
    """Collect sessions sorted by recency.

    When `canonicalize=True`, session cwds are normalized (Mac/Linux homes
    mapped to ~), and duplicates by session_id are collapsed to the most
    recent file on disk.
    """
    files = list_session_files()
    files.sort(key=os.path.getmtime, reverse=True)

    results: list[dict] = []
    seen_ids: set[str] = set()

    for filepath in files[: limit * 4]:
        info = get_session_info(filepath)
        if not info or not info["first_msg"]:
            continue

        if canonicalize:
            info["cwd"] = canonicalize_path(info.get("cwd"))

        if cwd_filter and not (info.get("cwd") or "").startswith(cwd_filter):
            continue

        sid = info.get("session_id")
        if canonicalize and sid:
            if sid in seen_ids:
                continue
            seen_ids.add(sid)

        results.append(info)
        if len(results) >= limit:
            break

    results.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return results


def _write_preview_script(sessions: list[dict], script_path: str) -> None:
    """Write a helper script fzf calls to render session previews."""
    mapping = {s["session_id"]: s["path"] for s in sessions if s.get("session_id")}

    script = f"""#!/usr/bin/env python3
import sys
import json
import os

MAPPING = {json.dumps(mapping)}
MAX_PREVIEW = 20

def get_preview(session_id):
    path = MAPPING.get(session_id)
    if not path or not os.path.exists(path):
        print("Session file not found.")
        return

    all_messages = []
    msg_num = 0
    cwd = None
    timestamp = None
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

            if msg_type == "summary" and data.get("sessionName"):
                name = data.get("sessionName")
            if not cwd and data.get("cwd"):
                cwd = data.get("cwd")
            if not timestamp and data.get("timestamp"):
                timestamp = data.get("timestamp")

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

    if name:
        print(f"Session: {{name}}")
    if cwd:
        home = os.path.expanduser("~")
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]
        print(f"Folder:  {{cwd}}")
    if timestamp:
        print(f"Started: {{timestamp[:19].replace('T', ' ')}}")
    print(f"Total user messages: {{total_user}}")
    print()

    recent = all_messages[-MAX_PREVIEW:]
    recent.reverse()

    print("Messages (latest first):")
    print()
    for num, text in recent:
        print(f"  {{num}}. {{text}}")

if __name__ == "__main__":
    line = sys.argv[1] if len(sys.argv) > 1 else ""
    if "###" in line:
        parts = line.split("###")
        sid = parts[1].strip() if len(parts) >= 2 else ""
        if sid:
            get_preview(sid)
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
        "  --no-canonicalize     Don't merge Mac/Linux paths (show raw cwds)\n"
        "  -h, --help            Show this help\n"
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

    canonicalize = True
    if "--no-canonicalize" in args:
        args.remove("--no-canonicalize")
        canonicalize = False

    if args:
        print(f"Unknown argument: {args[0]}", file=sys.stderr)
        _print_usage()
        sys.exit(2)

    _check_fzf()

    if not os.path.isdir(SESSIONS_DIR):
        print(f"No Claude Code sessions found — {SESSIONS_DIR} doesn't exist.")
        print("Run `claude` at least once to create it.")
        sys.exit(1)

    limit = 999 if show_all else DEFAULT_LIMIT
    sessions = get_sessions(
        limit=limit, cwd_filter=cwd_filter, canonicalize=canonicalize
    )
    if not sessions:
        print("No sessions found.")
        sys.exit(1)

    prefixes = _folder_prefixes()

    lines: list[str] = []
    for r in sessions:
        date = format_date(r["timestamp"])
        fname = folder_name(r["cwd"], prefixes)
        msgs = f"{r['msg_count']}msg"
        first_msg = r["first_msg"][:100]
        sid = r["session_id"] or "?"
        ffolder = display_cwd(r["cwd"])
        line = f"{date:<8} {fname:<15} {msgs:<7} {first_msg}  ###{sid}###{ffolder}"
        lines.append(line)

    preview_script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="claude_browse_preview_"
    )
    preview_script.close()
    _write_preview_script(sessions, preview_script.name)

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
            "--nth=1,3",
            f"--preview=python3 {preview_script.name} {{}}",
            "--preview-window=right:45%:wrap",
            "--bind=shift-up:preview-up,shift-down:preview-down",
            "--bind=ctrl-s:print(SAFE:)+accept",
        ]

        result = subprocess.run(
            fzf_cmd,
            input="\n".join(lines),
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

        session = next(
            (s for s in sessions if s["session_id"] == session_id), None
        )
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
        try:
            os.unlink(preview_script.name)
        except OSError:
            pass


if __name__ == "__main__":
    main()

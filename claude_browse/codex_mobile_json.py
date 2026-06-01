"""Mobile-safe Codex runner built on `codex exec --json`.

This intentionally avoids Codex's interactive TUI. The TUI uses fullscreen
redraw/cursor addressing that mobile SSH clients such as Termius do not retain
as normal scrollback. `codex exec --json` gives us structured events that can
be rendered as plain terminal transcript blocks instead.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_REAL_CODEX = "/Users/Shamanth/.npm-global/bin/codex"
DEFAULT_TOOL_OUTPUT_LIMIT = 6000
YOLO_FLAG = "--dangerously-bypass-approvals-and-sandbox"

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
RESET = "\033[0m" if USE_COLOR else ""
BOLD = "\033[1m" if USE_COLOR else ""
DIM = "\033[2m" if USE_COLOR else ""
CYAN = "\033[36m" if USE_COLOR else ""
GREEN = "\033[32m" if USE_COLOR else ""
RED = "\033[31m" if USE_COLOR else ""

_current_session_id: str | None = None
_last_raw_path: Path | None = None
_last_stderr_path: Path | None = None
_last_prompt: str = ""
_last_reply: str = ""
_last_exit_code = 0


def _term_width(default: int = 70) -> int:
    try:
        cols = shutil.get_terminal_size((default, 20)).columns
    except OSError:
        cols = default
    return max(32, min(cols, 100))


def _rule(label: str, color: str = "") -> str:
    color = color or DIM
    width = _term_width()
    text = f" {label} "
    side = max(3, (width - len(text)) // 2)
    left = "━" * side
    right = "━" * (width - side - len(text))
    return f"{color}{BOLD}{left}{text}{right}{RESET}"


def _status(text: str, color: str = "") -> None:
    color = color or DIM
    print(f"{color}[{text}]{RESET}", flush=True)


def _real_codex_binary() -> str:
    configured = os.environ.get("CODEX_REAL_BINARY")
    if configured:
        return configured
    if os.path.exists(DEFAULT_REAL_CODEX):
        return DEFAULT_REAL_CODEX
    return "codex"


def _tool_output_limit() -> int:
    raw = os.environ.get("CODEX_MOBILE_TOOL_OUTPUT_LIMIT", "")
    if not raw:
        return DEFAULT_TOOL_OUTPUT_LIMIT
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TOOL_OUTPUT_LIMIT


def _build_cmd(
    prompt: str,
    last_message_path: Path,
    *,
    session_id: str | None = None,
    real_codex: str | None = None,
    yolo: bool = False,
) -> list[str]:
    binary = real_codex or _real_codex_binary()
    common = [
        "--json",
        "--disable",
        "apps",
        "--disable",
        "enable_mcp_apps",
        "-o",
        str(last_message_path),
    ]
    if yolo:
        common.append(YOLO_FLAG)
    if session_id:
        return [binary, "exec", "resume", *common, session_id, "-"]
    return [binary, "exec", *common, "--color", "never", "-"]


def _print_user(prompt: str) -> None:
    print()
    print(_rule("USER", CYAN), flush=True)
    print(prompt.rstrip(), flush=True)


def _print_assistant(text: str) -> None:
    print()
    print(_rule("ASSISTANT", GREEN), flush=True)
    print(text.rstrip(), flush=True)


def _truncate_middle(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    keep = max(0, limit - 80)
    return text[:keep].rstrip() + "\n[tool output truncated; use :full for raw event log]", True


def _render_command_started(item: dict[str, Any]) -> None:
    command = str(item.get("command") or "").strip()
    print()
    print(_rule("TOOL", DIM), flush=True)
    print(f"{DIM}$ {command}{RESET}", flush=True)


def _render_command_completed(item: dict[str, Any]) -> None:
    status = str(item.get("status") or "")
    exit_code = item.get("exit_code")
    output = str(item.get("aggregated_output") or "")
    color = RED if status == "failed" or exit_code not in (0, None) else DIM
    print(f"{color}[tool {status or 'done'} · exit {exit_code}]{RESET}", flush=True)
    if output:
        rendered, _truncated = _truncate_middle(output.rstrip(), _tool_output_limit())
        print(rendered, flush=True)


def _render_event(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    item = event.get("item") or {}

    if event_type == "thread.started":
        thread_id = str(event.get("thread_id") or "")
        if thread_id:
            _status(f"thread {thread_id}")
        return thread_id or None

    if event_type == "turn.started":
        _status("turn started")
        return None

    if event_type == "item.started" and item.get("type") == "command_execution":
        _render_command_started(item)
        return None

    if event_type == "item.completed" and item.get("type") == "command_execution":
        _render_command_completed(item)
        return None

    if event_type == "item.completed" and item.get("type") == "agent_message":
        text = str(item.get("text") or "")
        _print_assistant(text)
        global _last_reply
        _last_reply = text
        return None

    if event_type == "turn.completed":
        usage = event.get("usage") or {}
        pieces = []
        if "output_tokens" in usage:
            pieces.append(f"output={usage.get('output_tokens')}")
        if "reasoning_output_tokens" in usage:
            pieces.append(f"reasoning={usage.get('reasoning_output_tokens')}")
        suffix = f" · {' · '.join(pieces)}" if pieces else ""
        _status(f"done{suffix}")
        return None

    if event_type:
        _status(f"event {event_type}")
    return None


def run_turn(
    prompt: str,
    *,
    session_id: str | None = None,
    show_user_block: bool = True,
    yolo: bool = False,
) -> str | None:
    """Run one Codex turn and render JSON events as normal stdout."""
    global _current_session_id, _last_exit_code, _last_prompt, _last_raw_path, _last_stderr_path

    prompt = prompt.strip()
    if not prompt:
        return session_id

    _last_prompt = prompt
    last_message_path = Path(tempfile.mktemp(prefix="codex-mobile-json-", suffix=".last"))
    raw_path = Path(tempfile.mktemp(prefix="codex-mobile-json-", suffix=".jsonl"))
    stderr_path = Path(tempfile.mktemp(prefix="codex-mobile-json-", suffix=".stderr"))
    _last_raw_path = raw_path
    _last_stderr_path = stderr_path

    cmd = _build_cmd(prompt, last_message_path, session_id=session_id, yolo=yolo)
    if show_user_block:
        _print_user(prompt)

    start = time.time()
    try:
        raw_file = raw_path.open("w", encoding="utf-8")
        stderr_file = stderr_path.open("w", encoding="utf-8")
    except OSError as exc:
        _status(f"could not create temp files: {exc}", RED)
        return session_id

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raw_file.close()
        stderr_file.close()
        _status(f"could not launch Codex: {exc}", RED)
        return session_id

    assert proc.stdin is not None
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError as exc:
        _status(f"could not send prompt to Codex: {exc}", RED)

    seen_session_id = session_id
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            raw_file.write(line)
            raw_file.flush()
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                print(stripped, flush=True)
                continue
            new_session_id = _render_event(event)
            if new_session_id:
                seen_session_id = new_session_id
    except KeyboardInterrupt:
        proc.terminate()
        _status("interrupted; terminating Codex", RED)
    finally:
        raw_file.close()
        stderr_file.close()

    code = proc.wait()
    _last_exit_code = code
    duration = time.time() - start
    stderr_text = _safe_read(stderr_path).strip()
    if code != 0:
        _status(f"codex exited with status {code}", RED)
        if stderr_text:
            print(stderr_text, flush=True)
    elif stderr_text:
        _status("codex stderr", DIM)
        print(stderr_text, flush=True)
    _status(f"elapsed {duration:.1f}s · :history all · :full raw · :q quit", DIM)

    _current_session_id = seen_session_id
    return seen_session_id


def _safe_read(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _page(content: str) -> None:
    if content and not content.endswith("\n"):
        content += "\n"
    less = shutil.which("less")
    if not less:
        print(content, end="", flush=True)
        return
    try:
        subprocess.run([less, "-R", "-F", "-X", "-"], input=content, text=True, check=False)
    except OSError as exc:
        _status(f"could not open less: {exc}", RED)
        print(content, end="", flush=True)


def _latest_session_file() -> Path | None:
    files = glob.glob(str(Path.home() / ".codex" / "sessions" / "**" / "*.jsonl"), recursive=True)
    if not files:
        return None
    return Path(max(files, key=os.path.getmtime))


def _find_session_file(session_id: str | None) -> Path | None:
    if not session_id:
        return _latest_session_file()
    root = Path.home() / ".codex" / "sessions"
    if not root.exists():
        return None
    matches = list(root.rglob(f"*{session_id}*.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _clean_message_content(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("input_text") or item.get("output_text")
                if text:
                    parts.append(str(text))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts).strip()
    return ""


def _extract_transcript_message(obj: dict[str, Any]) -> tuple[str, str] | None:
    if obj.get("type") != "response_item":
        return None
    payload = obj.get("payload") or {}
    if payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in {"user", "assistant"}:
        return None
    text = _clean_message_content(payload.get("content"))
    if not text:
        return None
    if role == "user" and (
        text.startswith("# AGENTS.md instructions") or text.startswith("<environment_context>")
    ):
        return None
    return role, text


def _render_history_text(session_id: str | None) -> str:
    path = _find_session_file(session_id)
    if path is None:
        return "No Codex session file found.\n"

    blocks: list[str] = [f"Session file: {path}\n"]
    previous: tuple[str, str] | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = _extract_transcript_message(obj)
                if message is None:
                    continue
                role, text = message
                fingerprint = (role, re.sub(r"\s+", " ", text)[:500])
                if fingerprint == previous:
                    continue
                previous = fingerprint
                label = "USER" if role == "user" else "ASSISTANT"
                blocks.append(f"\n{'=' * 10} {label} {'=' * 10}\n\n{text.rstrip()}\n")
    except OSError as exc:
        return f"Could not read Codex session file: {exc}\n"
    return "\n".join(blocks).lstrip()


def show_history() -> None:
    _page(f"\n{_rule('SESSION HISTORY', DIM)}\n{_render_history_text(_current_session_id)}")


def show_full() -> None:
    parts = []
    if _last_raw_path:
        parts.append(f"Raw JSONL: {_last_raw_path}\n{_safe_read(_last_raw_path)}")
    if _last_stderr_path:
        stderr = _safe_read(_last_stderr_path)
        if stderr:
            parts.append(f"Stderr: {_last_stderr_path}\n{stderr}")
    if not parts:
        _status("no turn has run yet")
        return
    _page(f"\n{_rule('RAW LAST TURN', DIM)}\n" + "\n\n".join(parts))


def read_prompt() -> str | None:
    try:
        line = input(f"\n{CYAN}{BOLD}you>{RESET} ")
    except EOFError:
        return None
    command = line.strip()
    if command in {":q", ":quit", "quit", "exit"}:
        return None
    if command == ":history":
        show_history()
        return ""
    if command == ":full":
        show_full()
        return ""
    if command == ":paste":
        print(f"{DIM}Paste prompt. End with a single '.' on its own line.{RESET}")
        lines: list[str] = []
        while True:
            try:
                item = input()
            except EOFError:
                break
            if item == ".":
                break
            lines.append(item)
        return "\n".join(lines).strip()
    return line.strip()


def _parse_args(argv: list[str]) -> tuple[str | None, str, bool]:
    parser = argparse.ArgumentParser(
        prog="codex-mobile-json",
        description="Run Codex in mobile-safe JSON transcript mode.",
    )
    parser.add_argument(
        "--yolo",
        action="store_true",
        help="Pass Codex's dangerous no-approval/no-sandbox flag to codex exec.",
    )
    parser.add_argument("args", nargs="*", help="'resume SESSION_ID [PROMPT]' or prompt text")
    ns = parser.parse_args(argv)
    args = list(ns.args)
    if args and args[0] == "resume":
        if len(args) < 2:
            parser.error("resume requires SESSION_ID")
        return args[1], " ".join(args[2:]).strip(), bool(ns.yolo)
    if args and args[0] == "start":
        return None, " ".join(args[1:]).strip(), bool(ns.yolo)
    return None, " ".join(args).strip(), bool(ns.yolo)


def main(argv: list[str] | None = None) -> int:
    global _current_session_id
    session_id, initial_prompt, yolo = _parse_args(list(sys.argv[1:] if argv is None else argv))
    _current_session_id = session_id

    print("═" * _term_width(), flush=True)
    print(f"{BOLD}Codex mobile JSON mode{RESET}", flush=True)
    if session_id:
        print(f"Resuming {DIM}{session_id}{RESET}", flush=True)
    else:
        print("Starting new Codex thread", flush=True)
    print(f"{DIM}:history all · :full raw · :paste multi · :q quit{RESET}", flush=True)
    print("═" * _term_width(), flush=True)

    if initial_prompt:
        session_id = run_turn(initial_prompt, session_id=session_id, yolo=yolo) or session_id
        if not sys.stdin.isatty():
            return _last_exit_code
    elif not sys.stdin.isatty():
        piped_prompt = sys.stdin.read().strip()
        if piped_prompt:
            run_turn(piped_prompt, session_id=session_id, yolo=yolo)
            return _last_exit_code
        return 0

    while True:
        prompt = read_prompt()
        if prompt is None:
            return 0
        if not prompt:
            continue
        session_id = (
            run_turn(prompt, session_id=session_id, show_user_block=False, yolo=yolo)
            or session_id
        )


if __name__ == "__main__":
    raise SystemExit(main())

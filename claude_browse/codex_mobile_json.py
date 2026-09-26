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
import threading
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
_turn_count = 0
_last_elapsed = 0.0
_yolo = False
_fork_from: str | None = None
_SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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
    fork_from: str | None = None,
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
    if fork_from:
        return [binary, "exec", "fork", *common, fork_from, "-"]
    return [binary, "exec", *common, "--color", "never", "-"]


def _print_user(prompt: str) -> None:
    print()
    print(_rule("USER", CYAN), flush=True)
    print(prompt.rstrip(), flush=True)


def _print_assistant(text: str) -> None:
    print()
    print(_rule("ASSISTANT", GREEN), flush=True)
    print(_render_markdown(text.rstrip()), flush=True)


_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_MD_BULLET_RE = re.compile(r"^(\s*)[-*]\s+")


def _render_markdown(text: str) -> str:
    """Light markdown -> ANSI. Untouched when color is off (NO_COLOR / not a TTY)."""
    if not USE_COLOR:
        return text
    out: list[str] = []
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(f"{DIM}{'─' * min(_term_width(), 40)}{RESET}")
            continue
        if in_code:
            out.append(line)
            continue
        heading = _MD_HEADING_RE.match(line)
        if heading:
            out.append(f"{BOLD}{heading.group(1)}{RESET}")
            continue
        line = _MD_BULLET_RE.sub(lambda m: f"{m.group(1)}• ", line)
        line = _MD_CODE_RE.sub(lambda m: f"{DIM}{m.group(1)}{RESET}", line)
        line = _MD_BOLD_RE.sub(lambda m: f"{BOLD}{m.group(1)}{RESET}", line)
        out.append(line)
    return "\n".join(out)


def _short_id(session_id: str | None) -> str:
    # Codex ids are time-ordered, so the first 8 chars collide; keep two groups.
    return session_id[:13] if session_id else "new"


def _display_cwd() -> str:
    cwd = os.getcwd()
    home = str(Path.home())
    if cwd == home or cwd.startswith(home + os.sep):
        return "~" + cwd[len(home):]
    return cwd


def _footer_label(session_id: str | None) -> str:
    return f"◇ thread {_short_id(session_id)}"


def _print_footer(session_id: str | None) -> None:
    yolo = "on" if _yolo else "off"
    print(f"{DIM}{_footer_label(session_id)} · idle · yolo {yolo} · {_display_cwd()}{RESET}", flush=True)


class _Ticker:
    """Live `thinking… Ns` counter. Rewrites one line on a TTY; prints a plain
    line every 30s otherwise."""

    def __init__(self, label: str) -> None:
        self._label = label
        self._start = time.time()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._tty = sys.stdout.isatty()
        self._last_len = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _elapsed(self) -> int:
        return int(time.time() - self._start)

    def _run(self) -> None:
        interval = 1.0 if self._tty else 30.0
        while not self._stop.wait(interval):
            with self._lock:
                if self._tty:
                    plain = f"{self._label} · thinking… {self._elapsed()}s"
                    pad = " " * max(0, self._last_len - len(plain))
                    sys.stdout.write(f"\r{DIM}{plain}{RESET}{pad}")
                    sys.stdout.flush()
                    self._last_len = len(plain)
                else:
                    print(f"{DIM}[thinking… {self._elapsed()}s]{RESET}", flush=True)

    def clear(self) -> None:
        with self._lock:
            if self._last_len:
                sys.stdout.write("\r" + " " * self._last_len + "\r")
                sys.stdout.flush()
                self._last_len = 0

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.clear()


def _truncate_middle(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    keep = max(0, limit - 80)
    return text[:keep].rstrip() + "\n[tool output truncated; use :full for raw event log]", True


# Two-line tool condensing (Claude-Code style). A tool call renders as a
# compact command line plus a one-line result summary (exit + line count). The
# full output is never dumped inline; it is always one `:full` keystroke away
# (the raw JSONL event log is tee'd to disk every turn). Failures stay loud: we
# keep the "tool failed" label and surface a short tail so the error is visible
# without `:full`. Set CODEX_MOBILE_TOOL_VERBOSE=1 to restore the old behavior
# of dumping aggregated output inline.
_FAIL_TAIL_LINES = 8
_BASH_WRAP_RE = re.compile(
    r"^\s*(?:/[\w./-]*/)?(?:bash|sh|zsh)\s+-[A-Za-z]*c\s+(['\"])(.*)\1\s*$",
    re.DOTALL,
)


def _tool_verbose() -> bool:
    return os.environ.get("CODEX_MOBILE_TOOL_VERBOSE", "").strip() not in ("", "0", "false", "False")


def _compact_command(command: Any) -> str:
    """Collapse a tool command to a single readable line.

    Strips the `bash -lc '…'` (or sh/zsh) wrapper Codex adds so the inner
    command shows directly, then flattens newlines and runs of whitespace.
    """
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    command = str(command or "").strip()
    match = _BASH_WRAP_RE.match(command)
    if match:
        command = match.group(2).strip()
    return re.sub(r"\s+", " ", command)


def _one_line(text: str, width: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if width > 1 and len(text) > width:
        return text[: width - 1].rstrip() + "…"
    return text


def _truncate_width(text: str, width: int) -> str:
    text = text.rstrip("\n")
    if width > 1 and len(text) > width:
        return text[: width - 1].rstrip() + "…"
    return text


def _line_count(output: str) -> int:
    output = output.rstrip("\n")
    if not output:
        return 0
    return output.count("\n") + 1


def _render_command_started(item: dict[str, Any]) -> None:
    command = _compact_command(item.get("command"))
    print()
    if _tool_verbose():
        print(_rule("TOOL", DIM), flush=True)
    print(f"{DIM}$ {_one_line(command, _term_width() - 2)}{RESET}", flush=True)


def _render_command_completed(item: dict[str, Any]) -> None:
    status = str(item.get("status") or "")
    exit_code = item.get("exit_code")
    output = str(item.get("aggregated_output") or "")
    failed = status == "failed" or exit_code not in (0, None)
    color = RED if failed else DIM
    n = _line_count(output)

    if _tool_verbose():
        print(f"{color}[tool {status or 'done'} · exit {exit_code}]{RESET}", flush=True)
        if output:
            rendered, _truncated = _truncate_middle(output.rstrip(), _tool_output_limit())
            print(rendered, flush=True)
        return

    code_label = exit_code if exit_code is not None else "?"
    count_label = f" · {n} line{'s' if n != 1 else ''}" if n else ""
    if failed:
        print(f"{color}  ⎿ tool failed · exit {code_label}{count_label}{RESET}", flush=True)
        if output:
            width = _term_width()
            for line in output.rstrip("\n").splitlines()[-_FAIL_TAIL_LINES:]:
                print(f"{RED}  {_truncate_width(line, width)}{RESET}", flush=True)
    else:
        hint = " · :full" if n else ""
        print(f"{color}  ⎿ exit {code_label}{count_label}{hint}{RESET}", flush=True)


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

    if event_type in ("error", "turn.failed"):
        err = event.get("error") if isinstance(event.get("error"), dict) else {}
        message = str(event.get("message") or err.get("message") or "").strip()
        _status(f"{event_type}: {message}" if message else event_type, RED)
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
    global _turn_count, _last_elapsed, _fork_from

    prompt = prompt.strip()
    if not prompt:
        return session_id

    _last_prompt = prompt
    last_message_path = Path(tempfile.mktemp(prefix="codex-mobile-json-", suffix=".last"))
    raw_path = Path(tempfile.mktemp(prefix="codex-mobile-json-", suffix=".jsonl"))
    stderr_path = Path(tempfile.mktemp(prefix="codex-mobile-json-", suffix=".stderr"))
    _last_raw_path = raw_path
    _last_stderr_path = stderr_path

    fork_from = None if session_id else _fork_from
    cmd = _build_cmd(prompt, last_message_path, session_id=session_id, yolo=yolo, fork_from=fork_from)
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
    _fork_from = None
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError as exc:
        _status(f"could not send prompt to Codex: {exc}", RED)

    seen_session_id = session_id
    ticker = _Ticker(_footer_label(session_id or fork_from))
    ticker.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            raw_file.write(line)
            raw_file.flush()
            stripped = line.strip()
            if not stripped:
                continue
            ticker.clear()
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
        ticker.clear()
        _status("interrupted; terminating Codex", RED)
    finally:
        ticker.stop()
        raw_file.close()
        stderr_file.close()

    code = proc.wait()
    _last_exit_code = code
    duration = time.time() - start
    _turn_count += 1
    _last_elapsed = duration
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
    _print_footer(seen_session_id)
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


def _session_id_from_path(path: Path | None) -> str | None:
    if path is None:
        return None
    match = _SESSION_ID_RE.search(path.name)
    return match.group(0) if match else None


def _session_meta(session_id: str | None) -> dict[str, Any]:
    path = _find_session_file(session_id) if session_id else None
    if path is None:
        return {}
    meta: dict[str, Any] = {"_path": str(path)}
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(200):
                line = handle.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload") or {}
                if obj.get("type") == "session_meta":
                    meta["cwd"] = payload.get("cwd")
                    meta["cli_version"] = payload.get("cli_version")
                elif obj.get("type") == "turn_context" and payload.get("model"):
                    meta["model"] = payload.get("model")
                    break
    except OSError:
        pass
    return meta


def _first_user_message(path: Path, limit: int = 400) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(limit):
                line = handle.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = _extract_transcript_message(obj)
                if message and message[0] == "user":
                    return message[1]
    except OSError:
        pass
    return ""


def _age(mtime: float) -> str:
    seconds = max(0, int(time.time() - mtime))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _recent_sessions(limit: int = 10) -> list[tuple[str, str, str]]:
    files = glob.glob(str(Path.home() / ".codex" / "sessions" / "**" / "*.jsonl"), recursive=True)
    files.sort(key=os.path.getmtime, reverse=True)
    rows: list[tuple[str, str, str]] = []
    for file in files[: limit * 4]:
        path = Path(file)
        session_id = _session_id_from_path(path)
        title = _one_line(_first_user_message(path), 60)
        if not session_id or not title:
            continue
        rows.append((session_id, _age(path.stat().st_mtime), title))
        if len(rows) >= limit:
            break
    return rows


def _pick_session() -> str | None:
    """Interactive startup picker. Returns a session id, None for a new thread,
    or "quit"."""
    rows = _recent_sessions()
    if not rows:
        return None
    print(f"{BOLD}Recent Codex sessions{RESET}", flush=True)
    for index, (session_id, age, title) in enumerate(rows, start=1):
        print(f"{index:>2}. {title:<60}  {DIM}{age} · {_short_id(session_id)}{RESET}", flush=True)
    print(f"{DIM} n. new thread   q. quit{RESET}", flush=True)
    while True:
        try:
            choice = input(f"{CYAN}{BOLD}pick>{RESET} ").strip().lower()
        except EOFError:
            return "quit"
        if choice in {"", "n", "new"}:
            return None
        if choice in {"q", "quit", "exit"}:
            return "quit"
        if choice.isdigit() and 1 <= int(choice) <= len(rows):
            return rows[int(choice) - 1][0]
        _status(f"pick 1-{len(rows)}, n or q", RED)


def _help_text() -> str:
    return "\n".join(
        [
            f"{BOLD}Local commands{RESET} (never sent to Codex)",
            "  /status            thread, model, cwd, turns, yolo",
            "  /resume <id>       switch to another Codex thread",
            "  /resume --last     switch to the most recent thread on disk",
            "  /history  :history transcript of the current thread",
            "  /full     :full    raw JSONL + stderr of the last turn",
            "  :paste             multi-line prompt, end with '.'",
            "  /quit  :q          exit",
            f"{DIM}Anything else is sent to Codex as a prompt.{RESET}",
        ]
    )


def show_status() -> None:
    global _current_session_id
    meta = _session_meta(_current_session_id)
    lines = [
        f"{BOLD}thread{RESET}   {_current_session_id or '(none yet: first prompt starts one)'}",
    ]
    if meta.get("_path"):
        lines.append(f"{BOLD}file{RESET}     {meta['_path']}")
    if meta.get("model"):
        lines.append(f"{BOLD}model{RESET}    {meta['model']}")
    lines.append(f"{BOLD}cwd{RESET}      {_display_cwd()}")
    lines.append(f"{BOLD}turns{RESET}    {_turn_count} this run · last {_last_elapsed:.1f}s")
    lines.append(f"{BOLD}yolo{RESET}     {'on' if _yolo else 'off'}")
    print("\n".join(lines), flush=True)


def _handle_local_command(command: str) -> bool:
    global _current_session_id
    parts = command.split()
    name = parts[0].lower()
    if name == "/status":
        show_status()
        return True
    if name == "/help":
        print(_help_text(), flush=True)
        return True
    if name == "/resume":
        if len(parts) < 2:
            _status("usage: /resume <id> | /resume --last", RED)
            return True
        target = parts[1]
        if target == "--last":
            session_id = _session_id_from_path(_latest_session_file())
            if not session_id:
                _status("no Codex session files found", RED)
                return True
        else:
            match = _SESSION_ID_RE.search(target)
            session_id = match.group(0) if match else None
            if not session_id:
                path = _find_session_file(target)
                session_id = _session_id_from_path(path)
            if not session_id:
                _status(f"no session matching {target}", RED)
                return True
        _current_session_id = session_id
        _status(f"now on thread {session_id}", GREEN)
        _print_footer(session_id)
        return True
    return False


def read_prompt() -> str | None:
    try:
        line = input(f"\n{CYAN}{BOLD}you>{RESET} ")
    except EOFError:
        return None
    command = line.strip()
    if command in {":q", ":quit", "quit", "exit", "/quit", "/exit"}:
        return None
    if command in {":history", "/history"}:
        show_history()
        return ""
    if command in {":full", "/full"}:
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
    if command.startswith("/"):
        if _handle_local_command(command):
            return ""
        _status("not a local command; sending to Codex", DIM)
    return line.strip()


def _parse_args(argv: list[str]) -> tuple[str | None, str, bool, str | None]:
    parser = argparse.ArgumentParser(
        prog="codex-mobile-json",
        description="Run Codex in mobile-safe JSON transcript mode.",
    )
    parser.add_argument(
        "--yolo",
        action="store_true",
        help="Pass Codex's dangerous no-approval/no-sandbox flag to codex exec.",
    )
    parser.add_argument(
        "args",
        nargs="*",
        help="'resume SESSION_ID [PROMPT]', 'fork SESSION_ID [PROMPT]', or prompt text; "
        "no args opens a picker of recent sessions",
    )
    ns = parser.parse_args(argv)
    args = list(ns.args)
    if args and args[0] in {"resume", "fork"}:
        if len(args) < 2:
            parser.error(f"{args[0]} requires SESSION_ID")
        if args[0] == "fork":
            return None, " ".join(args[2:]).strip(), bool(ns.yolo), args[1]
        return args[1], " ".join(args[2:]).strip(), bool(ns.yolo), None
    if args and args[0] == "start":
        return None, " ".join(args[1:]).strip(), bool(ns.yolo), None
    return None, " ".join(args).strip(), bool(ns.yolo), None


def main(argv: list[str] | None = None) -> int:
    global _current_session_id, _yolo, _fork_from
    argv = list(sys.argv[1:] if argv is None else argv)
    session_id, initial_prompt, yolo, fork_from = _parse_args(argv)
    _yolo = yolo
    _fork_from = fork_from

    print("═" * _term_width(), flush=True)
    print(f"{BOLD}Codex mobile JSON mode{RESET}", flush=True)
    picker = not session_id and not initial_prompt and not fork_from and sys.stdin.isatty()
    if picker:
        choice = _pick_session()
        if choice == "quit":
            return 0
        session_id = choice
    _current_session_id = session_id
    if session_id:
        print(f"Resuming {DIM}{session_id}{RESET}", flush=True)
    elif fork_from:
        print(f"Forking {DIM}{fork_from}{RESET} into a new thread", flush=True)
    else:
        print("Starting new Codex thread", flush=True)
    print(f"{DIM}/status · /resume · /help · :history all · :full raw · :paste multi · :q quit{RESET}", flush=True)
    print("═" * _term_width(), flush=True)
    _print_footer(session_id)

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
        run_turn(prompt, session_id=_current_session_id, show_user_block=False, yolo=_yolo)


if __name__ == "__main__":
    raise SystemExit(main())

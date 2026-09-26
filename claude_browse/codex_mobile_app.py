"""Phone-friendly Codex client on top of `codex app-server`.

One persistent app-server child per run, JSON-RPC over stdio. Agent text is
streamed and word-wrapped to the terminal width; a small live region at the
bottom is redrawn with relative cursor moves only (no alternate screen, no
absolute addressing), so mobile SSH clients keep normal scrollback.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

from claude_browse import codex_mobile_json as legacy

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
RESET = "\033[0m" if USE_COLOR else ""
BOLD = "\033[1m" if USE_COLOR else ""
DIM = "\033[2m" if USE_COLOR else ""
CYAN = "\033[36m" if USE_COLOR else ""
GREEN = "\033[32m" if USE_COLOR else ""
RED = "\033[31m" if USE_COLOR else ""
CLIENT_VERSION = "2.0.0"
FAIL_TAIL_LINES = 8

_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_MD_BULLET_RE = re.compile(r"^(\s*)[-*]\s+")
_MD_NUM_RE = re.compile(r"^(\s*)(\d+[.)])\s+")
_ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def _width() -> int:
    try:
        cols = shutil.get_terminal_size((70, 24)).columns
    except OSError:
        cols = 70
    return max(24, min(cols, 120)) - 1


def _visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def _clock() -> str:
    return _dt.datetime.now().strftime("%-I:%M %p")


def _short_id(thread_id: str | None) -> str:
    return legacy._short_id(thread_id)


def _display_cwd() -> str:
    return legacy._display_cwd()


def _wrap(line: str, width: int, indent: str = "") -> list[str]:
    if not line.strip():
        return [""]
    wrapped = textwrap.wrap(
        line,
        width=width,
        subsequent_indent=indent,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=False,
        drop_whitespace=True,
    )
    return wrapped or [""]


class _Markdown:
    """Line-at-a-time markdown to ANSI plus word wrap; keeps fence state."""

    def __init__(self) -> None:
        self.in_code = False

    def render(self, line: str, width: int) -> list[str]:
        if line.strip().startswith("```"):
            self.in_code = not self.in_code
            return [f"{DIM}{'─' * min(width, 40)}{RESET}"]
        if self.in_code:
            raw = line.rstrip("\n")
            return [raw[i : i + width] for i in range(0, max(1, len(raw)), width)] or [""]
        heading = _MD_HEADING_RE.match(line)
        if heading:
            return [f"{BOLD}{part}{RESET}" for part in _wrap(heading.group(1), width)]
        indent = ""
        bullet = _MD_BULLET_RE.match(line)
        if bullet:
            line = f"{bullet.group(1)}• " + line[bullet.end() :]
            indent = " " * (len(bullet.group(1)) + 2)
        else:
            number = _MD_NUM_RE.match(line)
            if number:
                indent = " " * (len(number.group(1)) + len(number.group(2)) + 1)
        if line.lstrip().startswith("> "):
            indent = " " * (len(line) - len(line.lstrip()) + 2)
        parts = _wrap(line.rstrip(), width, indent)
        out = []
        for part in parts:
            part = _MD_CODE_RE.sub(lambda m: f"{DIM}{m.group(1)}{RESET}", part)
            part = _MD_BOLD_RE.sub(lambda m: f"{BOLD}{m.group(1)}{RESET}", part)
            out.append(part)
        return out


class _Screen:
    """Permanent transcript above a redrawable live region (TTY only)."""

    def __init__(self) -> None:
        self.tty = sys.stdout.isatty()
        self._live: list[str] = []
        self._lock = threading.RLock()

    def _erase_live(self) -> None:
        if not self._live:
            return
        n = len(self._live)
        sys.stdout.write("\r\033[K")
        for _ in range(n - 1):
            sys.stdout.write("\033[1A\033[K")
        self._live = []

    def print(self, text: str = "") -> None:
        with self._lock:
            live = list(self._live)
            if self.tty:
                self._erase_live()
            sys.stdout.write(text + "\n")
            if self.tty and live:
                self._draw(live)
            sys.stdout.flush()

    def _draw(self, lines: list[str]) -> None:
        width = _width()
        shown = []
        for line in lines:
            if _visible_len(line) >= width:
                line = legacy._truncate_width(_ANSI_RE.sub("", line), width - 1)
            shown.append(line)
        sys.stdout.write("\n".join(shown))
        self._live = shown

    def live(self, lines: list[str]) -> None:
        if not self.tty:
            return
        with self._lock:
            self._erase_live()
            self._draw([line for line in lines if line is not None])
            sys.stdout.flush()

    def clear_live(self) -> None:
        if not self.tty:
            return
        with self._lock:
            self._erase_live()
            sys.stdout.flush()


class _Stream:
    """Accumulates agent deltas, emits wrapped complete lines, keeps the tail."""

    def __init__(self, screen: _Screen, md: _Markdown) -> None:
        self.screen = screen
        self.md = md
        self.buffer = ""
        self.total = ""
        self.started = False

    def feed(self, delta: str) -> None:
        if not delta:
            return
        if not self.started:
            self.started = True
            self.screen.print()
        self.total += delta
        self.buffer += delta
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            for rendered in self.md.render(line, _width()):
                self.screen.print(rendered)

    def tail(self) -> str:
        return self.buffer.replace("\n", " ")

    def finish(self, full_text: str | None = None) -> None:
        if full_text and len(full_text) > len(self.total):
            self.feed(full_text[len(self.total) :])
        if self.buffer:
            for rendered in self.md.render(self.buffer, _width()):
                self.screen.print(rendered)
            self.buffer = ""
        self.md.in_code = False


class AppServerError(RuntimeError):
    pass


class AppServer:
    """Minimal JSON-RPC client for `codex app-server` over stdio."""

    def __init__(self, binary: str, log_path: Path) -> None:
        self.binary = binary
        self.log_path = log_path
        self.proc: subprocess.Popen[str] | None = None
        self.inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()
        self._log = log_path.open("a", encoding="utf-8")
        self.stderr_path = log_path.with_suffix(".stderr")

    def start(self) -> None:
        stderr = self.stderr_path.open("w", encoding="utf-8")
        try:
            self.proc = subprocess.Popen(
                [self.binary, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise AppServerError(f"could not start {self.binary} app-server: {exc}") from exc
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            self._log.write("<< " + line + "\n")
            self._log.flush()
            try:
                self.inbox.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.inbox.put(None)

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        raw = json.dumps(message)
        self._log.write(">> " + raw + "\n")
        self._log.flush()
        with self._lock:
            try:
                self.proc.stdin.write(raw + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise AppServerError(f"app-server pipe closed: {exc}") from exc

    def request_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def respond_error(self, request_id: Any, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": message}})

    def rotate_log(self) -> None:
        self._log.close()
        self._log = self.log_path.open("w", encoding="utf-8")

    def close(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._log.close()


class Client:
    def __init__(self, yolo: bool, binary: str) -> None:
        self.yolo = yolo
        self.screen = _Screen()
        log_path = Path(tempfile.mktemp(prefix="codex-mobile-app-", suffix=".log"))
        self.server = AppServer(binary, log_path)
        self.thread_id: str | None = None
        self.thread_path: str | None = None
        self.model: str | None = None
        self.model_override: str | None = None
        self.turn_id: str | None = None
        self.turn_count = 0
        self.last_elapsed = 0.0
        self.busy = False
        self.interrupts = 0
        self.stream: _Stream | None = None
        self.md = _Markdown()
        self.reasoning: dict[str, str] = {}
        self.pending: dict[int, dict[str, Any] | None] = {}
        self.turn_error: str | None = None
        self.error_printed = False
        self.error_deadline = 0.0
        self.started_at = 0.0

    # ----- transport -------------------------------------------------------

    def start(self) -> None:
        self.server.start()
        result = self.request(
            "initialize",
            {"clientInfo": {"name": "codexmobile", "title": "Codex mobile", "version": CLIENT_VERSION}},
            timeout=20,
        )
        if not isinstance(result, dict):
            raise AppServerError("initialize returned no result")
        self.server.notify("initialized")

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 120) -> Any:
        rid = self.server.request_id()
        self.pending[rid] = None
        self.server.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AppServerError(f"{method}: timed out after {timeout:.0f}s")
            try:
                message = self.server.inbox.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                self._tick()
                continue
            if message is None:
                raise AppServerError("app-server exited")
            if message.get("id") == rid and "method" not in message:
                del self.pending[rid]
                if "error" in message:
                    err = message["error"] or {}
                    raise AppServerError(str(err.get("message") or err))
                return message.get("result")
            self.dispatch(message)

    def dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if not method:
            return
        params = message.get("params") or {}
        if "id" in message:
            self.handle_server_request(message["id"], method, params)
            return
        handler = getattr(self, "on_" + method.replace("/", "_"), None)
        if handler:
            handler(params)

    # ----- rendering helpers -------------------------------------------------

    def status_line(self) -> str:
        model = self.model_override or self.model or ""
        parts = [f"◇ {_short_id(self.thread_id)}"]
        if self.busy:
            parts.append(f"thinking… {int(time.time() - self.started_at)}s")
        else:
            parts.append("idle")
        if model:
            parts.append(model)
        if self.yolo:
            parts.append("yolo")
        return f"{DIM}{' · '.join(parts)}{RESET}"

    def _tick(self) -> None:
        if not self.busy:
            return
        if self.screen.tty:
            lines = []
            tail = self.stream.tail() if self.stream else ""
            if tail:
                lines.append(tail)
            lines.append(self.status_line())
            self.screen.live(lines)
        else:
            elapsed = int(time.time() - self.started_at)
            if elapsed and elapsed % 30 == 0 and getattr(self, "_last_plain_tick", -1) != elapsed:
                self._last_plain_tick = elapsed
                self.screen.print(f"{DIM}[thinking… {elapsed}s]{RESET}")

    def say(self, text: str, color: str = "") -> None:
        for line in text.splitlines() or [""]:
            for part in _wrap(line, _width()):
                self.screen.print(f"{color}{part}{RESET}" if color else part)

    def error(self, text: str) -> None:
        self.say(text, RED)

    # ----- notifications ----------------------------------------------------

    def on_thread_started(self, params: dict[str, Any]) -> None:
        thread = params.get("thread") or {}
        if thread.get("id") and not self.thread_id:
            self.thread_id = thread["id"]
        if thread.get("model"):
            self.model = thread["model"]

    def on_turn_started(self, params: dict[str, Any]) -> None:
        turn = params.get("turn") or {}
        if turn.get("id"):
            self.turn_id = turn["id"]

    def on_item_started(self, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        kind = item.get("type")
        if kind == "commandExecution":
            self._finish_stream()
            command = legacy._compact_command(item.get("command"))
            self.screen.print()
            self.screen.print(f"{DIM}$ {legacy._one_line(command, _width() - 2)}{RESET}")
        elif kind == "agentMessage":
            self.stream = _Stream(self.screen, self.md)

    def on_item_agentMessage_delta(self, params: dict[str, Any]) -> None:
        if self.stream is None:
            self.stream = _Stream(self.screen, self.md)
        self.stream.feed(str(params.get("delta") or ""))
        self._tick()

    def on_item_reasoning_summaryTextDelta(self, params: dict[str, Any]) -> None:
        key = str(params.get("itemId"))
        self.reasoning[key] = self.reasoning.get(key, "") + str(params.get("delta") or "")

    def on_item_completed(self, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        kind = item.get("type")
        if kind == "agentMessage":
            if self.stream is None:
                self.stream = _Stream(self.screen, self.md)
            self.stream.finish(str(item.get("text") or ""))
            self.stream = None
        elif kind == "commandExecution":
            self._render_command_done(item)
        elif kind == "fileChange":
            self._finish_stream()
            changes = item.get("changes") or []
            paths = [str(c.get("path") or "") for c in changes if isinstance(c, dict)]
            status = str(item.get("status") or "")
            color = RED if status == "failed" else DIM
            label = f"✎ {len(paths)} file{'s' if len(paths) != 1 else ''} changed"
            if status == "failed":
                label = "✎ file change failed"
            self.screen.print(f"{color}{label}{RESET}")
            for path in paths[:8]:
                self.screen.print(f"{DIM}  {legacy._truncate_width(path, _width() - 2)}{RESET}")
        elif kind == "reasoning":
            summary = self.reasoning.pop(str(item.get("id")), "")
            if not summary:
                parts = item.get("summary") or []
                summary = " ".join(
                    str(p.get("text") if isinstance(p, dict) else p) for p in parts if p
                ).strip()
            if summary:
                self._finish_stream()
                first = legacy._one_line(summary, _width() - 2)
                self.screen.print(f"{DIM}∴ {first}{RESET}")

    def _render_command_done(self, item: dict[str, Any]) -> None:
        status = str(item.get("status") or "")
        exit_code = item.get("exitCode")
        output = str(item.get("aggregatedOutput") or "")
        failed = status == "failed" or exit_code not in (0, None)
        color = RED if failed else DIM
        n = legacy._line_count(output)
        code_label = exit_code if exit_code is not None else "?"
        count_label = f" · {n} line{'s' if n != 1 else ''}" if n else ""
        if failed:
            self.screen.print(f"{color}  ⎿ failed · exit {code_label}{count_label}{RESET}")
            width = _width() - 2
            for line in output.rstrip("\n").splitlines()[-FAIL_TAIL_LINES:]:
                self.screen.print(f"{RED}  {legacy._truncate_width(line, width)}{RESET}")
        else:
            self.screen.print(f"{color}  ⎿ exit {code_label}{count_label}{RESET}")

    def on_turn_completed(self, params: dict[str, Any]) -> None:
        turn = params.get("turn") or {}
        if self.turn_id and turn.get("id") not in (None, self.turn_id):
            return
        error = turn.get("error") or {}
        if error.get("message") and not self.turn_error:
            self.turn_error = str(error["message"])
        status = str(turn.get("status") or "")
        if status == "interrupted" and not self.turn_error:
            self.turn_error = "interrupted"
        self.busy = False

    def on_error(self, params: dict[str, Any]) -> None:
        error = params.get("error") or {}
        message = str(error.get("message") or params.get("message") or "unknown error")
        info = error.get("codexErrorInfo")
        if isinstance(info, str):
            message = f"{info}: {message}"
        self._finish_stream()
        self.error(message)
        if not params.get("willRetry"):
            self.turn_error = message
            self.error_printed = True
            self.error_deadline = time.time() + 5

    def on_warning(self, params: dict[str, Any]) -> None:
        message = str(params.get("message") or "")
        if message:
            self.say(f"⚠ {message}", DIM)

    def on_model_rerouted(self, params: dict[str, Any]) -> None:
        to_model = params.get("toModel") or params.get("model")
        if to_model:
            self.model = str(to_model)

    def _finish_stream(self) -> None:
        if self.stream is not None:
            self.stream.finish()
            self.stream = None

    # ----- server requests --------------------------------------------------

    def handle_server_request(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        if method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            command = legacy._compact_command(params.get("command"))
            reason = str(params.get("reason") or "")
            if self.yolo:
                self.server.respond(request_id, {"decision": "accept"})
                return
            self._finish_stream()
            self.screen.print(f"{BOLD}approve?{RESET} $ {legacy._one_line(command, _width() - 12)}")
            if reason:
                self.say(reason, DIM)
            self.server.respond(request_id, {"decision": "accept" if self._ask_yes_no() else "decline"})
            return
        if method in ("item/fileChange/requestApproval", "applyPatchApproval"):
            if self.yolo:
                self.server.respond(request_id, {"decision": "accept"})
                return
            self._finish_stream()
            self.screen.print(f"{BOLD}approve file changes?{RESET}")
            self.server.respond(request_id, {"decision": "accept" if self._ask_yes_no() else "decline"})
            return
        if method == "item/permissions/requestApproval":
            self.server.respond(request_id, {"decision": "accept" if self.yolo or self._ask_yes_no() else "decline"})
            return
        if method == "item/tool/requestUserInput":
            answers = {}
            self._finish_stream()
            self.screen.clear_live()
            for question in params.get("questions") or []:
                if not isinstance(question, dict):
                    continue
                qid = str(question.get("id") or "")
                self.say(str(question.get("question") or question.get("header") or qid), BOLD)
                try:
                    reply = input(f"{CYAN}{BOLD}answer>{RESET} ")
                except EOFError:
                    reply = ""
                answers[qid] = {"answers": [reply.strip()]}
            self.server.respond(request_id, {"answers": answers})
            return
        self.server.respond_error(request_id, f"{method} not supported by codexmobile")

    def _ask_yes_no(self) -> bool:
        self.screen.clear_live()
        while True:
            try:
                reply = input(f"{CYAN}{BOLD}[y/n]>{RESET} ").strip().lower()
            except EOFError:
                return False
            if reply in ("y", "yes"):
                return True
            if reply in ("n", "no", ""):
                return False

    # ----- thread lifecycle -------------------------------------------------

    def _thread_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": os.getcwd()}
        if self.yolo:
            params["approvalPolicy"] = "never"
            params["sandbox"] = "danger-full-access"
        else:
            params["approvalPolicy"] = "on-request"
            params["sandbox"] = "workspace-write"
        if self.model_override:
            params["model"] = self.model_override
        return params

    def _adopt(self, result: dict[str, Any]) -> None:
        thread = result.get("thread") or {}
        self.thread_id = str(thread.get("id") or self.thread_id or "")
        self.thread_path = thread.get("path")
        self.model = str(result.get("model") or thread.get("model") or self.model or "")

    def new_thread(self) -> None:
        self.thread_id = None
        self._adopt(self.request("thread/start", self._thread_params()))

    def resume_thread(self, thread_id: str) -> None:
        params = self._thread_params()
        params["threadId"] = thread_id
        self.thread_id = None
        self._adopt(self.request("thread/resume", params))

    def fork_thread(self, thread_id: str) -> None:
        params = self._thread_params()
        params["threadId"] = thread_id
        self.thread_id = None
        self._adopt(self.request("thread/fork", params))

    def list_threads(self, limit: int = 10) -> list[dict[str, Any]]:
        result = self.request(
            "thread/list",
            {"limit": limit, "sortKey": "updated_at", "sortDirection": "desc"},
            timeout=30,
        )
        return list((result or {}).get("data") or [])

    # ----- turns -------------------------------------------------------------

    def run_turn(self, text: str) -> None:
        if not self.thread_id:
            self.new_thread()
        self.server.rotate_log()
        self.turn_error = None
        self.error_printed = False
        self.error_deadline = 0.0
        self.reasoning.clear()
        self.md = _Markdown()
        self.stream = None
        self.interrupts = 0
        self.turn_id = None
        params: dict[str, Any] = {"threadId": self.thread_id, "input": [{"type": "text", "text": text}]}
        if self.model_override:
            params["model"] = self.model_override
        self.started_at = time.time()
        self.busy = True
        try:
            result = self.request("turn/start", params, timeout=60)
        except AppServerError as exc:
            self.busy = False
            self.error(str(exc))
            return
        turn = (result or {}).get("turn") or {}
        self.turn_id = self.turn_id or turn.get("id")
        while self.busy:
            try:
                try:
                    message = self.server.inbox.get(timeout=0.25)
                except queue.Empty:
                    self._tick()
                    if self.error_deadline and time.time() > self.error_deadline:
                        self.busy = False
                    continue
                if message is None:
                    self.busy = False
                    self.error("app-server exited during the turn")
                    break
                self.dispatch(message)
            except KeyboardInterrupt:
                self.interrupts += 1
                if self.interrupts >= 2:
                    self.busy = False
                    self.screen.clear_live()
                    raise
                self.screen.print(f"{DIM}[interrupting… press Ctrl-C again to quit]{RESET}")
                if self.thread_id and self.turn_id:
                    try:
                        self.server.send(
                            {
                                "jsonrpc": "2.0",
                                "id": self.server.request_id(),
                                "method": "turn/interrupt",
                                "params": {"threadId": self.thread_id, "turnId": self.turn_id},
                            }
                        )
                    except AppServerError as exc:
                        self.error(str(exc))
        self._finish_stream()
        self.screen.clear_live()
        self.last_elapsed = time.time() - self.started_at
        self.turn_count += 1
        if self.turn_error:
            if self.turn_error != "interrupted" and not self.error_printed:
                self.error(self.turn_error)
            self.screen.print(f"{DIM}✳ {self.turn_error if self.turn_error == 'interrupted' else 'failed'} after {self.last_elapsed:.1f}s · {_clock()}{RESET}")
        else:
            self.screen.print(f"{DIM}✳ done in {self.last_elapsed:.1f}s · {_clock()}{RESET}")

    # ----- local commands -----------------------------------------------------

    def show_status(self) -> None:
        lines = [f"{BOLD}thread{RESET}   {self.thread_id or '(none yet: first prompt starts one)'}"]
        if self.thread_path:
            lines.append(f"{BOLD}file{RESET}     {self.thread_path}")
        lines.append(f"{BOLD}model{RESET}    {self.model_override or self.model or '(default)'}")
        lines.append(f"{BOLD}cwd{RESET}      {_display_cwd()}")
        lines.append(f"{BOLD}turns{RESET}    {self.turn_count} this run · last {self.last_elapsed:.1f}s")
        lines.append(f"{BOLD}yolo{RESET}     {'on' if self.yolo else 'off'}")
        lines.append(f"{BOLD}log{RESET}      {self.server.log_path}")
        for line in lines:
            for part in _wrap(line, _width(), "         "):
                self.screen.print(part)

    def help_text(self) -> str:
        return "\n".join(
            [
                f"{BOLD}Local commands{RESET} (never sent to Codex)",
                "  /status          thread, model, cwd, turns, yolo",
                "  /resume <id>     switch to another Codex thread",
                "  /resume --last   most recent thread",
                "  /fork <id>       fork a thread into a new one",
                "  /new             start a new thread",
                "  /model [name]    show or set the model",
                "  /history         transcript of the current thread",
                "  /full            raw JSON-RPC log of the last turn",
                "  /quit            exit (Ctrl-C twice also works)",
                f"{DIM}Anything else is sent to Codex.{RESET}",
            ]
        )

    def _resolve_thread(self, target: str) -> str | None:
        if target == "--last":
            threads = self.list_threads(limit=1)
            if threads:
                return str(threads[0].get("id"))
            return legacy._session_id_from_path(legacy._latest_session_file())
        match = legacy._SESSION_ID_RE.search(target)
        if match:
            return match.group(0)
        for thread in self.list_threads(limit=50):
            if str(thread.get("id") or "").startswith(target):
                return str(thread["id"])
        return legacy._session_id_from_path(legacy._find_session_file(target))

    def handle_local(self, command: str) -> bool:
        parts = command.split()
        name = parts[0].lower()
        if name == "/status":
            self.show_status()
            return True
        if name == "/help":
            for line in self.help_text().splitlines():
                self.screen.print(line)
            return True
        if name in ("/history",):
            legacy._page(legacy._render_history_text(self.thread_id))
            return True
        if name in ("/full",):
            raw = legacy._safe_read(self.server.log_path)
            err = legacy._safe_read(self.server.stderr_path)
            legacy._page((raw or "(no events)") + ("\n\n--- stderr ---\n" + err if err.strip() else ""))
            return True
        if name == "/new":
            try:
                self.new_thread()
            except AppServerError as exc:
                self.error(str(exc))
                return True
            self.say(f"new thread {self.thread_id}", GREEN)
            return True
        if name == "/model":
            if len(parts) < 2:
                self.say(f"model: {self.model_override or self.model or '(default)'}")
                return True
            self.model_override = parts[1]
            self.say(f"model for next turns: {self.model_override}", GREEN)
            return True
        if name in ("/resume", "/fork"):
            if len(parts) < 2:
                self.error(f"usage: {name} <id>" + (" | /resume --last" if name == "/resume" else ""))
                return True
            try:
                target = self._resolve_thread(parts[1])
                if not target:
                    self.error(f"no thread matching {parts[1]}")
                    return True
                if name == "/resume":
                    self.resume_thread(target)
                    self.say(f"now on thread {self.thread_id}", GREEN)
                else:
                    self.fork_thread(target)
                    self.say(f"forked into thread {self.thread_id}", GREEN)
            except AppServerError as exc:
                self.error(str(exc))
            return True
        return False

    # ----- picker ------------------------------------------------------------

    def pick_thread(self) -> str | None:
        """Returns a thread id, None for a new thread, or "quit"."""
        try:
            threads = self.list_threads()
        except AppServerError:
            threads = []
        rows: list[tuple[str, str, str]] = []
        for thread in threads:
            tid = str(thread.get("id") or "")
            preview = legacy._one_line(str(thread.get("preview") or ""), 60)
            updated = thread.get("updatedAt") or thread.get("recencyAt") or 0
            if tid and preview:
                rows.append((tid, legacy._age(float(updated)), preview))
        if not rows:
            rows = legacy._recent_sessions()
        if not rows:
            return None
        width = _width()
        self.screen.print(f"{BOLD}Recent Codex threads{RESET}")
        for index, (tid, age, title) in enumerate(rows, start=1):
            meta = f"{age} · {_short_id(tid)}"
            room = max(10, width - len(meta) - 6)
            self.screen.print(f"{index:>2}. {legacy._one_line(title, room):<{room}} {DIM}{meta}{RESET}")
        self.screen.print(f"{DIM} n. new thread   q. quit{RESET}")
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
            self.error(f"pick 1-{len(rows)}, n or q")

    # ----- REPL ----------------------------------------------------------------

    def print_user(self, text: str) -> None:
        self.screen.print()
        lines = text.rstrip().splitlines() or [""]
        for index, line in enumerate(lines):
            prefix = "> " if index == 0 else "  "
            for part in _wrap(prefix + line, _width(), "  "):
                self.screen.print(f"{BOLD}{part}{RESET}")

    def read_prompt(self) -> str | None:
        tty = self.screen.tty
        if tty:
            self.screen.live([self.status_line()])
            sys.stdout.write("\n")
            self.screen._live.append("")
        try:
            line = input(f"{BOLD}> {RESET}")
        except EOFError:
            if tty:
                sys.stdout.write("\r\033[K\033[1A\033[K")
                sys.stdout.flush()
                self.screen._live = []
            return None
        except KeyboardInterrupt:
            if tty:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self.screen._live = []
            raise
        if tty:
            rows = max(1, -(-(2 + len(line)) // _width()))
            sys.stdout.write("\033[1A\033[K" * (rows + 1))
            sys.stdout.flush()
            self.screen._live = []
        return line

    def repl(self) -> int:
        idle_interrupts = 0
        while True:
            try:
                line = self.read_prompt()
            except KeyboardInterrupt:
                idle_interrupts += 1
                sys.stdout.write("\n")
                if idle_interrupts >= 2:
                    return 0
                self.say("(press Ctrl-C again or /quit to exit)", DIM)
                continue
            idle_interrupts = 0
            if line is None:
                return 0
            text = line.strip()
            if not text:
                continue
            if text in {"/quit", "/exit", ":q", ":quit", "quit", "exit"}:
                return 0
            if text in {":history", ":full"}:
                text = "/" + text[1:]
            if text.startswith("/"):
                if self.handle_local(text):
                    continue
                self.say("not a local command; sending to Codex", DIM)
            self.print_user(text)
            try:
                self.run_turn(text)
            except KeyboardInterrupt:
                return 0
            except AppServerError as exc:
                self.error(str(exc))
                return 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="codexmobile", description="Codex on the phone, Claude Code style.")
    parser.add_argument("--yolo", action="store_true", help="No approvals, no sandbox.")
    parser.add_argument("--legacy", action="store_true", help="Use the codex exec --json transcript mode.")
    parser.add_argument("args", nargs="*", help="'resume ID [PROMPT]', 'fork ID [PROMPT]', 'start [PROMPT]' or prompt text")
    return parser.parse_args(argv)


def _plan(args: list[str]) -> tuple[str, str | None, str]:
    if args and args[0] in {"resume", "fork"}:
        if len(args) < 2:
            raise SystemExit(f"{args[0]} requires a thread id")
        return args[0], args[1], " ".join(args[2:]).strip()
    if args and args[0] == "start":
        return "start", None, " ".join(args[1:]).strip()
    return "auto", None, " ".join(args).strip()


def _fallback(reason: str, argv: list[str]) -> int:
    print(f"{RED}[app-server unavailable: {reason}]{RESET}", flush=True)
    print(f"{DIM}[falling back to codex exec transcript mode]{RESET}", flush=True)
    return legacy.main([a for a in argv if a != "--legacy"])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ns = _parse_args(argv)
    if ns.legacy:
        return legacy.main([a for a in argv if a != "--legacy"])
    mode, target, initial_prompt = _plan(list(ns.args))
    binary = os.environ.get("CODEX_REAL_BINARY") or "codex"
    client = Client(yolo=bool(ns.yolo), binary=binary)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        client.start()
    except AppServerError as exc:
        return _fallback(str(exc), argv)

    screen = client.screen
    screen.print("═" * _width())
    screen.print(f"{BOLD}Codex mobile{RESET} {DIM}· app-server · /help for commands{RESET}")
    try:
        if mode == "resume" and target:
            client.resume_thread(target)
            screen.print(f"Resuming {DIM}{client.thread_id}{RESET}")
        elif mode == "fork" and target:
            client.fork_thread(target)
            screen.print(f"Forked into {DIM}{client.thread_id}{RESET}")
        elif mode == "auto" and not initial_prompt and sys.stdin.isatty():
            choice = client.pick_thread()
            if choice == "quit":
                client.server.close()
                return 0
            if choice:
                client.resume_thread(choice)
                screen.print(f"Resuming {DIM}{client.thread_id}{RESET}")
            else:
                client.new_thread()
                screen.print(f"New thread {DIM}{client.thread_id}{RESET}")
        else:
            client.new_thread()
            screen.print(f"New thread {DIM}{client.thread_id}{RESET}")
    except AppServerError as exc:
        client.server.close()
        return _fallback(str(exc), argv)
    screen.print("═" * _width())

    code = 0
    try:
        if initial_prompt:
            client.print_user(initial_prompt)
            client.run_turn(initial_prompt)
            if not sys.stdin.isatty():
                return 1 if client.turn_error else 0
        elif not sys.stdin.isatty():
            piped = sys.stdin.read().strip()
            if piped:
                client.print_user(piped)
                client.run_turn(piped)
                return 1 if client.turn_error else 0
            return 0
        code = client.repl()
    except KeyboardInterrupt:
        code = 0
    finally:
        client.screen.clear_live()
        client.server.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())

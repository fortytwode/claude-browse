"""Phone-friendly Codex client on top of `codex app-server`.

Talks JSON-RPC to the machine's shared app-server daemon through
`codex app-server proxy` (WebSocket over the proxy's stdio), so a thread that
is open elsewhere on the Mac can be joined and a turn keeps running when the
phone's connection drops. Without a daemon it spawns a private
`codex app-server` on stdio. Agent text is streamed and word-wrapped to the
terminal width; one status row is rewritten in place (no alternate screen, no
cursor-up, no absolute addressing), so mobile SSH clients keep scrollback.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import errno
import json
import os
import queue
import re
import select
import shutil
import signal
import struct
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
CLIENT_VERSION = "2.1.0"
FAIL_TAIL_LINES = 8
STATE_PATH = Path.home() / ".cache" / "codexmobile" / "seen.json"
WRITER_LOCKS = Path.home() / ".codex" / "thread-writer-locks"
_ACTIVE_WRITER_RE = re.compile(r"already has an active writer", re.I)
_DAEMON_BUSY_RE = re.compile(r"draining|retry after reconnecting|lost the Codex daemon|pipe closed", re.I)
_GONE_ERRNOS = {errno.EIO, errno.EPIPE, errno.ENXIO, errno.EBADF}


class TerminalGone(Exception):
    """The terminal went away (dropped SSH connection, SIGHUP, SIGTERM)."""

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
    """Permanent transcript plus one status row. The status row lives on the
    cursor's own line and is only ever rewritten with \\r + ESC[K; nothing
    moves the cursor up, so terminals with odd row accounting stay in sync."""

    def __init__(self) -> None:
        self.tty = sys.stdout.isatty()
        self.col = 0
        self.status_shown = False
        self._last_blank = True
        self._lock = threading.RLock()

    @staticmethod
    def _out(text: str = "", flush: bool = False) -> None:
        try:
            if text:
                sys.stdout.write(text)
            if flush:
                sys.stdout.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise TerminalGone(str(exc)) from exc
        except OSError as exc:
            if exc.errno in _GONE_ERRNOS:
                raise TerminalGone(str(exc)) from exc
            raise

    def _drop_status(self) -> None:
        if self.status_shown:
            self._out("\r\033[K")
            self.status_shown = False
            self.col = 0

    def write(self, text: str) -> None:
        """Append text to the current transcript line (no newline)."""
        with self._lock:
            self._drop_status()
            self._out(text, flush=True)
            self.col += _visible_len(text)

    def newline(self) -> None:
        with self._lock:
            self._drop_status()
            self._out("\n", flush=True)
            self._last_blank = self.col == 0
            self.col = 0

    def end_line(self) -> None:
        if self.col:
            self.newline()

    def print(self, text: str = "") -> None:
        with self._lock:
            self._drop_status()
            self.end_line()
            self._out(text + "\n", flush=True)
            self._last_blank = not _ANSI_RE.sub("", text).strip()
            self.col = 0

    def block(self) -> None:
        with self._lock:
            self.end_line()
            if not self._last_blank:
                self.print()

    def status(self, text: str) -> None:
        if not self.tty:
            return
        with self._lock:
            if self.col:
                return
            plain = _ANSI_RE.sub("", text)
            limit = _width()
            if len(plain) > limit:
                text = plain[: limit - 1] + "…"
            self._out("\r\033[K" + text, flush=True)
            self.status_shown = True

    def clear_status(self) -> None:
        with self._lock:
            self._drop_status()
            self._out(flush=True)


class _Stream:
    """Streams agent text straight into the transcript, wrapping at word
    boundaries as deltas arrive. Light markdown: bullets, headings, **bold**,
    `code`, fenced blocks (raw, hard-cut at the width)."""

    def __init__(self, screen: _Screen) -> None:
        self.screen = screen
        self.word = ""
        self.total = ""
        self.started = False
        self.at_line_start = True
        self.indent = ""
        self.line_bold = False
        self.bold = False
        self.code = False
        self.in_fence = False
        self.fence_line = ""
        self.fence_opening = False
        self.fence_held = ""

    def feed(self, delta: str) -> None:
        if not delta:
            return
        if not self.started:
            self.started = True
            self.screen.block()
        self.total += delta
        for ch in delta:
            if self.in_fence:
                self._fence_char(ch)
            elif ch == "\n":
                self._flush_word()
                if self.in_fence:
                    self.fence_opening = False
                    continue
                self._end_line()
            elif ch in " \t":
                self._flush_word()
                self._space()
            else:
                self.word += ch

    def _fence_char(self, ch: str) -> None:
        if ch == "\n":
            if self.fence_opening:
                self.fence_opening = False
            elif self.fence_held == "```":
                self._close_fence()
            else:
                self._fence_write(self.fence_held)
                self.screen.newline()
            self.fence_held = ""
            return
        if self.fence_opening:
            return
        if ch == "`" and len(self.fence_held) < 3 and self.fence_held == "`" * len(self.fence_held):
            self.fence_held += ch
            return
        if self.fence_held:
            self._fence_write(self.fence_held)
            self.fence_held = "\x00"
        self._fence_write(ch)

    def _fence_write(self, text: str) -> None:
        for ch in text.replace("\x00", ""):
            if self.screen.col >= _width():
                self.screen.newline()
            self.screen.write(ch)

    def _close_fence(self) -> None:
        self.in_fence = False
        self.screen.end_line()
        self.screen.print(f"{DIM}{'─' * min(_width(), 40)}{RESET}")
        self.at_line_start = True

    def _style(self) -> str:
        return (BOLD if self.bold or self.line_bold else "") + (DIM if self.code else "")

    def _emit(self, text: str) -> None:
        width = _width()
        if self.at_line_start:
            self.screen.write(self.indent)
            self.at_line_start = False
        elif self.screen.col + len(text) > width and self.screen.col > len(self.indent):
            self.screen.newline()
            self.screen.write(self.indent)
        while len(text) > width - len(self.indent):
            room = width - self.screen.col
            if room <= 0:
                self.screen.newline()
                self.screen.write(self.indent)
                room = width - self.screen.col
            self.screen.write(self._style() + text[:room] + (RESET if self._style() else ""))
            text = text[room:]
            self.screen.newline()
            self.screen.write(self.indent)
        style = self._style()
        self.screen.write(style + text + (RESET if style else ""))

    def _flush_word(self) -> None:
        word = self.word
        self.word = ""
        if not word:
            return
        if self.at_line_start and self.indent == "":
            if word.startswith("```"):
                self.screen.end_line()
                self.screen.print(f"{DIM}{'─' * min(_width(), 40)}{RESET}")
                self.in_fence = True
                self.fence_opening = True
                self.fence_held = ""
                return
            if word in ("-", "*", "•"):
                self.screen.write("• ")
                self.at_line_start = False
                self.indent = "  "
                return
            if word.strip("#") == "" and len(word) <= 6:
                self.line_bold = True
                return
            if word[:-1].isdigit() and word[-1] in ".)":
                self.screen.write(word + " ")
                self.at_line_start = False
                self.indent = " " * (len(word) + 1)
                return
        parts = word.split("**")
        for index, part in enumerate(parts):
            if index:
                self.bold = not self.bold
            segs = part.split("`")
            for j, seg in enumerate(segs):
                if j:
                    self.code = not self.code
                if seg:
                    self._emit(seg)
        self._pending_space = True

    def _space(self) -> None:
        if getattr(self, "_pending_space", False) and not self.at_line_start:
            if self.screen.col < _width():
                self.screen.write(" ")
            else:
                self.screen.newline()
                self.screen.write(self.indent)
            self._pending_space = False

    def _end_line(self) -> None:
        self._pending_space = False
        if self.at_line_start:
            self.screen.newline()
        else:
            self.screen.newline()
        self.at_line_start = True
        self.indent = ""
        self.line_bold = False
        self.code = False

    def finish(self, full_text: str | None = None) -> None:
        if full_text and len(full_text) > len(self.total):
            self.feed(full_text[len(self.total) :])
        if self.in_fence:
            self._fence_write(self.fence_held.replace("```", ""))
            self.fence_held = ""
            self._close_fence()
        self._flush_word()
        self.screen.end_line()
        self.bold = self.code = self.line_bold = False


class AppServerError(RuntimeError):
    pass


class AppServer:
    """JSON-RPC to Codex. Prefers the shared daemon (`codex app-server proxy`,
    WebSocket frames over the proxy's stdio); falls back to a private
    `codex app-server` speaking JSON lines on stdio."""

    def __init__(self, binary: str, log_path: Path, prefer_daemon: bool = True) -> None:
        self.binary = binary
        self.log_path = log_path
        self.prefer_daemon = prefer_daemon
        self.mode = "private"
        self.daemon_reason = ""
        self.proc: subprocess.Popen[bytes] | None = None
        self.inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()
        self._log = log_path.open("a", encoding="utf-8")
        self._closed = False
        self.stderr_path = log_path.with_suffix(".stderr")

    # ----- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self.prefer_daemon:
            try:
                self._start_daemon()
                self.mode = "daemon"
            except AppServerError as exc:
                self.daemon_reason = str(exc)
                self._stop_child()
        if self.mode != "daemon":
            self._start_private()
        self._spawn_reader()

    def restart_private(self, reason: str) -> None:
        self.daemon_reason = reason
        self._stop_child()
        self.mode = "private"
        self.inbox = queue.Queue()
        self._start_private()
        self._spawn_reader()

    def reconnect_daemon(self) -> None:
        self._stop_child(grace=0.5)
        self.inbox = queue.Queue()
        try:
            self._start_daemon()
        except AppServerError:
            self._stop_child(grace=0.5)
            raise
        self._spawn_reader()

    def _popen(self, args: list[str]) -> subprocess.Popen[bytes]:
        stderr = self.stderr_path.open("a", encoding="utf-8")
        try:
            return subprocess.Popen(
                [self.binary, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as exc:
            raise AppServerError(f"could not start {self.binary} {' '.join(args)}: {exc}") from exc
        finally:
            stderr.close()

    def _start_private(self) -> None:
        self.proc = self._popen(["app-server"])

    def _start_daemon(self) -> None:
        proc = self.proc = self._popen(["app-server", "proxy"])
        assert proc.stdin and proc.stdout
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        try:
            proc.stdin.write(request.encode())
        except OSError as exc:
            raise AppServerError(f"no Codex daemon to join ({exc})") from exc
        head = b""
        fd = proc.stdout.fileno()
        deadline = time.time() + 5
        while not head.endswith(b"\r\n\r\n"):
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AppServerError("the Codex daemon did not answer")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 1)
            if not chunk:
                raise AppServerError("no Codex daemon running")
            head += chunk
            if len(head) > 8192:
                raise AppServerError("unexpected reply from the Codex daemon")
        if b" 101" not in head.split(b"\r\n", 1)[0]:
            raise AppServerError("the Codex daemon refused the connection")

    def _spawn_reader(self) -> None:
        assert self.proc
        target = self._read_frames if self.mode == "daemon" else self._read_lines
        threading.Thread(target=target, args=(self.proc, self.inbox), daemon=True).start()

    # ----- reading ---------------------------------------------------------

    def _accept(self, text: str, inbox: queue.Queue[dict[str, Any] | None]) -> None:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._log.write("<< " + line + "\n")
                self._log.flush()
            except ValueError:
                pass
            try:
                inbox.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _read_lines(self, proc: subprocess.Popen[bytes], inbox: queue.Queue[dict[str, Any] | None]) -> None:
        assert proc.stdout
        pending = b""
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    self._accept(raw.decode("utf-8", "replace"), inbox)
        except (OSError, ValueError):
            pass
        inbox.put(None)

    @staticmethod
    def _read_exact(proc: subprocess.Popen[bytes], size: int) -> bytes:
        assert proc.stdout
        data = b""
        while len(data) < size:
            chunk = proc.stdout.read(size - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def _read_frames(self, proc: subprocess.Popen[bytes], inbox: queue.Queue[dict[str, Any] | None]) -> None:
        message = b""
        try:
            while True:
                first, second = self._read_exact(proc, 2)
                opcode = first & 0x0F
                size = second & 0x7F
                if size == 126:
                    size = struct.unpack(">H", self._read_exact(proc, 2))[0]
                elif size == 127:
                    size = struct.unpack(">Q", self._read_exact(proc, 8))[0]
                mask = self._read_exact(proc, 4) if second & 0x80 else b""
                data = self._read_exact(proc, size) if size else b""
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if opcode == 0x9:
                    try:
                        self._send_frame(0xA, data)
                    except AppServerError:
                        break
                    continue
                if opcode == 0x8:
                    break
                if opcode in (0x0, 0x1, 0x2):
                    message += data
                    if first & 0x80:
                        self._accept(message.decode("utf-8", "replace"), inbox)
                        message = b""
        except (EOFError, OSError, ValueError, struct.error):
            pass
        inbox.put(None)

    # ----- writing ---------------------------------------------------------

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        size = len(payload)
        head = bytes([0x80 | opcode])
        if size < 126:
            head += bytes([0x80 | size])
        elif size < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", size)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._write(head + mask + masked)

    def _write(self, data: bytes) -> None:
        proc = self.proc
        if not proc or not proc.stdin:
            raise AppServerError("app-server is not running")
        with self._lock:
            try:
                view = memoryview(data)
                while view:
                    written = proc.stdin.write(view)
                    view = view[written or 0 :]
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise AppServerError(f"app-server pipe closed: {exc}") from exc

    def send(self, message: dict[str, Any]) -> None:
        raw = json.dumps(message)
        try:
            self._log.write(">> " + raw + "\n")
            self._log.flush()
        except ValueError:
            pass
        if self.mode == "daemon":
            self._send_frame(0x1, raw.encode())
        else:
            self._write(raw.encode() + b"\n")

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

    # ----- shutdown --------------------------------------------------------

    def _stop_child(self, grace: float = 2.0) -> None:
        proc, self.proc = self.proc, None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                continue
        for stream in (proc.stdout, proc.stdin):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.mode == "daemon" and self.proc:
            try:
                self._send_frame(0x8, b"")
            except AppServerError:
                pass
        self._stop_child()
        try:
            self._log.close()
        except OSError:
            pass


def _load_seen() -> dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_seen(thread_id: str, turn_id: str | None, items: list[str]) -> None:
    data = _load_seen()
    data[thread_id] = {"turn": turn_id, "items": items[-400:], "at": int(time.time())}
    if len(data) > 200:
        for key in sorted(data, key=lambda k: data[k].get("at", 0))[: len(data) - 200]:
            data.pop(key, None)
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def _ps(pid: int) -> dict[str, Any] | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,ppid=,tty=,command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.split(None, 3)
    if len(parts) < 4:
        return None
    return {"pid": int(parts[0]), "ppid": int(parts[1]), "tty": parts[2], "command": parts[3]}


def _terminal_lost(process: dict[str, Any]) -> bool:
    """A session of ours has lost its terminal when it has no controlling tty
    left, the tty device is gone, or the shell that ran it has exited."""
    tty = str(process.get("tty") or "")
    if tty in ("", "??", "?", "-"):
        return True
    if not os.path.exists(f"/dev/{tty}"):
        return True
    return process.get("ppid") == 1


_OURS_RE = re.compile(
    r"^\S*[Pp]ython[\d.]*\s+(?:-\S+\s+)*"
    r"(?:\S*/(?:codexmobile|codex-mobile-json)|-m\s+claude_browse\.codex_mobile_(?:app|json))(?:\s|$)"
)


def _is_ours(command: str) -> bool:
    """True only for an interpreter whose script is codexmobile itself; a
    command line that merely mentions the name does not count."""
    return bool(_OURS_RE.search(command))


def _writer_holders(thread_id: str) -> list[dict[str, Any]]:
    """Processes holding the thread's writer lock, each classified as
    stale (ours, terminal gone), live (ours, on a terminal) or foreign."""
    lock = WRITER_LOCKS / f"{thread_id}.lock"
    if not lock.exists():
        return []
    try:
        out = subprocess.run(["lsof", "-t", "--", str(lock)], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    holders = []
    for token in out.split():
        if not token.isdigit() or int(token) == os.getpid():
            continue
        holder = _ps(int(token))
        if not holder:
            continue
        command = holder["command"]
        parent = _ps(holder["ppid"]) if holder["ppid"] > 1 else None
        owner = parent if parent and _is_ours(parent["command"]) else None
        if owner and owner["pid"] == os.getpid():
            continue
        managed = "--managed-daemon" in command or "app-server-daemon/" in command or "--listen" in command
        if owner:
            gone = _terminal_lost(owner)
            holder.update(kind="stale" if gone else "live", owner=owner["pid"], where=f"another codexmobile session on {owner['tty']}")
        elif "/ChatGPT.app/" in command:
            holder.update(kind="foreign", where="the Codex desktop app")
        elif managed:
            holder.update(kind="foreign", where="a Codex session on the Mac (desktop app or terminal)")
        elif holder["ppid"] == 1 and re.search(r"(^|/)codex app-server\s*$", command):
            holder.update(kind="stale", owner=None, where="a private server left by a dropped connection")
        elif holder["tty"] not in ("??", "?", "-"):
            holder.update(kind="foreign", where=f"a Codex terminal session on {holder['tty']}")
        else:
            holder.update(kind="foreign", where="a Codex session on the Mac (desktop app or terminal)")
        holders.append(holder)
    return holders


def _release(holders: list[dict[str, Any]]) -> bool:
    """Terminate stale holders of ours (owner first, then its servers)."""
    targets: list[int] = []
    for holder in holders:
        if holder.get("kind") != "stale":
            return False
        for pid in (holder.get("owner"), holder["pid"]):
            if pid and pid not in targets and pid != os.getpid():
                targets.append(pid)
    if not targets:
        return False
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + 3
    while time.time() < deadline and any(_ps(pid) for pid in targets):
        time.sleep(0.2)
    for pid in targets:
        if _ps(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    time.sleep(0.3)
    return True


class Client:
    def __init__(self, yolo: bool, binary: str, prefer_daemon: bool = True) -> None:
        self.yolo = yolo
        self.screen = _Screen()
        log_path = Path(tempfile.mktemp(prefix="codex-mobile-app-", suffix=".log"))
        self.server = AppServer(binary, log_path, prefer_daemon=prefer_daemon)
        self.parent_pid = os.getppid()
        self.shown: list[str] = []
        self.started_items: set[str] = set()
        self.last_turn_shown: str | None = None
        self.foreign_activity = False
        self.joining = False
        self.recovering = False
        self.backlog: list[dict[str, Any]] = []
        self.expect_thread: str | None = None
        self.thread_active = False
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
        self.reasoning: dict[str, str] = {}
        self.reasoning_started = 0.0
        self.reasoning_pending = False
        self.pending: dict[int, dict[str, Any] | None] = {}
        self.turn_error: str | None = None
        self.error_printed = False
        self.error_deadline = 0.0
        self.started_at = 0.0

    # ----- transport -------------------------------------------------------

    def start(self) -> None:
        self.server.start()
        try:
            self._initialize(8 if self.server.mode == "daemon" else 20)
        except AppServerError as exc:
            if self.server.mode != "daemon":
                raise
            self.server.restart_private(str(exc))
            self._initialize(20)

    def _initialize(self, timeout: float) -> None:
        result = self.request(
            "initialize",
            {"clientInfo": {"name": "codexmobile", "title": "Codex mobile", "version": CLIENT_VERSION}},
            timeout=timeout,
        )
        if not isinstance(result, dict):
            raise AppServerError("initialize returned no result")
        self.server.notify("initialized")

    def check_terminal(self) -> None:
        parent = os.getppid()
        if parent != self.parent_pid and parent == 1:
            raise TerminalGone("parent process exited")

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 120) -> Any:
        try:
            return self._request(method, params, timeout)
        except AppServerError as exc:
            mid_turn = self.busy and method != "turn/start"
            if self.server.mode != "daemon" or self.recovering or mid_turn or not _DAEMON_BUSY_RE.search(str(exc)):
                raise
            reason = str(exc)
        return self._recover(reason, method, params, timeout)

    def _recover(self, reason: str, method: str, params: dict[str, Any] | None, timeout: float) -> Any:
        """The shared daemon restarts itself for updates. Reconnect and retry;
        if it is not back in time, carry on with a private server."""
        opening = method in ("initialize", "thread/resume", "thread/start", "thread/fork")

        def resubscribe() -> None:
            if not opening and self.thread_id:
                again = self._thread_params()
                again.update(threadId=self.thread_id, excludeTurns=True)
                self._request("thread/resume", again, 60)

        self.recovering = True
        try:
            self.say("· the shared Codex daemon is restarting; reconnecting", DIM)
            deadline = time.time() + 12
            while time.time() < deadline:
                try:
                    self.server.reconnect_daemon()
                    self._initialize(8)
                    resubscribe()
                    return self._request(method, params, timeout)
                except AppServerError as exc:
                    if not _DAEMON_BUSY_RE.search(str(exc)) and "daemon" not in str(exc).lower():
                        raise
                    time.sleep(1.0)
            self.server.restart_private(reason)
            self._initialize(20)
            self.say("· it is not back yet; using a private server (a turn here stops if the connection drops)", DIM)
            resubscribe()
            return self._request(method, params, timeout)
        finally:
            self.recovering = False

    def _request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 120) -> Any:
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
                raise AppServerError("app-server exited" if self.server.mode == "private" else "lost the Codex daemon")
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
        scope = params.get("threadId") if isinstance(params, dict) else None
        if scope is not None and scope not in (self.thread_id, self.expect_thread):
            return
        if "id" in message:
            self.handle_server_request(message["id"], method, params)
            return
        if not self.busy and method.startswith(("item/", "turn/")):
            if self.joining:
                self.backlog.append(message)
            elif method in ("turn/started", "item/started", "item/completed", "turn/completed"):
                self.foreign_activity = True
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
        if self.yolo:
            parts.append("yolo")
        limit = _width()
        if model:
            base = len(" · ".join(parts)) + 3
            room = limit - base
            if room >= 4:
                parts.insert(len(parts) - (1 if self.yolo else 0), model if len(model) <= room else model[: room - 1] + "…")
        line = " · ".join(parts)
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        return f"{DIM}{line}{RESET}"

    def _tick(self) -> None:
        self.check_terminal()
        if not self.busy:
            return
        if self.screen.tty:
            if self.stream is None or not self.stream.started:
                self.screen.status(self.status_line())
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
        self.screen.block()
        self.say(text, RED)

    # ----- notifications ----------------------------------------------------

    def on_thread_started(self, params: dict[str, Any]) -> None:
        thread = params.get("thread") or {}
        if thread.get("model") and thread.get("id") == self.thread_id:
            self.model = thread["model"]

    def on_turn_started(self, params: dict[str, Any]) -> None:
        turn = params.get("turn") or {}
        if turn.get("id"):
            self.turn_id = turn["id"]

    def on_item_started(self, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        kind = item.get("type")
        if item.get("id"):
            if str(item["id"]) in self.shown:
                return
            self.started_items.add(str(item["id"]))
        if kind == "reasoning":
            if not self.reasoning_started:
                self.reasoning_started = time.time()
            self.reasoning_pending = True
        elif kind == "agentMessage":
            self._collapse_reasoning()
            self.stream = _Stream(self.screen)
        elif kind == "commandExecution":
            self._collapse_reasoning()
            self._finish_stream()
            command = legacy._compact_command(item.get("command"))
            self.screen.block()
            self.screen.print(f"{DIM}$ {legacy._one_line(command, _width() - 2)}{RESET}")
        elif kind == "webSearch":
            self._collapse_reasoning()
            self._finish_stream()
            self.screen.block()
            self.screen.print(f"{DIM}🔍 Searching: {legacy._one_line(self._search_query(item), _width() - 13)}{RESET}")
        elif kind == "mcpToolCall":
            self._collapse_reasoning()
            self._finish_stream()
            self.screen.block()
            self.screen.print(f"{DIM}⚙ {legacy._one_line(self._mcp_name(item), _width() - 2)}{RESET}")
        elif kind not in ("userMessage", "fileChange", "plan", None):
            self._collapse_reasoning()
            self._finish_stream()
            self.screen.block()
            self.screen.print(f"{DIM}· {legacy._one_line(self._item_label(kind, item), _width() - 2)}{RESET}")

    @staticmethod
    def _search_query(item: dict[str, Any]) -> str:
        action = item.get("action") or {}
        query = item.get("query") or action.get("query")
        if not query and isinstance(action.get("queries"), list):
            query = ", ".join(str(q) for q in action["queries"])
        if not query and action.get("url"):
            query = str(action["url"])
        return str(query or "web")

    @staticmethod
    def _mcp_name(item: dict[str, Any]) -> str:
        server = str(item.get("server") or "").strip()
        tool = str(item.get("tool") or "").strip()
        return f"{server}.{tool}" if server else tool or "tool"

    @staticmethod
    def _item_label(kind: str, item: dict[str, Any]) -> str:
        words = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(kind)).lower()
        extra = item.get("tool") or item.get("path") or item.get("kind") or ""
        return f"{words} {extra}".strip()

    def _collapse_reasoning(self) -> None:
        if not self.reasoning_pending and not self.reasoning_started:
            return
        elapsed = max(1, int(round(time.time() - self.reasoning_started))) if self.reasoning_started else 0
        self.reasoning_pending = False
        self.reasoning_started = 0.0
        self.reasoning.clear()
        self._finish_stream()
        self.screen.block()
        self.screen.print(f"{DIM}✳ Cogitated for {elapsed}s{RESET}")

    def on_item_agentMessage_delta(self, params: dict[str, Any]) -> None:
        item_id = params.get("itemId")
        if item_id and str(item_id) not in self.started_items:
            return
        if self.stream is None:
            self._collapse_reasoning()
            self.stream = _Stream(self.screen)
        self.stream.feed(str(params.get("delta") or ""))
        self._tick()

    def on_item_reasoning_summaryTextDelta(self, params: dict[str, Any]) -> None:
        if not self.reasoning_started:
            self.reasoning_started = time.time()
        self.reasoning_pending = True
        key = str(params.get("itemId"))
        self.reasoning[key] = self.reasoning.get(key, "") + str(params.get("delta") or "")

    def on_item_completed(self, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        kind = item.get("type")
        item_id = str(item.get("id") or "")
        if item_id:
            if item_id in self.shown:
                return
            quiet = ("agentMessage", "userMessage", "reasoning", "fileChange", "plan", None)
            if item_id not in self.started_items and kind not in quiet:
                self.on_item_started({"item": item})
            self.shown.append(item_id)
        if kind == "agentMessage":
            if self.stream is None:
                self._collapse_reasoning()
                self.stream = _Stream(self.screen)
            self.stream.finish(str(item.get("text") or ""))
            self.stream = None
        elif kind == "commandExecution":
            self._render_command_done(item)
        elif kind == "fileChange":
            self._collapse_reasoning()
            self._finish_stream()
            self.screen.block()
            status = str(item.get("status") or "")
            color = RED if status in ("failed", "declined") else DIM
            changes = [c for c in (item.get("changes") or []) if isinstance(c, dict)]
            if not changes:
                self.screen.print(f"{color}✎ file change {status or 'done'}{RESET}")
            for change in changes:
                path = str(change.get("path") or "")
                added = removed = 0
                for line in str(change.get("diff") or "").splitlines():
                    if line.startswith("+") and not line.startswith("+++"):
                        added += 1
                    elif line.startswith("-") and not line.startswith("---"):
                        removed += 1
                verb = {"add": "Created", "delete": "Deleted"}.get(str(change.get("kind") or "").lower(), "Edited")
                if status in ("failed", "declined"):
                    verb = f"{status.capitalize()} edit to"
                counts = f" (+{added} −{removed})" if added or removed else ""
                room = _width() - len(verb) - len(counts) - 3
                self.screen.print(f"{color}✎ {verb} {legacy._truncate_width(path, room)}{counts}{RESET}")
        elif kind == "webSearch":
            results = item.get("results")
            if isinstance(results, list) and results:
                self.screen.print(f"{DIM}  ⎿ {len(results)} result{'s' if len(results) != 1 else ''}{RESET}")
        elif kind == "mcpToolCall":
            status = str(item.get("status") or "")
            error = item.get("error")
            if status == "failed" or error:
                msg = str((error or {}).get("message") if isinstance(error, dict) else error or "failed")
                self.screen.print(f"{RED}  ⎿ failed · {legacy._one_line(msg, _width() - 12)}{RESET}")
            else:
                result = item.get("result")
                summary = ""
                if isinstance(result, dict):
                    content = result.get("content")
                    if isinstance(content, list):
                        summary = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict)).strip()
                    else:
                        summary = json.dumps(result)[:200]
                elif result is not None:
                    summary = str(result)
                summary = legacy._one_line(summary, _width() - 6) if summary else "done"
                self.screen.print(f"{DIM}  ⎿ {summary}{RESET}")
        elif kind == "plan":
            self._collapse_reasoning()
            self._finish_stream()
            self.screen.block()
            self.screen.print(f"{DIM}☰ Plan: {legacy._one_line(str(item.get('text') or ''), _width() - 8)}{RESET}")
        elif kind == "reasoning":
            self.reasoning_pending = True

    def on_turn_plan_updated(self, params: dict[str, Any]) -> None:
        plan = params.get("plan") or []
        if isinstance(plan, list) and plan:
            done = sum(1 for step in plan if isinstance(step, dict) and str(step.get("status") or "") == "completed")
            current = next((str(step.get("step") or "") for step in plan if isinstance(step, dict) and str(step.get("status") or "") == "inProgress"), "")
            self._finish_stream()
            self.screen.block()
            label = f"☰ Plan {done}/{len(plan)}" + (f" · {current}" if current else "")
            self.screen.print(f"{DIM}{legacy._one_line(label, _width())}{RESET}")

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
            self.screen.clear_status()
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
        self.screen.clear_status()
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

    def _adopt(self, result: dict[str, Any], previous: str | None) -> None:
        thread = result.get("thread") or {}
        self.thread_id = str(thread.get("id") or "") or None
        self.thread_path = thread.get("path")
        self.model = str(result.get("model") or thread.get("model") or self.model or "")
        status = thread.get("status")
        self.thread_active = isinstance(status, dict) and status.get("type") == "active"
        if previous != self.thread_id:
            self.shown = []
            self.started_items = set()
            self.last_turn_shown = None
            self.foreign_activity = False
            if previous and self.server.mode == "daemon":
                try:
                    self.request("thread/unsubscribe", {"threadId": previous}, timeout=5)
                except AppServerError:
                    pass

    def _switch(self, method: str, params: dict[str, Any]) -> None:
        previous = self.thread_id
        if previous:
            self.remember()
        self.thread_id = None
        try:
            result = self.request(method, params)
        except AppServerError:
            self.thread_id = previous
            raise
        finally:
            self.expect_thread = None
        self._adopt(result or {}, previous)

    def new_thread(self) -> None:
        self._switch("thread/start", self._thread_params())
        self.remember()

    def resume_thread(self, thread_id: str) -> None:
        params = self._thread_params()
        params["threadId"] = thread_id
        params["excludeTurns"] = True
        self.expect_thread = thread_id
        self._switch("thread/resume", params)

    def fork_thread(self, thread_id: str) -> None:
        params = self._thread_params()
        params["threadId"] = thread_id
        params["excludeTurns"] = True
        self._switch("thread/fork", params)
        self.remember()

    # ----- opening a thread ---------------------------------------------------

    def _choose(self, options: str, allowed: str) -> str:
        if not sys.stdin.isatty():
            self.say("Nothing opened (not an interactive terminal). `codexmobile fork <id>` works on a copy.", DIM)
            return "q"
        self.say(options, DIM)
        self.screen.clear_status()
        while True:
            try:
                reply = input(f"{CYAN}{BOLD}choose>{RESET} ").strip().lower()[:1]
            except EOFError:
                return "q"
            if reply and reply in allowed:
                return reply

    def open_thread(self, target: str, fork: bool = False, in_session: bool = False) -> bool:
        """Resume or fork a thread and catch up on it. False: nothing opened."""
        released = False
        leave = "q keep the current thread" if in_session else "q quit"
        while True:
            try:
                if fork:
                    self.fork_thread(target)
                    self.screen.print(f"Forked into {DIM}{self.thread_id}{RESET}")
                    return True
                self.joining = True
                self.backlog = []
                self.resume_thread(target)
                self.screen.print(f"Resuming {DIM}{self.thread_id}{RESET}")
                self.replay()
                return True
            except AppServerError as exc:
                self.joining = False
                message = str(exc)
                if self.server.proc is None or self.server.proc.poll() is not None:
                    raise
                if _ACTIVE_WRITER_RE.search(message):
                    holders = _writer_holders(target)
                    if holders and not released and _release(holders):
                        released = True
                        self.say("· released a stale session from a dropped connection", DIM)
                        continue
                    places = sorted({str(h.get("where")) for h in holders}) or ["another Codex session on the Mac"]
                    self.screen.block()
                    self.say(f"This thread is open in {' and '.join(places)}. Only one of them can write to it at a time.")
                    choice = self._choose(f"f fork it into a new thread · p pick another · {leave}", "fpq")
                else:
                    self.error(message)
                    choice = self._choose(f"p pick another · n new thread · {leave}", "pnq")
            if choice == "f":
                fork = True
            elif choice == "n":
                self.new_thread()
                self.screen.print(f"New thread {DIM}{self.thread_id}{RESET}")
                return True
            elif choice == "p":
                picked = self.pick_thread()
                if picked == "quit":
                    return False
                if picked is None:
                    self.new_thread()
                    self.screen.print(f"New thread {DIM}{self.thread_id}{RESET}")
                    return True
                target, fork, released = picked, False, False
            else:
                return False

    # ----- catching up ----------------------------------------------------------

    def remember(self) -> None:
        if self.thread_id:
            _save_seen(self.thread_id, self.last_turn_shown, self.shown)

    @staticmethod
    def _stamp(epoch: Any) -> str:
        try:
            return _dt.datetime.fromtimestamp(float(epoch)).strftime("%-I:%M %p")
        except (TypeError, ValueError, OSError, OverflowError):
            return _clock()

    def _replay_item(self, item: dict[str, Any]) -> None:
        kind = item.get("type")
        item_id = str(item.get("id") or "")
        if kind == "userMessage":
            parts = [str(c.get("text") or "") for c in item.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
            text = "\n".join(part for part in parts if part.strip())
            if text.strip():
                self.print_user(text)
        elif kind == "agentMessage":
            text = str(item.get("text") or "")
            if text.strip():
                stream = _Stream(self.screen)
                stream.feed(text)
                stream.finish()
        else:
            self.on_item_completed({"item": item})
            return
        if item_id:
            self.shown.append(item_id)

    def replay(self) -> None:
        """Show what this client has not displayed yet on the current thread,
        then attach to a turn that is still running."""
        backlog, self.backlog = self.backlog, []
        try:
            self._replay(backlog)
        finally:
            self.joining = False
            self.backlog = []

    def _replay(self, backlog: list[dict[str, Any]]) -> None:
        if not self.thread_id:
            return
        seen = _load_seen().get(self.thread_id) or {}
        known = bool(seen) or bool(self.shown) or bool(self.last_turn_shown)
        if seen and not self.shown:
            self.shown = [str(i) for i in seen.get("items") or []]
            self.last_turn_shown = seen.get("turn") or self.last_turn_shown
        try:
            result = self.request(
                "thread/turns/list",
                {"threadId": self.thread_id, "limit": 8, "itemsView": "full", "sortDirection": "desc"},
                timeout=30,
            )
        except AppServerError:
            return
        backlog += self.backlog
        self.backlog = []
        turns = [t for t in reversed((result or {}).get("data") or []) if isinstance(t, dict)]
        if not turns:
            return
        ids = [t.get("id") for t in turns]
        older = False
        if not known:
            missed = turns[-1:]
        elif self.last_turn_shown in ids:
            missed = turns[ids.index(self.last_turn_shown) :]
        else:
            missed = turns[-3:]
            older = len(turns) > 3
        announced = False
        for turn in missed:
            items = [i for i in turn.get("items") or [] if isinstance(i, dict)]
            fresh = [i for i in items if i.get("type") != "reasoning" and str(i.get("id") or "") not in self.shown]
            running = turn.get("status") == "inProgress"
            attach = running and turn is turns[-1] and self.thread_active
            if not fresh and not attach:
                if not running:
                    self.last_turn_shown = turn.get("id")
                continue
            if fresh and not announced:
                announced = True
                self.screen.block()
                if not known:
                    self.say("· last turn in this thread", DIM)
                else:
                    self.say("· catching up on what you missed" + (" (older turns: /history)" if older else ""), DIM)
            for item in fresh:
                self._replay_item(item)
            if attach:
                self._attach(turn, backlog)
                return
            self.last_turn_shown = turn.get("id")
            self.screen.block()
            status = str(turn.get("status") or "")
            error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
            if error.get("message"):
                self.say(str(error["message"]), RED)
            when = self._stamp(turn.get("completedAt") or turn.get("startedAt"))
            took = f" in {float(turn['durationMs']) / 1000:.1f}s" if turn.get("durationMs") else ""
            if status == "completed":
                self.screen.print(f"{DIM}✳ done{took} · {when}{RESET}")
            elif running:
                self.screen.print(f"{DIM}✳ stopped when its session ended · {when}{RESET}")
            else:
                self.screen.print(f"{DIM}✳ {status or 'ended'}{took.replace(' in ', ' after ')} · {when}{RESET}")
        self.remember()

    def _attach(self, turn: dict[str, Any], backlog: list[dict[str, Any]]) -> None:
        self.turn_id = turn.get("id")
        started = turn.get("startedAt")
        self.started_at = float(started) if started else time.time()
        self.turn_error = None
        self.error_printed = False
        self.error_deadline = 0.0
        self.reasoning.clear()
        self.reasoning_started = 0.0
        self.reasoning_pending = False
        self.stream = None
        self.interrupts = 0
        self.screen.block()
        self.say("· this turn is still running; following it live", DIM)
        self.joining = False
        self.busy = True
        for message in backlog:
            self.dispatch(message)
        self._pump()
        self._finish_turn()

    def _drain_idle(self) -> None:
        while True:
            try:
                message = self.server.inbox.get_nowait()
            except queue.Empty:
                break
            if message is None:
                raise AppServerError("app-server exited" if self.server.mode == "private" else "lost the Codex daemon")
            self.dispatch(message)
        if self.foreign_activity:
            self.foreign_activity = False
            self.joining = True
            self.replay()

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
        self._drain_idle()
        self.server.rotate_log()
        self.turn_error = None
        self.error_printed = False
        self.error_deadline = 0.0
        self.reasoning.clear()
        self.reasoning_started = 0.0
        self.reasoning_pending = False
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
        self._pump()
        self._finish_turn()

    def _interrupt(self) -> None:
        if self.thread_id and self.turn_id:
            self.server.send(
                {
                    "jsonrpc": "2.0",
                    "id": self.server.request_id(),
                    "method": "turn/interrupt",
                    "params": {"threadId": self.thread_id, "turnId": self.turn_id},
                }
            )

    def _pump(self) -> None:
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
                    if self.server.mode == "daemon":
                        self.error("lost the Codex daemon; the turn may still be running. Reopen the thread to catch up.")
                    else:
                        self.error("app-server exited during the turn")
                    break
                self.dispatch(message)
            except KeyboardInterrupt:
                self.interrupts += 1
                if self.interrupts >= 2:
                    self.busy = False
                    self.screen.clear_status()
                    raise
                self.screen.print(f"{DIM}[interrupting… press Ctrl-C again to quit]{RESET}")
                try:
                    self._interrupt()
                except AppServerError as exc:
                    self.error(str(exc))

    def _finish_turn(self) -> None:
        self._collapse_reasoning()
        self._finish_stream()
        self.screen.clear_status()
        self.last_elapsed = time.time() - self.started_at
        self.turn_count += 1
        self.last_turn_shown = self.turn_id or self.last_turn_shown
        self.remember()
        if self.turn_error:
            if self.turn_error != "interrupted" and not self.error_printed:
                self.error(self.turn_error)
            self.screen.block()
            self.screen.print(f"{DIM}✳ {self.turn_error if self.turn_error == 'interrupted' else 'failed'} after {self.last_elapsed:.1f}s · {_clock()}{RESET}")
        else:
            self.screen.block()
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
        lines.append(f"{BOLD}via{RESET}      {'shared Codex daemon' if self.server.mode == 'daemon' else 'private server'}")
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
                if not self.open_thread(target, fork=name == "/fork", in_session=True):
                    self.say(f"staying on thread {self.thread_id or '(none)'}", DIM)
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
        self.screen.block()
        self.screen.print(f"{BOLD}Recent Codex threads{RESET}")
        for index, (tid, age, title) in enumerate(rows, start=1):
            for line in legacy._picker_lines(index, title, f"{age} · {_short_id(tid)}", _width()):
                self.screen.print(line)
        self.screen.print(f"{DIM} n. new thread   q. quit{RESET}")
        while True:
            self.screen.clear_status()
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
        self.screen.block()
        lines = text.strip().splitlines() or [""]
        for index, line in enumerate(lines):
            prefix = "> " if index == 0 else "  "
            for part in _wrap(prefix + line.strip(), _width(), "  "):
                self.screen.print(f"{CYAN}{BOLD}{part}{RESET}")
        self.screen.print()

    def read_prompt(self) -> str | None:
        self.screen.clear_status()
        self.screen.end_line()
        if self.screen.tty:
            self.screen.print(self.status_line())
        try:
            line = input(f"{CYAN}{BOLD}> {RESET}")
        except EOFError:
            try:
                sys.stdout.write("\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                pass
            return None
        self.screen.col = 0
        self.screen._last_blank = False
        self.screen.print()
        return line

    def repl(self) -> int:
        idle_interrupts = 0
        while True:
            try:
                line = self.read_prompt()
            except KeyboardInterrupt:
                idle_interrupts += 1
                sys.stdout.write("\n")
                self.screen.col = 0
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
            if not self.screen.tty:
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
    parser.add_argument("--last", action="store_true", help="With resume: the most recently updated thread.")
    parser.add_argument("args", nargs="*", help="'resume ID|--last [PROMPT]', 'fork ID [PROMPT]', 'start [PROMPT]' or prompt text")
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


def _on_hangup(signum: int, _frame: Any) -> None:
    raise TerminalGone(f"signal {signum}")


def _terminal_error(exc: BaseException) -> bool:
    if isinstance(exc, (TerminalGone, BrokenPipeError)):
        return True
    return isinstance(exc, OSError) and exc.errno in _GONE_ERRNOS


def _leave(client: Client, gone: bool) -> None:
    """Release everything. With the terminal gone a turn on the shared daemon
    keeps running there; on a private server it is interrupted first."""
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
    if gone:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
        except OSError:
            pass
    else:
        try:
            client.screen.clear_status()
        except (TerminalGone, OSError):
            pass
    try:
        client.remember()
    except OSError:
        pass
    if client.busy and client.server.mode == "private":
        try:
            client._interrupt()
        except AppServerError:
            pass
    client.server.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ns = _parse_args(argv)
    if ns.legacy:
        return legacy.main([a for a in argv if a != "--legacy"])
    plan_args = list(ns.args)
    if ns.last:
        plan_args = ["resume", "--last", *(plan_args[1:] if plan_args[:1] == ["resume"] else plan_args)]
    mode, target, initial_prompt = _plan(plan_args)
    binary = os.environ.get("CODEX_REAL_BINARY") or "codex"
    client = Client(yolo=bool(ns.yolo), binary=binary, prefer_daemon=not os.environ.get("CODEX_MOBILE_PRIVATE"))
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGHUP, _on_hangup)
    signal.signal(signal.SIGTERM, _on_hangup)
    gone = False
    try:
        try:
            client.start()
        except AppServerError as exc:
            client.server.close()
            return _fallback(str(exc), argv)
        return _run(client, mode, target, initial_prompt)
    except KeyboardInterrupt:
        return 0
    except BaseException as exc:
        if not _terminal_error(exc):
            raise
        gone = True
        return 129
    finally:
        _leave(client, gone)


def _run(client: Client, mode: str, target: str | None, initial_prompt: str) -> int:
    screen = client.screen
    screen.print("═" * _width())
    via = "shared daemon" if client.server.mode == "daemon" else "private server"
    screen.print(f"{BOLD}Codex mobile{RESET} {DIM}· {via} · /help{RESET}")
    if client.server.mode != "daemon" and client.server.prefer_daemon:
        client.say(f"· no shared Codex daemon ({client.server.daemon_reason}); a turn here stops if the connection drops", DIM)
    try:
        opened = True
        if mode == "resume" and target == "--last":
            threads = client.list_threads(1)
            if not threads:
                raise AppServerError("no Codex threads to resume")
            target = str(threads[0].get("id") or "")
        if mode in ("resume", "fork") and target:
            opened = client.open_thread(target, fork=mode == "fork")
        elif mode == "auto" and not initial_prompt and sys.stdin.isatty():
            choice = client.pick_thread()
            if choice == "quit":
                return 0
            if choice:
                opened = client.open_thread(choice)
            else:
                client.new_thread()
                screen.print(f"New thread {DIM}{client.thread_id}{RESET}")
        else:
            client.new_thread()
            screen.print(f"New thread {DIM}{client.thread_id}{RESET}")
    except AppServerError as exc:
        client.error(str(exc))
        return 1
    if not opened:
        return 0 if sys.stdin.isatty() else 1
    screen.block()
    screen.print("═" * _width())

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
    return client.repl()


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared session parsing and formatting utilities.

Both claude-browse and claude-resume use these. No I/O side effects beyond
reading session JSONL files from disk. No network calls ever — by design.
"""

from __future__ import annotations

import getpass
import glob
import json
import os
from collections.abc import Iterable
from datetime import datetime, timezone

SESSIONS_DIR = os.path.expanduser("~/.claude/projects")


_NOISE_PREFIXES = (
    "<local-command",
    "<command",
    "<task-notification",
    "<system-reminder",
    "Caveat:",
    "[Request interrupted",
    # Auto-compaction inserts a synthetic user message that begins with
    # this preamble. It's machinery, not the user's voice.
    "This session is being continued from a previous conversation",
)

# Pure confirmations: recognized so they don't get picked as the "latest
# substantive user message" for the list-view topic-drift suffix.
_CONFIRMATION_WORDS = frozenset({
    "yes", "yep", "yeah", "ok", "okay", "k", "sure",
    "go ahead", "go ahead please", "yes please",
    "yes go ahead", "yes go ahead please",
    "please go ahead", "please do", "please",
    "sounds good", "looks good", "great", "perfect",
    "nice", "thanks", "thank you", "done", "cool",
    "yes thanks", "ok thanks", "yep thanks",
    "all good", "got it", "noted",
})


def _extract_text(content) -> str:
    """Pull plain text from a message's content field, regardless of shape.

    Claude Code emits content as either a string or a list of typed parts
    ({"type": "text", "text": "..."}). Returns "" if no text is present.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("text"):
                # Tool-use entries lack a top-level text key, so this naturally
                # skips them. Only "text" parts contribute to the corpus.
                if c.get("type") in (None, "text"):
                    parts.append(c["text"])
        return " ".join(parts)
    return ""


def get_session_info(jsonl_path: str) -> dict | None:
    """Extract session metadata from a session JSONL file.

    Returns None if the file is unreadable. Missing fields are returned as
    empty strings or 0 — callers should check for truthiness, not None.

    Reads three title-event shapes for compatibility:
      - "ai-title" (modern Claude Code, auto-generated, locked early)
      - "custom-title" (modern Claude Code, user-set via /name)
      - "summary" (legacy, sessionName field)
    Custom titles win over AI titles win over legacy summaries.
    """
    first_user_msg = None
    last_user_msg = None
    session_id = None
    timestamp = None
    cwd = None
    ai_title = None
    custom_title = None
    legacy_name = None
    msg_count = 0

    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                msg = data.get("message", data)
                msg_type = data.get("type", "")

                if msg_type == "custom-title":
                    custom_title = data.get("customTitle") or custom_title
                elif msg_type == "ai-title":
                    ai_title = data.get("aiTitle") or ai_title
                elif msg_type == "summary":
                    legacy_name = data.get("sessionName") or legacy_name

                if not session_id and data.get("sessionId"):
                    session_id = data.get("sessionId")
                if not cwd and data.get("cwd"):
                    cwd = data.get("cwd")
                if not timestamp and data.get("timestamp"):
                    timestamp = data.get("timestamp")

                if msg.get("role") == "user":
                    msg_count += 1
                    text = _extract_text(msg.get("content", ""))
                    if text and len(text) > 3:
                        cleaned = text.replace("\n", " ").strip()
                        is_noise = any(
                            cleaned.startswith(p) for p in _NOISE_PREFIXES
                        )
                        if not is_noise:
                            if not first_user_msg:
                                first_user_msg = cleaned
                            # Track the most recent substantive message.
                            # Skip pure confirmations like "yes" / "go ahead"
                            # since they're not topic signal.
                            stripped = cleaned.lower().strip(".,!?;: ")
                            if (
                                stripped not in _CONFIRMATION_WORDS
                                and len(cleaned) >= 25
                            ):
                                last_user_msg = cleaned
                elif msg.get("role") == "assistant":
                    msg_count += 1
    except Exception:
        return None

    name = custom_title or ai_title or legacy_name

    return {
        "path": jsonl_path,
        "session_id": session_id,
        "first_msg": (first_user_msg or "").replace("\n", " ").strip()[:200],
        "last_msg": (last_user_msg or "").replace("\n", " ").strip()[:200],
        "timestamp": timestamp,
        "cwd": cwd,
        "name": name,
        "msg_count": msg_count,
    }


def extract_search_corpus(jsonl_path: str) -> str:
    """Concatenate user + assistant message text from a session, lowercased.

    Used for keyword search. Includes both sides of the conversation so a
    query for an acronym or named concept the assistant introduced (e.g.
    "BANP", "search corpus") still finds the session. Users typically
    don't remember whether *they* or the *assistant* first said the word.

    Tool-use blocks, tool results, and system context are filtered out via
    the typed-parts shape (only `type: text` entries contribute), so paths
    in `cwd:` lines and CLAUDE.md contents do not leak in.
    """
    parts: list[str] = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                msg = data.get("message", data)
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _extract_text(msg.get("content", ""))
                if not text:
                    continue
                if role == "user":
                    cleaned = text.lstrip()
                    if any(cleaned.startswith(p) for p in _NOISE_PREFIXES):
                        continue
                parts.append(text)
    except Exception:
        return ""
    return " ".join(parts).lower()


# Backwards-compatible alias. Older callers of extract_user_text get the
# expanded corpus automatically; the rename makes the new semantics
# discoverable while keeping the old import path working.
extract_user_text = extract_search_corpus


def list_session_files() -> list[str]:
    """All session files on disk, excluding subagent-spawned ones."""
    pattern = os.path.join(SESSIONS_DIR, "*", "*.jsonl")
    return [f for f in glob.glob(pattern) if "/subagents/" not in f]


def format_date(ts: str | None) -> str:
    """Format an ISO timestamp compactly, relative to now."""
    if not ts:
        return "???"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = now - dt

        if diff.total_seconds() < 3600:
            return f"{int(diff.total_seconds() / 60)}m ago"
        elif diff.total_seconds() < 86400:
            return f"{int(diff.total_seconds() / 3600)}h ago"
        elif diff.days < 365:
            return dt.strftime("%-b %d")
        else:
            return dt.strftime("%b %Y")
    except Exception:
        return ts[:10]


def canonicalize_path(path: str | None) -> str | None:
    """Normalize a cwd across machines so the same project looks the same
    whether the session was recorded on Mac (/Users/<name>) or Linux
    (/home/<name>).

    Rules (applied in order):
      1. If path starts with /Users/<CURRENT_USER> or /home/<CURRENT_USER>,
         replace that prefix with the current $HOME.
      2. If path matches /Users/<any> or /home/<any> case-insensitively for
         the current user, same replacement.
      3. Otherwise return unchanged.

    This is the cross-machine sync feature: a Mac-recorded session cwd
    /Users/Shamanth/foo and a Linux-recorded /home/shamanth/foo both
    canonicalize to $HOME/foo, so claude-browse treats them as the same
    project.

    Honors $CLAUDE_BROWSE_PATH_ALIASES for custom mappings, formatted as:
        src1=dst1:src2=dst2
    Each alias rewrites any path starting with src1 to start with dst1.
    """
    if not path:
        return path

    home = os.path.expanduser("~")
    user = os.environ.get("USER") or getpass.getuser()

    # 1/2. Normalize Mac vs Linux home layouts to current $HOME
    for prefix in (f"/Users/{user}", f"/home/{user}"):
        if path == prefix:
            return home
        if path.startswith(prefix + "/"):
            return home + path[len(prefix):]
    # Case-insensitive match for Mac users (HFS+ often case-insensitive)
    lower = path.lower()
    for prefix in (f"/users/{user.lower()}", f"/home/{user.lower()}"):
        if lower == prefix:
            return home
        if lower.startswith(prefix + "/"):
            return home + path[len(prefix):]

    # 3. Custom aliases from env
    aliases = os.environ.get("CLAUDE_BROWSE_PATH_ALIASES", "")
    if aliases:
        for pair in aliases.split(":"):
            if "=" not in pair:
                continue
            src, dst = pair.split("=", 1)
            src, dst = src.strip(), dst.strip()
            if path == src:
                return dst
            if path.startswith(src + "/"):
                return dst + path[len(src):]

    return path


def folder_name(cwd: str | None, known_prefixes: Iterable[str] = ()) -> str:
    """Extract a short, meaningful folder name from a cwd for TUI columns.

    `known_prefixes` lets callers provide repo-specific strip rules (e.g.
    "team-operations/clients/" → show only the client name). The default
    empty tuple means: just return the last path component.
    """
    if not cwd:
        return "?"
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        rel = cwd[len(home):].strip("/")
    else:
        rel = cwd

    for prefix in known_prefixes:
        if rel.startswith(prefix):
            remainder = rel[len(prefix):].strip("/")
            if remainder:
                return remainder.split("/")[0]
            return prefix.rstrip("/").rsplit("/", 1)[-1]

    return rel.rstrip("/").rsplit("/", 1)[-1] if "/" in rel else (rel or "?")


def extract_query_terms(query: str) -> list[str]:
    """Pull the literal search terms out of a user query.

    Mirrors fts.normalize_query's parser, but returns plain strings suitable
    for case-insensitive substring highlighting (not FTS5 syntax). Bare words
    each become a term; double-quoted spans become phrase terms — so a query
    of `postiz "tiktok ads"` returns ["postiz", "tiktok ads"].
    """
    terms: list[str] = []
    in_quote = False
    current: list[str] = []
    for ch in query:
        if ch == '"':
            if current:
                terms.append("".join(current).strip())
                current = []
            in_quote = not in_quote
        elif ch.isspace() and not in_quote:
            if current:
                terms.append("".join(current).strip())
                current = []
        else:
            current.append(ch)
    if current:
        terms.append("".join(current).strip())
    return [t for t in terms if t]


def highlight_terms(
    text: str,
    terms: list[str],
    open_marker: str = "\033[1;33m",
    close_marker: str = "\033[0m",
) -> str:
    """Wrap each case-insensitive occurrence of any term with ANSI markers.

    Longer terms match first so a phrase like "tiktok ads" highlights as one
    span instead of "tiktok" leaving an orphaned " ads" beside it.
    """
    if not terms or not text:
        return text
    import re

    ordered = sorted({t for t in terms if t}, key=len, reverse=True)
    if not ordered:
        return text
    pattern = re.compile("|".join(re.escape(t) for t in ordered), re.IGNORECASE)
    return pattern.sub(
        lambda m: f"{open_marker}{m.group(0)}{close_marker}", text
    )


def display_cwd(cwd: str | None) -> str:
    """Home-abbreviated full path for display. ~/foo/bar style."""
    if not cwd:
        return ""
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd

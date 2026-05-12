"""Shared session parsing and formatting utilities.

Used by the browser entrypoints. No I/O side effects beyond reading local
session data from disk. No network calls ever — by design.
"""

from __future__ import annotations

import getpass
import glob
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone

SESSIONS_DIR = os.path.expanduser("~/.claude/projects")
CODEX_STATE_DB = os.path.expanduser("~/.codex/state_5.sqlite")
CODEX_HISTORY_PATH = os.path.expanduser("~/.codex/history.jsonl")

# Toggl-style rollup lines ('- musopia: 1.0h', '* maxrewards: 0.5h') are
# templated noise: they mention every client equally and dominate FTS hits
# for client names. Routed to a separate low-weight index column rather than
# stripped, so a deliberate query like '0.5h' still retrieves them.
_BOILERPLATE_RE = re.compile(
    r"^\s*[-*]\s*[\w][\w\s.&'-]*?:\s*\d+(?:\.\d+)?\s*h\s*$"
)


def _is_boilerplate_line(line: str) -> bool:
    return bool(_BOILERPLATE_RE.match(line))


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

_CODEX_HISTORY_CACHE: dict[str, object] = {
    "mtime": None,
    "entries": {},
}


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


def _flatten_text(text: str) -> str:
    return text.replace("\n", " ").strip()


def _is_noise_text(cleaned: str) -> bool:
    return any(cleaned.startswith(p) for p in _NOISE_PREFIXES)


def _is_substantive_text(cleaned: str) -> bool:
    stripped = cleaned.lower().strip(".,!?;: ")
    return (
        stripped not in _CONFIRMATION_WORDS
        and len(cleaned) >= 25
        and not _is_noise_text(cleaned)
    )


def _split_boilerplate(text: str) -> tuple[str, list[str]]:
    keep_lines: list[str] = []
    boilerplate: list[str] = []
    for line in text.split("\n"):
        if _is_boilerplate_line(line):
            boilerplate.append(line.strip())
        else:
            keep_lines.append(line)
    return ("\n".join(keep_lines).strip(), boilerplate)


def get_session_info(jsonl_path: str) -> dict | None:
    """Extract session metadata from a Claude session JSONL file.

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
    last_timestamp = None
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
                if data.get("timestamp"):
                    if not timestamp:
                        timestamp = data.get("timestamp")
                    last_timestamp = data.get("timestamp")

                if msg.get("role") == "user":
                    msg_count += 1
                    text = _extract_text(msg.get("content", ""))
                    if text and len(text) > 3:
                        cleaned = _flatten_text(text)
                        if not _is_noise_text(cleaned):
                            if not first_user_msg:
                                first_user_msg = cleaned
                            if _is_substantive_text(cleaned):
                                last_user_msg = cleaned
                elif msg.get("role") == "assistant":
                    msg_count += 1
    except Exception:
        return None

    name = custom_title or ai_title or legacy_name

    return {
        "path": jsonl_path,
        "provider": "claude",
        "session_id": session_id,
        "first_msg": (first_user_msg or "").strip()[:200],
        "last_msg": (last_user_msg or "").strip()[:200],
        "timestamp": timestamp,
        "last_timestamp": last_timestamp,
        "cwd": cwd,
        "name": name,
        "msg_count": msg_count,
    }


def extract_fielded_corpus(jsonl_path: str) -> dict[str, str]:
    """Extract per-field text for the multi-column FTS index.

    Splits a Claude session's text into named fields so the ranker can weight
    each one differently (cwd is a strong topic anchor, assistant text is the
    weakest signal, etc.):

      cwd         - working directory the session was started in
      title       - custom-title > ai-title > legacy summary
      first_msg   - the first substantive user message (the brief)
      user_text   - subsequent user messages, minus boilerplate lines
      asst_text   - all assistant message text
      boilerplate - Toggl-style rollup lines split out from user_text

    All fields lowercased. cwd kept raw (slashes/dashes); FTS5's unicode61
    tokenizer handles path component splitting.
    """
    cwd = ""
    title_custom = ""
    title_ai = ""
    title_legacy = ""
    first_msg = ""
    first_user_seen = False
    user_parts: list[str] = []
    asst_parts: list[str] = []
    boilerplate_parts: list[str] = []

    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                if not cwd and data.get("cwd"):
                    cwd = data["cwd"]

                msg_type = data.get("type", "")
                if msg_type == "custom-title":
                    title_custom = data.get("customTitle") or title_custom
                elif msg_type == "ai-title":
                    title_ai = data.get("aiTitle") or title_ai
                elif msg_type == "summary":
                    title_legacy = data.get("sessionName") or title_legacy

                msg = data.get("message", data)
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue

                text = _extract_text(msg.get("content", ""))
                if not text:
                    continue

                if role == "user":
                    cleaned = text.lstrip()
                    if _is_noise_text(cleaned):
                        continue
                    body, boilerplate = _split_boilerplate(text)
                    boilerplate_parts.extend(boilerplate)
                    if not body:
                        continue
                    if not first_user_seen:
                        first_msg = body
                        first_user_seen = True
                    else:
                        user_parts.append(body)
                else:
                    asst_parts.append(text)
    except Exception:
        return {
            "cwd": "", "title": "", "first_msg": "",
            "user_text": "", "asst_text": "", "boilerplate": "",
        }

    title = title_custom or title_ai or title_legacy or ""
    return {
        "cwd": cwd,
        "title": title.lower(),
        "first_msg": first_msg.lower(),
        "user_text": " ".join(user_parts).lower(),
        "asst_text": " ".join(asst_parts).lower(),
        "boilerplate": " ".join(boilerplate_parts).lower(),
    }


def extract_search_corpus(jsonl_path: str) -> str:
    """Backward-compat: returns the user + assistant text concatenated.

    Equivalent to the old single-blob corpus. Used by the test helper and
    any external caller; new internal code uses extract_fielded_corpus().
    """
    fields = extract_fielded_corpus(jsonl_path)
    return " ".join(
        v for k, v in fields.items()
        if k in ("first_msg", "user_text", "asst_text") and v
    )


# Backwards-compatible alias. Older callers of extract_user_text get the
# expanded corpus automatically; the rename makes the new semantics
# discoverable while keeping the old import path working.
extract_user_text = extract_search_corpus


def list_session_files() -> list[str]:
    """All Claude session files on disk, excluding subagent-spawned ones."""
    pattern = os.path.join(SESSIONS_DIR, "*", "*.jsonl")
    return [f for f in glob.glob(pattern) if "/subagents/" not in f]


def _epoch_ms_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(
            value / 1000.0, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _epoch_s_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(
            float(value), tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _load_codex_history() -> dict[str, list[dict[str, object]]]:
    """Parse ~/.codex/history.jsonl once per mtime and cache the result."""
    if not os.path.exists(CODEX_HISTORY_PATH):
        return {}

    mtime = os.path.getmtime(CODEX_HISTORY_PATH)
    cached_mtime = _CODEX_HISTORY_CACHE.get("mtime")
    if cached_mtime == mtime:
        return _CODEX_HISTORY_CACHE["entries"]  # type: ignore[return-value]

    entries: dict[str, list[dict[str, object]]] = {}
    try:
        with open(CODEX_HISTORY_PATH) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                sid = data.get("session_id")
                text = data.get("text")
                if not sid or not isinstance(text, str) or not text.strip():
                    continue
                entries.setdefault(sid, []).append({
                    "text": text,
                    "ts": data.get("ts"),
                })
    except Exception:
        entries = {}

    _CODEX_HISTORY_CACHE["mtime"] = mtime
    _CODEX_HISTORY_CACHE["entries"] = entries
    return entries


def _list_codex_index_records() -> list[dict]:
    """Return Codex threads normalized into the shared index-record shape."""
    if not os.path.exists(CODEX_STATE_DB):
        return []

    history = _load_codex_history()
    history_mtime = (
        os.path.getmtime(CODEX_HISTORY_PATH)
        if os.path.exists(CODEX_HISTORY_PATH)
        else 0.0
    )
    state_mtime = os.path.getmtime(CODEX_STATE_DB)

    conn = sqlite3.connect(CODEX_STATE_DB)
    try:
        rows = conn.execute(
            """
            SELECT id, cwd, title, first_user_message, created_at_ms,
                   updated_at_ms, created_at, updated_at
            FROM threads
            WHERE COALESCE(thread_source, '') != 'subagent'
            ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC
            """
        ).fetchall()
    finally:
        conn.close()

    records: list[dict] = []
    for row in rows:
        sid, cwd, title, first_user_message, created_ms, updated_ms, created_s, updated_s = row
        events = history.get(sid, [])
        event_texts = [str(e.get("text", "")) for e in events]

        msg_count = len(events)
        first_msg = _flatten_text(first_user_message or "").strip()
        if not first_msg:
            for text in event_texts:
                cleaned = _flatten_text(text)
                if cleaned and not _is_noise_text(cleaned):
                    first_msg = cleaned
                    break

        last_msg = ""
        user_parts: list[str] = []
        boilerplate_parts: list[str] = []
        for text in event_texts:
            cleaned = _flatten_text(text)
            if not cleaned or _is_noise_text(cleaned):
                continue
            body, boilerplate = _split_boilerplate(text)
            if not body:
                continue
            user_parts.append(body)
            boilerplate_parts.extend(boilerplate)
            body_flat = _flatten_text(body)
            if _is_substantive_text(body_flat):
                last_msg = body_flat

        created_at = _epoch_ms_to_iso(created_ms) or _epoch_s_to_iso(created_s)
        updated_at = _epoch_ms_to_iso(updated_ms) or _epoch_s_to_iso(updated_s)
        mtime = max(state_mtime, history_mtime)

        records.append({
            "path": f"codex://{sid}",
            "provider": "codex",
            "session_id": sid,
            "first_msg": first_msg[:200],
            "last_msg": last_msg[:200],
            "timestamp": created_at,
            "last_timestamp": updated_at,
            "cwd": canonicalize_path(cwd),
            "name": (title or "").strip() or first_msg[:200],
            "msg_count": msg_count,
            "mtime": mtime,
            "fields": {
                "cwd": (cwd or "").lower(),
                "title": (title or "").lower(),
                "first_msg": first_msg.lower(),
                "user_text": " ".join(user_parts).lower(),
                "asst_text": "",
                "boilerplate": " ".join(boilerplate_parts).lower(),
            },
        })

    return records


def list_index_records() -> list[dict]:
    """Return every Claude and Codex session in one normalized record shape."""
    records: list[dict] = []

    for filepath in list_session_files():
        info = get_session_info(filepath)
        if not info or not info.get("session_id") or not info.get("first_msg"):
            continue
        records.append({
            **info,
            "provider": "claude",
            "cwd": canonicalize_path(info.get("cwd")),
            "mtime": os.path.getmtime(filepath),
            "fields": extract_fielded_corpus(filepath),
        })

    records.extend(_list_codex_index_records())
    return records


def _claude_user_preview_messages(path: str) -> list[tuple[int, str]]:
    messages: list[tuple[int, str]] = []
    msg_num = 0

    try:
        with open(path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                msg = data.get("message", data)
                if msg.get("role") != "user":
                    continue
                msg_num += 1
                text = _extract_text(msg.get("content", ""))
                if not text:
                    continue
                cleaned = _flatten_text(text)
                if len(cleaned) <= 3 or _is_noise_text(cleaned):
                    continue
                messages.append((msg_num, cleaned[:140]))
    except Exception:
        return []

    return messages


def _codex_user_preview_messages(session_id: str) -> list[tuple[int, str]]:
    messages: list[tuple[int, str]] = []
    for idx, entry in enumerate(_load_codex_history().get(session_id, []), 1):
        text = str(entry.get("text", ""))
        cleaned = _flatten_text(text)
        if len(cleaned) <= 3 or _is_noise_text(cleaned):
            continue
        messages.append((idx, cleaned[:140]))
    return messages


def get_preview_messages(provider: str, path: str, session_id: str) -> list[tuple[int, str]]:
    """Latest user-message preview corpus for a session, provider-aware."""
    if provider == "codex":
        return _codex_user_preview_messages(session_id)
    return _claude_user_preview_messages(path)


def _claude_transcript_excerpt(path: str, limit: int = 24) -> list[tuple[str, str]]:
    return _claude_transcript_turns(path)[-limit:]


def _claude_transcript_turns(path: str) -> list[tuple[str, str]]:
    excerpt: list[tuple[str, str]] = []

    try:
        with open(path) as f:
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
                cleaned = _flatten_text(text)
                if len(cleaned) <= 3:
                    continue
                if role == "user" and _is_noise_text(cleaned):
                    continue
                excerpt.append((role, cleaned))
    except Exception:
        return []

    return excerpt


def _codex_transcript_excerpt(session_id: str, limit: int = 24) -> list[tuple[str, str]]:
    return _codex_transcript_turns(session_id)[-limit:]


def _codex_transcript_turns(session_id: str) -> list[tuple[str, str]]:
    excerpt: list[tuple[str, str]] = []
    for entry in _load_codex_history().get(session_id, []):
        cleaned = _flatten_text(str(entry.get("text", "")))
        if len(cleaned) <= 3 or _is_noise_text(cleaned):
            continue
        excerpt.append(("user", cleaned))
    return excerpt


def _latest_turn_text(excerpt: list[tuple[str, str]], role: str) -> str:
    for turn_role, text in reversed(excerpt):
        if turn_role == role:
            return text
    return ""


def _matching_turns(
    provider: str,
    path: str,
    session_id: str,
    selection_query: str,
    limit: int = 6,
) -> list[tuple[str, str]]:
    terms = [term.lower() for term in extract_query_terms(selection_query) if term.strip()]
    if not terms:
        return []

    if provider == "codex":
        turns = _codex_transcript_turns(session_id)
    else:
        turns = _claude_transcript_turns(path)

    matched = [
        (role, text)
        for role, text in turns
        if any(term in text.lower() for term in terms)
    ]
    return list(reversed(matched[-limit:]))


def build_import_markdown(
    session: dict,
    target_provider: str,
    selection_query: str | None = None,
) -> str:
    """Create a compact Markdown brief so another app can continue a thread."""
    provider = session.get("provider") or "claude"
    session_id = session.get("session_id") or "?"
    cwd = session.get("cwd") or ""
    started = session.get("timestamp") or "unknown"
    last_activity = session.get("last_timestamp") or started
    title = session.get("name") or session.get("first_msg") or ""
    first_msg = session.get("first_msg") or ""
    last_msg = session.get("last_msg") or ""
    target_name = provider_display_name(target_provider)
    path = session.get("path") or ""

    if provider == "codex":
        excerpt = _codex_transcript_excerpt(session_id)
    else:
        excerpt = _claude_transcript_excerpt(path)
    recent_excerpt = list(reversed(excerpt[-10:]))
    latest_assistant = _latest_turn_text(excerpt, "assistant")
    matched_turns = _matching_turns(
        provider,
        path,
        session_id,
        selection_query or "",
    )

    lines = [
        "# Imported Session Context",
        "",
        (
            "This is prior conversation context being handed into a new "
            f"{target_name} session."
        ),
        "It is not a native cross-vendor resume.",
        (
            "Prioritize the end-of-thread state and most recent turns below "
            "over the original opening prompt if they differ."
        ),
        "",
        f"- Source app: {provider_display_name(provider)}",
        f"- Original session id: `{session_id}`",
        f"- Original folder: `{cwd}`" if cwd else "- Original folder: unknown",
        f"- Started: {started}",
        f"- Last activity: {last_activity}",
    ]

    if selection_query and selection_query.strip():
        lines.extend([
            "",
            "## Reopen Intent",
            "",
            (
                f"- This thread was reopened from a search for: "
                f"`{selection_query.strip()}`"
            ),
        ])
        if matched_turns:
            lines.extend([
                "- Matching turns below are likely why this thread was selected.",
                "",
            ])
            for role, text in matched_turns:
                label = "User" if role == "user" else "Assistant"
                lines.append(f"### {label}")
                lines.append(text)
                lines.append("")
        else:
            lines.extend([
                "- No exact matching transcript turns were recovered for that query.",
                "",
            ])

    lines.extend([
        "",
        "## End-of-Thread Priority",
        "",
    ])

    if last_msg:
        lines.append(f"- Latest substantive user message: {last_msg}")
    if latest_assistant:
        lines.append(f"- Latest assistant response: {latest_assistant}")
    if title:
        lines.append(f"- Original session title or topic: {title}")
    if first_msg:
        lines.append(f"- Original first user prompt: {first_msg}")

    lines.extend([
        "",
        "## Most Recent Transcript Turns",
        "",
    ])

    if not recent_excerpt:
        lines.append("No recent transcript excerpt was available.")
        return "\n".join(lines) + "\n"

    if provider == "codex":
        lines.append("Note: Codex local history only exposes user turns here.")
        lines.append("Most recent turns are shown first.")
        lines.append("")
    else:
        lines.append("Most recent turns are shown first.")
        lines.append("")

    for role, text in recent_excerpt:
        label = "User" if role == "user" else "Assistant"
        lines.append(f"### {label}")
        lines.append(text)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_import_file(
    session: dict,
    target_provider: str,
    selection_query: str | None = None,
) -> str:
    """Write a temporary Markdown import brief and return its path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix="claude_browse_import_",
        delete=False,
    )
    try:
        tmp.write(build_import_markdown(session, target_provider, selection_query))
    finally:
        tmp.close()
    return tmp.name


def build_codex_import_markdown(session: dict) -> str:
    """Backward-compatible wrapper for the original one-way handoff helper."""
    return build_import_markdown(session, "codex")


def write_codex_import_file(session: dict) -> str:
    """Backward-compatible wrapper for the original one-way handoff helper."""
    return write_import_file(session, "codex")


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


def provider_display_name(provider: str | None) -> str:
    if provider == "codex":
        return "CodeX"
    return "Claude"


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
    import re as _re

    ordered = sorted({t for t in terms if t}, key=len, reverse=True)
    if not ordered:
        return text
    pattern = _re.compile("|".join(_re.escape(t) for t in ordered), _re.IGNORECASE)
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

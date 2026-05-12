"""Shared session parsing and formatting utilities.

Used by the browser entrypoints. No I/O side effects beyond reading local
session data from disk. No network calls ever — by design.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone

from .providers import claude as claude_provider
from .providers import codex as codex_provider
from .providers import common as provider_common
from .providers import copilot as copilot_provider
from .providers import gemini as gemini_provider
from .providers import get_provider, provider_ids

SESSIONS_DIR = claude_provider.SESSIONS_DIR
CODEX_STATE_DB = codex_provider.CODEX_STATE_DB
CODEX_HISTORY_PATH = codex_provider.CODEX_HISTORY_PATH
_CODEX_HISTORY_CACHE = codex_provider._CODEX_HISTORY_CACHE
GEMINI_TMP_DIR = gemini_provider.GEMINI_TMP_DIR
COPILOT_HOME = copilot_provider.COPILOT_HOME
canonicalize_path = provider_common.canonicalize_path


def get_session_info(jsonl_path: str) -> dict | None:
    """Backward-compatible Claude session metadata wrapper."""
    return get_provider("claude").session_info(jsonl_path)


def extract_fielded_corpus(jsonl_path: str) -> dict[str, str]:
    """Backward-compatible Claude fielded-corpus wrapper."""
    return get_provider("claude").fielded_corpus(jsonl_path)


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
    return get_provider("claude").session_files()


def _load_codex_history() -> dict[str, list[dict[str, object]]]:
    """Backward-compatible wrapper for the CodeX history cache."""
    return codex_provider.load_history()


def _list_codex_index_records() -> list[dict]:
    """Backward-compatible wrapper for CodeX index records."""
    return get_provider("codex").list_index_records()


def list_index_records() -> list[dict]:
    """Return every source-capable provider session in one normalized record shape."""
    records: list[dict] = []
    for provider in provider_ids(source_capable=True):
        records.extend(get_provider(provider).list_index_records())
    return records


def _claude_user_preview_messages(path: str) -> list[tuple[int, str]]:
    return get_provider("claude").preview_messages(path, "")


def _codex_user_preview_messages(session_id: str) -> list[tuple[int, str]]:
    return get_provider("codex").preview_messages("", session_id)


def get_preview_messages(provider: str, path: str, session_id: str) -> list[tuple[int, str]]:
    """Latest user-message preview corpus for a session, provider-aware."""
    return get_provider(provider).preview_messages(path, session_id)


def _claude_transcript_excerpt(path: str, limit: int = 24) -> list[tuple[str, str]]:
    return get_provider("claude").transcript_excerpt(path, "", limit)


def _claude_transcript_turns(path: str) -> list[tuple[str, str]]:
    return get_provider("claude").transcript_turns(path, "")


def _codex_transcript_excerpt(session_id: str, limit: int = 24) -> list[tuple[str, str]]:
    return get_provider("codex").transcript_excerpt("", session_id, limit)


def _codex_transcript_turns(session_id: str) -> list[tuple[str, str]]:
    return get_provider("codex").transcript_turns("", session_id)


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

    turns = get_provider(provider).transcript_turns(path, session_id)

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
    source_spec = get_provider(provider)
    excerpt = source_spec.transcript_excerpt(path, session_id)
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

    if not source_spec.assistant_turns_available:
        lines.append(
            f"Note: {provider_display_name(provider)} local history only exposes user turns here."
        )
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
    try:
        return get_provider(provider).display_name
    except ValueError:
        return str(provider or "Claude")


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

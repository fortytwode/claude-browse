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
from .query import significant_query_terms
from .work_state import (
    build_work_state,
    render_restart_card_markdown,
    render_status_update_markdown,
)

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


def list_index_records(
    known_sessions: dict[str, tuple[str, float]] | None = None,
) -> list[dict]:
    """Return every source-capable provider session in one normalized record shape.

    known_sessions maps already-indexed path -> (sid, mtime); providers use
    it to skip parsing unchanged files and emit stub records instead.
    """
    records: list[dict] = []
    for provider in provider_ids(source_capable=True):
        records.extend(
            get_provider(provider).list_index_records(known_sessions=known_sessions)
        )
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


# Recent-turns window for the handoff brief. Relocate widens it because a
# relocated session starts fresh with no native transcript to fall back on, so
# more of the conversation should ride inline (the full transcript is also
# linked for on-demand reads).
_DEFAULT_RECENT_LIMIT = 10
_RELOCATE_RECENT_LIMIT = 25


def build_import_markdown(
    session: dict,
    target_provider: str,
    selection_query: str | None = None,
    *,
    reenter_topic: bool = False,
    relocate: bool = False,
) -> str:
    """Create a compact Markdown brief so another app can continue a thread.

    When ``relocate`` is set, the brief also points the new session at the full
    original transcript on disk (so it can pull deeper history on demand) and
    widens the inline recent-turns window.
    """
    provider = session.get("provider") or "claude"
    session_id = session.get("session_id") or "?"
    cwd = session.get("cwd") or ""
    transcript_path = session.get("path") or ""
    started = session.get("timestamp") or "unknown"
    last_activity = session.get("last_timestamp") or started
    target_name = provider_display_name(target_provider)
    source_spec = get_provider(provider)
    work_state = build_work_state(
        session,
        selection_query or "",
        recent_limit=_RELOCATE_RECENT_LIMIT if relocate else _DEFAULT_RECENT_LIMIT,
        match_limit=6,
    )
    recent_excerpt = work_state["recent_turns"]
    matched_turns = work_state.get("matched_exchange") or work_state["matching_turns"]

    lines = [
        "# Imported Session Context",
        "",
    ]
    if reenter_topic:
        lines.extend([
            (
                "This is prior conversation context being handed into a new "
                f"{target_name} session so you can re-enter an earlier topic."
            ),
            "It is not a native rewind of the original thread.",
            (
                "Prioritize the matched exchange and Reopen Intent below. "
                "Use later turns only as context about how the thread evolved afterward."
            ),
        ])
    else:
        lines.extend([
            (
                "This is prior conversation context being handed into a new "
                f"{target_name} session."
            ),
            "It is not a native cross-vendor resume.",
            (
                "Prioritize the end-of-thread state and most recent turns below "
                "over the original opening prompt if they differ."
            ),
        ])
    lines.extend([
        "",
        f"- Source app: {provider_display_name(provider)}",
        f"- Original session id: `{session_id}`",
        f"- Original folder: `{cwd}`" if cwd else "- Original folder: unknown",
        f"- Started: {started}",
        f"- Last activity: {last_activity}",
    ])
    if relocate:
        lines.extend([
            "",
            "## Resuming Here (relocated)",
            "",
            (
                f"- Previous folder: `{cwd}`"
                if cwd
                else "- Previous folder: unknown"
            ),
        ])
        if transcript_path:
            lines.append(
                f"- Full original transcript: `{transcript_path}` — read it if you "
                "need detail beyond the recent turns below."
            )
    lines.extend([""] + render_restart_card_markdown(work_state))

    if selection_query and selection_query.strip():
        lines.extend([
            "",
            "## Reopen Intent",
            "",
            (
                f"- This thread was reopened from a search for: `{selection_query.strip()}`"
            ),
        ])
        if reenter_topic:
            lines.append("- Use the matching exchange below as the point to re-enter.")
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


def build_handoff_markdown(
    session: dict,
    selection_query: str | None = None,
    *,
    reenter_topic: bool = False,
) -> str:
    """Create a reusable human-readable handoff brief from one session."""
    provider = session.get("provider") or "claude"
    session_id = session.get("session_id") or "?"
    cwd = session.get("cwd") or ""
    started = session.get("timestamp") or "unknown"
    last_activity = session.get("last_timestamp") or started
    work_state = build_work_state(
        session,
        selection_query or "",
        recent_limit=10,
        match_limit=6,
    )
    recent_excerpt = work_state["recent_turns"]
    matched_turns = work_state.get("matched_exchange") or work_state["matching_turns"]

    lines = [
        "# Resume Work Handoff",
        "",
        f"- Source app: {provider_display_name(provider)}",
        f"- Original session id: `{session_id}`",
        f"- Original folder: `{cwd}`" if cwd else "- Original folder: unknown",
        f"- Started: {started}",
        f"- Last activity: {last_activity}",
        "",
    ]

    lines.extend(render_status_update_markdown(work_state))
    lines.extend(render_restart_card_markdown(work_state))

    if selection_query and selection_query.strip():
        lines.extend(
            [
                "",
                "## Reopen Intent",
                "",
                f"- This thread was reopened from a search for: `{selection_query.strip()}`",
            ]
        )
        if reenter_topic:
            lines.append("- Use the matching exchange below as the point to re-enter.")
        if matched_turns:
            lines.extend(
                [
                    "- Matching turns below are likely why this thread was selected.",
                    "",
                ]
            )
            for role, text in matched_turns:
                label = "User" if role == "user" else "Assistant"
                lines.append(f"### {label}")
                lines.append(text)
                lines.append("")

    lines.extend(
        [
            "",
            "## Most Recent Transcript Turns",
            "",
        ]
    )
    if not recent_excerpt:
        lines.append("No recent transcript excerpt was available.")
        return "\n".join(lines).rstrip() + "\n"

    source_spec = get_provider(provider)
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
    *,
    reenter_topic: bool = False,
    relocate: bool = False,
) -> str:
    """Write a temporary Markdown import brief and return its path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix="claude_browse_import_",
        delete=False,
    )
    try:
        tmp.write(
            build_import_markdown(
                session,
                target_provider,
                selection_query,
                reenter_topic=reenter_topic,
                relocate=relocate,
            )
        )
    finally:
        tmp.close()
    return tmp.name


def build_codex_import_markdown(session: dict) -> str:
    """Backward-compatible wrapper for the original one-way handoff helper."""
    return build_import_markdown(session, "codex")


def write_codex_import_file(session: dict) -> str:
    """Backward-compatible wrapper for the original one-way handoff helper."""
    return write_import_file(session, "codex")


def parse_ts(ts: str | None) -> datetime | None:
    """Parse a stored ISO timestamp as an aware UTC datetime."""
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_local(ts: str | None) -> str:
    """Render a stored UTC timestamp in the viewer's local timezone.

    Stored timestamps are UTC ("...Z"). Slicing the ISO string and swapping
    the T -- what the preview used to do -- printed UTC while reading as
    local time, so every absolute time was off by the viewer's whole offset
    (5h30m in IST, enough to make a live thread look hours idle).
    """
    parsed = parse_ts(ts)
    if parsed is None:
        return ts[:19].replace("T", " ") if ts else "???"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def newest_ts(ts: str | None, mtime: float | None) -> str | None:
    """Pick whichever of an indexed timestamp and a file mtime is newer.

    The picker paints from the previous index and refreshes in a detached
    child, so a session being actively written reports the age it had at
    the last completed index pass. The file's mtime costs a stat() and is
    always current, so it wins whenever it is ahead.
    """
    if not mtime:
        return ts
    from_mtime = datetime.fromtimestamp(mtime, timezone.utc)
    parsed = parse_ts(ts)
    if parsed is not None and parsed >= from_mtime:
        return ts
    return from_mtime.isoformat().replace("+00:00", "Z")


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

    Longer descriptive queries drop filler like "find me the thread where"
    so highlighting and transcript matching stay anchored on the specific
    names, phrases, and nouns the user actually cares about.
    """
    return significant_query_terms(query)


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

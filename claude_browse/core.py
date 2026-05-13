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


def build_import_markdown(
    session: dict,
    target_provider: str,
    selection_query: str | None = None,
    *,
    reenter_topic: bool = False,
) -> str:
    """Create a compact Markdown brief so another app can continue a thread."""
    provider = session.get("provider") or "claude"
    session_id = session.get("session_id") or "?"
    cwd = session.get("cwd") or ""
    started = session.get("timestamp") or "unknown"
    last_activity = session.get("last_timestamp") or started
    target_name = provider_display_name(target_provider)
    source_spec = get_provider(provider)
    work_state = build_work_state(
        session,
        selection_query or "",
        recent_limit=10,
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

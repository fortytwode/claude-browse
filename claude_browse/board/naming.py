"""Auto-namer: reflects what a session is CURRENTLY about, not just its
opening prompt.

Runs out-of-band only, invoked from board/sync.py -- never from the hot
hook path (board/hook.py).

Design note: an earlier version of this permanently preferred Claude
Code's own ai-title (or a one-time Haiku name) forever once set. That's
wrong for long-running sessions whose topic drifts a lot -- a session
that opened as "continue this imported context" can, dozens of turns
later, be deep in building an entirely different feature, and the frozen
opening-derived title stops meaning anything (found from live use: this
exact codebase's own board showed a stale, uninformative name for its own
multi-hour build). So naming now periodically re-synthesizes from RECENT
conversation content once a session has grown enough since it was last
named, rather than locking a name in forever.
"""

from __future__ import annotations

import os

from claude_browse.board import store
from claude_browse.providers.claude import (
    get_session_info,
    list_session_files,
    transcript_turns,
)

_MODEL = "claude-haiku-4-5-20251001"

#: Re-synthesize the name once at least this many new messages have
#: accumulated since the last naming pass. Frequent enough to catch topic
#: drift within a session; infrequent enough that the API cost stays
#: negligible and the board name doesn't flicker every turn.
_REFRESH_AFTER_MSGS = 20

#: How many of the most recent turns to feed the model when refreshing --
#: "what's happening lately", not the whole transcript.
_RECENT_TURNS = 4
_RECENT_CHARS_PER_TURN = 300


def _find_jsonl_path(session_id: str) -> str | None:
    """Locate a session's jsonl by id.

    Uses list_session_files() rather than a plain glob over SESSIONS_DIR --
    it already unions that glob with a history.jsonl-based recovery path for
    sessions whose project directory was renamed or moved, which a narrower
    glob would silently miss.
    """
    target = f"{session_id}.jsonl"
    for path in list_session_files():
        if os.path.basename(path) == target:
            return path
    return None


def _get_client():
    try:
        from team_ops.ai_usage import tracked_anthropic_client

        return tracked_anthropic_client("agent_board")
    except Exception:
        import anthropic

        return anthropic.Anthropic()


def _naming_context(path: str, info: dict) -> str:
    """Opening + recent turns, so the model sees the thread's whole arc.

    An earlier version fed ONLY the last few turns, which over-corrected
    the original frozen-on-the-opening-prompt bug: a 900-message session
    about building a feature got named after whatever micro-task happened
    in the final turns ("adding missing problem statement section") --
    real user feedback. The name must capture the overall topic, weighted
    toward where the work ended up, so the model needs both ends of the
    thread.
    """
    parts = []
    first_msg = (info.get("first_msg") or "").strip()
    if first_msg:
        parts.append(f"how the session started: {first_msg[:_RECENT_CHARS_PER_TURN]}")
    turns = transcript_turns(path, "")
    recent = turns[-_RECENT_TURNS:]
    if recent:
        parts.append("most recent exchanges:")
        parts.extend(f"{role}: {text[:_RECENT_CHARS_PER_TURN]}" for role, text in recent)
    return "\n".join(parts)


def compute_name(session_id: str, *, info: dict | None = None) -> str | None:
    """Best-effort name reflecting the session's overall topic. Never raises.

    A short/fresh session reuses Claude Code's own ai-title if present (free,
    usually still accurate). A session that has grown past the refresh
    threshold -- or has no existing title at all -- gets a fresh Haiku
    synthesis from its opening PLUS its most recent turns, so the name
    reflects the thread's overall arc (weighted toward where the work has
    ended up), not the opening prompt frozen forever and not just the very
    latest exchange.
    """
    path = _find_jsonl_path(session_id)
    if not path:
        return None

    if info is None:
        info = get_session_info(path)
    if not info:
        return None

    existing_title = info.get("name")
    msg_count = info.get("msg_count") or 0

    if existing_title and msg_count < _REFRESH_AFTER_MSGS:
        return existing_title

    prompt_body = _naming_context(path, info)
    if not prompt_body:
        return existing_title

    try:
        client = _get_client()
        response = client.messages.create(
            model=_MODEL,
            max_tokens=20,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "This shows how an ongoing coding-assistant session started "
                        "and its most recent exchanges. Name the session's OVERALL "
                        "topic in 4-6 words, lowercase, no punctuation, no quotes. "
                        "The name must say what the thread as a whole is about -- "
                        "weighted toward where the work has ended up if the topic "
                        "drifted, but never just the very latest exchange or "
                        "micro-task:\n\n" + prompt_body
                    ),
                }
            ],
        )
        text = response.content[0].text.strip().strip('"').lower()
        return text or existing_title
    except Exception:
        return existing_title


def maybe_name(session_id: str) -> None:
    """Name or re-name a session if it's never been named, or has grown
    enough since the last naming pass to be worth refreshing."""
    row = store.get(session_id)
    if row is None:
        return

    path = _find_jsonl_path(session_id)
    if not path:
        return
    info = get_session_info(path)
    if not info:
        return

    msg_count = info.get("msg_count") or 0
    named_at = row.get("named_at_msg_count")

    already_named = row.get("name_source") == "haiku"
    grown_enough = named_at is None or (msg_count - named_at) >= _REFRESH_AFTER_MSGS
    if already_named and not grown_enough:
        return

    name = compute_name(session_id, info=info)
    if name:
        store.upsert(session_id, name=name, name_source="haiku", named_at_msg_count=msg_count)

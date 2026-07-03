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


#: A name is a terse label, not a sentence. Anything outside these bounds is
#: model misbehavior (preamble, explanation, truncated rambling) and must be
#: rejected rather than stored -- the name goes verbatim onto the statusline,
#: the macOS banner, and the Slack alert, so verbosity there is user-facing.
_NAME_MAX_WORDS = 8
_NAME_MAX_CHARS = 60


def _clean_name(raw: str) -> str | None:
    """Normalize a model response into a valid name, or None if it isn't one.

    Defense in depth behind the prompt/prefill: strips quotes and any
    'the topic is:'-style preamble (take what follows the last colon),
    drops leading slash-command tokens leaked from transcripts, then
    rejects anything that still doesn't look like a terse label.
    """
    text = raw.strip().strip('"').strip("'")
    if ":" in text:
        text = text.rsplit(":", 1)[1]
    words = [w for w in text.split() if w]
    while words and not words[0][0].isalnum():
        words.pop(0)
    text = " ".join(words).strip(" .").lower()
    if not (2 <= len(text.split()) <= _NAME_MAX_WORDS) or len(text) > _NAME_MAX_CHARS:
        return None
    return text


def _naming_context(path: str, info: dict) -> str:
    """Turns sampled across the WHOLE thread, so the model sees the arc.

    Naming has failed twice in the same direction now (real user feedback
    both times): v1 fed only the last few turns and named a 900-message
    feature build after its final micro-task; v2 added the opening, but
    imported sessions open with boilerplate, so recent turns still
    dominated. The durable fix is arc coverage: sample user turns at
    ~25/50/75% of the thread (user turns carry intent; assistant turns
    echo), plus the opening and the most recent exchanges.
    """
    parts = []
    first_msg = (info.get("first_msg") or "").strip()
    if first_msg:
        parts.append(f"opening: {first_msg[:_RECENT_CHARS_PER_TURN]}")

    turns = transcript_turns(path, "")
    user_turns = [(i, text) for i, (role, text) in enumerate(turns) if role == "user"]
    if len(user_turns) > 6:
        mids = []
        for frac in (0.25, 0.5, 0.75):
            idx, text = user_turns[int(len(user_turns) * frac)]
            if idx not in mids:
                mids.append(idx)
                parts.append(f"along the way: {text[:_RECENT_CHARS_PER_TURN]}")

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
            max_tokens=30,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Here is how an ongoing coding-assistant session started "
                        "and its most recent exchanges:\n\n" + prompt_body + "\n\n"
                        "Name this session's OVERALL topic -- what the thread as a "
                        "whole is about, weighted toward where the work ended up if "
                        "it drifted, never just the latest micro-task.\n"
                        "Reply with ONLY the name: 4-6 lowercase words, no "
                        "punctuation, no quotes, no preamble, no explanation."
                    ),
                },
                # Prefill: the assistant turn already begins, so the model can
                # only continue with the name itself -- no room for "looking at
                # your recent exchanges, the topic is:" style preamble (which
                # once burned the whole token budget and shipped as a garbage
                # name to the board, the macOS banner, and the Slack alert).
                {"role": "assistant", "content": "name:"},
            ],
        )
        text = _clean_name(response.content[0].text)
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

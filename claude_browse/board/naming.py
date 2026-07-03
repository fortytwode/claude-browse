"""Auto-namer: provisional (instant, from first prompt) -> Haiku upgrade.

Runs out-of-band only, invoked from board/sync.py -- never from the hot
hook path (board/hook.py). Prefers an existing ai-title/custom-title (from
Claude Code's own session naming) over ever calling the model.
"""

from __future__ import annotations

import os

from claude_browse.board import store
from claude_browse.providers.claude import get_session_info, list_session_files

_MODEL = "claude-haiku-4-5-20251001"


def _find_jsonl_path(session_id: str) -> str | None:
    """Locate a session's jsonl by id.

    Uses list_session_files() rather than a plain glob over SESSIONS_DIR --
    it already unions that glob with a history.jsonl-based recovery path for
    sessions whose project directory was renamed or moved, which a narrower
    glob would silently miss (naming.compute_name() would return None for
    that session, not because naming failed, but because discovery did).
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


def compute_name(session_id: str) -> str | None:
    """Best-effort: existing title > Haiku synthesis > None. Never raises."""
    path = _find_jsonl_path(session_id)
    if not path:
        return None

    info = get_session_info(path)
    if not info:
        return None

    if info.get("name"):
        return info["name"]

    first_msg = info.get("first_msg")
    if not first_msg:
        return None

    try:
        client = _get_client()
        response = client.messages.create(
            model=_MODEL,
            max_tokens=20,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Summarize this coding session request in 4-6 words, "
                        "lowercase, no punctuation, no quotes:\n\n" + first_msg
                    ),
                }
            ],
        )
        text = response.content[0].text.strip().strip('"').lower()
        return text or None
    except Exception:
        return None


def maybe_name(session_id: str) -> None:
    """Upgrade a provisional name to a Haiku-derived one, once. No-op otherwise."""
    row = store.get(session_id)
    if row is not None and row.get("name_source") == "haiku":
        return

    name = compute_name(session_id)
    if name:
        store.upsert(session_id, name=name, name_source="haiku")

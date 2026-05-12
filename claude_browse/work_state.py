"""Structured restart-card extraction for session previews and handoffs."""

from __future__ import annotations

import os
from collections.abc import Iterable

from .git_state import inspect_repo_state
from .providers import get_provider
from .providers.common import is_substantive_text

_QUESTION_STARTERS = (
    "what ",
    "why ",
    "how ",
    "which ",
    "where ",
    "when ",
    "who ",
    "can ",
    "could ",
    "should ",
    "would ",
    "is ",
    "are ",
    "do ",
    "did ",
    "will ",
    "have ",
    "has ",
)


def _query_terms(query: str) -> list[str]:
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
    return [term for term in terms if term]


def _word_overlap(a: str, b: str) -> int:
    left = {word for word in a.lower().split() if len(word) >= 4}
    right = {word for word in b.lower().split() if len(word) >= 4}
    return len(left & right)


def _latest_user_turn(
    turns: Iterable[tuple[str, str]],
    *,
    substantive_only: bool,
) -> str:
    for role, text in reversed(list(turns)):
        if role != "user":
            continue
        if substantive_only and not is_substantive_text(text):
            continue
        return text
    return ""


def _latest_assistant_turn(turns: Iterable[tuple[str, str]]) -> str:
    for role, text in reversed(list(turns)):
        if role == "assistant":
            return text
    return ""


def _likely_open_question(turns: list[tuple[str, str]]) -> str:
    if not turns:
        return ""

    last_role, last_text = turns[-1]
    if last_role == "user" and is_substantive_text(last_text):
        return last_text

    for role, text in reversed(turns):
        if role != "user" or not is_substantive_text(text):
            continue
        lowered = text.lower().lstrip()
        if "?" in text or lowered.startswith(_QUESTION_STARTERS):
            return text
    return ""


def _matching_turns(
    turns: list[tuple[str, str]],
    selection_query: str,
    limit: int,
) -> list[tuple[str, str]]:
    terms = [term.lower() for term in _query_terms(selection_query) if term.strip()]
    if not terms:
        return []
    matched = [
        (role, text)
        for role, text in turns
        if any(term in text.lower() for term in terms)
    ]
    return list(reversed(matched[-limit:]))


def _recent_turns(
    turns: list[tuple[str, str]],
    limit: int,
) -> list[tuple[str, str]]:
    return list(reversed(turns[-limit:]))


def _current_task(title: str, first_msg: str, last_meaningful_user: str) -> str:
    title = (title or "").strip()
    first_msg = (first_msg or "").strip()
    last_meaningful_user = (last_meaningful_user or "").strip()

    if title and last_meaningful_user:
        overlap = _word_overlap(title, last_meaningful_user)
        return title if overlap >= 2 else last_meaningful_user
    if last_meaningful_user:
        return last_meaningful_user
    return title or first_msg


def _suggested_next_prompt(
    cwd: str | None,
    current_task: str,
    likely_open_question: str,
    latest_assistant: str,
) -> str:
    location = "this directory"
    if cwd:
        location = os.path.basename(cwd.rstrip("/")) or cwd

    if likely_open_question:
        return (
            f"Continue the work in {location}. Check the current repo state first, "
            f"then address this unresolved request: {likely_open_question}"
        )
    if current_task and latest_assistant:
        return (
            f"Continue the work in {location}. Check the current repo state first, "
            f"then pick up from the latest assistant progress on: {current_task}"
        )
    if current_task:
        return (
            f"Continue the work in {location}. Check the current repo state first, "
            f"then continue: {current_task}"
        )
    return (
        f"Continue the work in {location}. Check the current repo state first "
        "and recover the latest meaningful task from the recent turns."
    )


def build_work_state(
    session: dict[str, object],
    selection_query: str = "",
    *,
    recent_limit: int = 8,
    match_limit: int = 4,
) -> dict[str, object]:
    provider = str(session.get("provider") or "claude")
    path = str(session.get("path") or "")
    session_id = str(session.get("session_id") or "")
    spec = get_provider(provider)
    turns = spec.transcript_turns(path, session_id)

    title = str(session.get("name") or session.get("title") or "")
    first_msg = str(session.get("first_msg") or "")
    last_meaningful_user = _latest_user_turn(turns, substantive_only=True)
    latest_assistant = _latest_assistant_turn(turns)
    current_task = _current_task(title, first_msg, last_meaningful_user)

    return {
        "provider": provider,
        "provider_name": spec.display_name,
        "session_title": title,
        "opening_topic": first_msg,
        "current_task": current_task,
        "last_meaningful_user": last_meaningful_user,
        "latest_assistant": latest_assistant,
        "likely_open_question": _likely_open_question(turns),
        "matching_turns": _matching_turns(turns, selection_query, match_limit),
        "recent_turns": _recent_turns(turns, recent_limit),
        "assistant_turns_available": spec.assistant_turns_available,
        "repo_state": inspect_repo_state(session.get("cwd")),
        "topic_shifted": bool(
            current_task and first_msg and _word_overlap(current_task, first_msg) <= 1
        ),
        "suggested_next_prompt": _suggested_next_prompt(
            session.get("cwd"),
            current_task,
            _likely_open_question(turns),
            latest_assistant,
        ),
    }


def render_restart_card_terminal(state: dict[str, object]) -> str:
    lines: list[str] = ["Restart Card", ""]

    current_task = str(state.get("current_task") or "")
    if current_task:
        lines.append(f"Current task: {current_task}")

    if state.get("topic_shifted") and state.get("opening_topic"):
        lines.append(f"Opened with: {state['opening_topic']}")

    if state.get("session_title"):
        lines.append(f"Session title: {state['session_title']}")

    repo_state = state.get("repo_state")
    if isinstance(repo_state, dict) and repo_state.get("summary"):
        lines.append(f"Current repo state: {repo_state['summary']}")

    if state.get("last_meaningful_user"):
        lines.append(f"Last meaningful ask: {state['last_meaningful_user']}")

    if state.get("latest_assistant"):
        lines.append(f"Latest assistant answer: {state['latest_assistant']}")

    if state.get("likely_open_question"):
        lines.append(f"Likely open question: {state['likely_open_question']}")

    if state.get("suggested_next_prompt"):
        lines.append(f"Suggested next prompt: {state['suggested_next_prompt']}")

    matching_turns = state.get("matching_turns") or []
    if matching_turns:
        lines.extend(["", "Why this likely matched your search:", ""])
        for role, text in matching_turns:
            label = "User" if role == "user" else "Assistant"
            lines.append(f"  {label}: {text}")

    recent_turns = state.get("recent_turns") or []
    if recent_turns:
        lines.extend(["", "Recent turns (latest first):", ""])
        for role, text in recent_turns:
            label = "User" if role == "user" else "Assistant"
            lines.append(f"  {label}: {text}")

    return "\n".join(lines)


def render_restart_card_markdown(state: dict[str, object]) -> list[str]:
    lines = ["## Restart Card", ""]

    if state.get("current_task"):
        lines.append(f"- Current task: {state['current_task']}")
    if state.get("topic_shifted") and state.get("opening_topic"):
        lines.append(f"- Opened with: {state['opening_topic']}")
    if state.get("session_title"):
        lines.append(f"- Session title: {state['session_title']}")

    repo_state = state.get("repo_state")
    if isinstance(repo_state, dict) and repo_state.get("summary"):
        lines.append(f"- Current repo state: {repo_state['summary']}")

    if state.get("last_meaningful_user"):
        lines.append(f"- Last meaningful user ask: {state['last_meaningful_user']}")
    if state.get("latest_assistant"):
        lines.append(f"- Latest assistant response: {state['latest_assistant']}")
    if state.get("likely_open_question"):
        lines.append(f"- Likely open question: {state['likely_open_question']}")
    if state.get("suggested_next_prompt"):
        lines.append(f"- Suggested next prompt: {state['suggested_next_prompt']}")

    lines.append("")
    return lines

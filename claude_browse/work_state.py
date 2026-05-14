"""Structured restart-card extraction for session previews and handoffs."""

from __future__ import annotations

import os
from collections.abc import Iterable

from .git_state import inspect_repo_state
from .providers import get_provider
from .providers.common import is_substantive_text
from .query import build_query_plan, term_spans, text_contains_term

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
    terms = _query_match_terms(selection_query)
    if not terms:
        return []
    matched = [
        (role, _excerpt_around_terms(text, terms))
        for role, text in turns
        if any(text_contains_term(text, term) for term in terms)
    ]
    return list(reversed(matched[-limit:]))


def _query_match_terms(selection_query: str) -> list[str]:
    plan = build_query_plan(selection_query)
    return [term.lower() for term in plan.highlight_terms if term.strip()]


def _excerpt_around_terms(
    text: str,
    terms: list[str],
    *,
    before: int = 48,
    after: int = 180,
) -> str:
    clean = " ".join(text.split())
    lowered = clean.lower()
    matches = []
    for term in terms:
        spans = term_spans(lowered, term)
        if spans:
            start, end = spans[0]
            matches.append((term, start, end))
    if not matches:
        return clean
    phrase_matches = [
        (_term, start, end) for _term, start, end in matches if " " in _term
    ]
    chosen = phrase_matches or matches
    start = max(0, min(pos for _term, pos, _end in chosen) - before)
    end = min(len(clean), max(pos for _term, _start, pos in chosen) + after)
    excerpt = clean[start:end]
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(clean):
        excerpt = excerpt + "…"
    return excerpt


def _exchange_for_index(
    turns: list[tuple[str, str]],
    idx: int,
) -> list[tuple[str, str]]:
    role, _text = turns[idx]
    if role == "user":
        exchange = [turns[idx]]
        if idx + 1 < len(turns) and turns[idx + 1][0] == "assistant":
            exchange.append(turns[idx + 1])
        return exchange

    exchange: list[tuple[str, str]] = []
    if idx - 1 >= 0 and turns[idx - 1][0] == "user":
        exchange.append(turns[idx - 1])
    exchange.append(turns[idx])
    return exchange


def _exchange_match_score(
    exchange: list[tuple[str, str]],
    lowered_terms: list[str],
) -> tuple[int, int]:
    combined = " ".join(text.lower() for _role, text in exchange)
    term_count = sum(1 for term in lowered_terms if text_contains_term(combined, term))
    return term_count, -len(combined)


def _latest_match_index(
    turns: list[tuple[str, str]],
    selection_query: str,
) -> int | None:
    terms = _query_match_terms(selection_query)
    if not terms:
        return None

    ranked: list[tuple[int, int, int, int]] = []
    for idx in range(len(turns) - 1, -1, -1):
        role, text = turns[idx]
        if any(text_contains_term(text, term) for term in terms):
            exchange = _exchange_for_index(turns, idx)
            term_count, brevity = _exchange_match_score(exchange, terms)
            ranked.append((term_count, brevity, 1 if role == "assistant" else 0, idx))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][3]


def _matched_exchange(
    turns: list[tuple[str, str]],
    match_index: int | None,
    selection_query: str,
) -> list[tuple[str, str]]:
    if match_index is None or not turns:
        return []
    terms = _query_match_terms(selection_query)
    return [
        (role, _excerpt_around_terms(text, terms))
        for role, text in _exchange_for_index(turns, match_index)
    ]


def _post_match_recent_turns(
    turns: list[tuple[str, str]],
    match_index: int | None,
    limit: int,
) -> list[tuple[str, str]]:
    if match_index is None or match_index >= len(turns) - 1:
        return []
    return list(reversed(turns[match_index + 1 :][-limit:]))


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


def _iso_preview(ts: object) -> str:
    text = str(ts or "").strip()
    if not text:
        return ""
    return text[:19].replace("T", " ")


def _recommended_action(
    selection_query: str,
    current_task: str,
    latest_assistant: str,
    likely_open_question: str,
    matched_exchange: list[tuple[str, str]],
    *,
    thread_continued_after_match: bool,
    topic_shifted: bool,
) -> tuple[str, str]:
    if not selection_query.strip() or not matched_exchange:
        return (
            "Enter",
            "Resume the full thread from its latest state.",
        )

    if not thread_continued_after_match:
        return (
            "Enter",
            "Resume the full thread; the matched topic is still the latest state.",
        )

    plan = build_query_plan(selection_query)
    current_blob = " ".join(
        part for part in (current_task, latest_assistant, likely_open_question) if part
    ).lower()
    matched_blob = " ".join(text for _role, text in matched_exchange).lower()
    anchors = [str(term).strip(".*").lower() for term in plan.anchor_terms if term]
    current_anchor_hits = sum(1 for anchor in anchors if anchor and anchor in current_blob)
    matched_anchor_hits = sum(1 for anchor in anchors if anchor and anchor in matched_blob)

    if plan.descriptive or topic_shifted or matched_anchor_hits > current_anchor_hits:
        return (
            "Ctrl-T",
            "Re-enter the earlier matched topic; the thread later moved to newer work.",
        )

    return (
        "Enter",
        "Resume the full thread; the latest state still appears aligned with your query.",
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
    latest_match_index = _latest_match_index(turns, selection_query)
    matched_exchange = _matched_exchange(turns, latest_match_index, selection_query)
    likely_open_question = _likely_open_question(turns)
    thread_continued_after_match = bool(
        latest_match_index is not None and latest_match_index < len(turns) - 1
    )
    topic_shifted = bool(
        current_task and first_msg and _word_overlap(current_task, first_msg) <= 1
    )
    recommended_action, recommended_action_reason = _recommended_action(
        selection_query,
        current_task,
        latest_assistant,
        likely_open_question,
        matched_exchange,
        thread_continued_after_match=thread_continued_after_match,
        topic_shifted=topic_shifted,
    )

    return {
        "provider": provider,
        "provider_name": spec.display_name,
        "session_title": title,
        "opening_topic": first_msg,
        "match_label": str(session.get("match_label") or ""),
        "match_timestamp": str(session.get("match_timestamp") or ""),
        "match_confidence": str(session.get("match_confidence") or ""),
        "current_task": current_task,
        "last_meaningful_user": last_meaningful_user,
        "latest_assistant": latest_assistant,
        "likely_open_question": likely_open_question,
        "matching_turns": _matching_turns(turns, selection_query, match_limit),
        "matched_exchange": matched_exchange,
        "thread_continued_after_match": thread_continued_after_match,
        "post_match_recent_turns": _post_match_recent_turns(
            turns, latest_match_index, recent_limit
        ),
        "recent_turns": _recent_turns(turns, recent_limit),
        "assistant_turns_available": spec.assistant_turns_available,
        "repo_state": inspect_repo_state(session.get("cwd")),
        "topic_shifted": topic_shifted,
        "recommended_action": recommended_action,
        "recommended_action_reason": recommended_action_reason,
        "suggested_next_prompt": _suggested_next_prompt(
            session.get("cwd"),
            current_task,
            likely_open_question,
            latest_assistant,
        ),
    }


def render_restart_card_terminal(state: dict[str, object]) -> str:
    lines: list[str] = ["Restart Card", ""]

    match_label = str(state.get("match_label") or "")
    match_timestamp = _iso_preview(state.get("match_timestamp"))
    match_confidence = str(state.get("match_confidence") or "")
    recommended_action = str(state.get("recommended_action") or "")
    recommended_action_reason = str(state.get("recommended_action_reason") or "")
    if match_label or match_timestamp or match_confidence or recommended_action:
        lines.extend(["Why this surfaced:", ""])
        if match_label:
            lines.append(f"  Match type: {match_label}")
        if match_timestamp:
            lines.append(f"  Matched on: {match_timestamp}")
        if match_confidence:
            lines.append(f"  Match confidence: {match_confidence}")
        if recommended_action:
            lines.append(
                f"  Best action: {recommended_action} — {recommended_action_reason}"
            )
        lines.append("")

    matched_exchange = state.get("matched_exchange") or []
    if matched_exchange:
        lines.extend(["Last matching exchange:", ""])
        for role, text in matched_exchange:
            label = "User" if role == "user" else "Assistant"
            lines.append(f"  {label}: {text}")
        if state.get("thread_continued_after_match"):
            lines.append("")
            lines.append(
                "Thread continued afterward on another topic. The latest turns below are newer than the matched topic."
            )

    current_task = str(state.get("current_task") or "")
    if current_task:
        lines.extend(["", f"Current task: {current_task}"])

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
    if matching_turns and not matched_exchange:
        lines.extend(["", "Why this likely matched your search:", ""])
        for role, text in matching_turns:
            label = "User" if role == "user" else "Assistant"
            lines.append(f"  {label}: {text}")

    post_match_recent_turns = state.get("post_match_recent_turns") or []
    if post_match_recent_turns:
        lines.extend(["", "Later turns after the match (latest first):", ""])
        for role, text in post_match_recent_turns:
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

    match_label = str(state.get("match_label") or "")
    match_timestamp = _iso_preview(state.get("match_timestamp"))
    match_confidence = str(state.get("match_confidence") or "")
    recommended_action = str(state.get("recommended_action") or "")
    recommended_action_reason = str(state.get("recommended_action_reason") or "")
    if match_label or match_timestamp or match_confidence or recommended_action:
        lines.extend(["", "### Why This Surfaced", ""])
        if match_label:
            lines.append(f"- Match type: {match_label}")
        if match_timestamp:
            lines.append(f"- Matched on: {match_timestamp}")
        if match_confidence:
            lines.append(f"- Match confidence: {match_confidence}")
        if recommended_action:
            lines.append(
                f"- Best action: `{recommended_action}` — {recommended_action_reason}"
            )
        lines.append("")

    matched_exchange = state.get("matched_exchange") or []
    if matched_exchange:
        lines.extend(["", "### Last Matching Exchange", ""])
        for role, text in matched_exchange:
            label = "User" if role == "user" else "Assistant"
            lines.append(f"#### {label}")
            lines.append(text)
            lines.append("")
        if state.get("thread_continued_after_match"):
            lines.append("- The thread continued afterward on another topic.")
        post_match_recent_turns = state.get("post_match_recent_turns") or []
        if post_match_recent_turns:
            lines.extend(["", "### Later Turns After The Match", ""])
            for role, text in post_match_recent_turns:
                label = "User" if role == "user" else "Assistant"
                lines.append(f"#### {label}")
                lines.append(text)
                lines.append("")

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


def render_status_update_markdown(state: dict[str, object]) -> list[str]:
    lines = ["## Status Update", ""]

    if state.get("current_task"):
        lines.append(f"- Working on: {state['current_task']}")

    repo_state = state.get("repo_state")
    if isinstance(repo_state, dict) and repo_state.get("summary"):
        lines.append(f"- Repo state: {repo_state['summary']}")

    if state.get("latest_assistant"):
        lines.append(f"- Latest progress: {state['latest_assistant']}")

    if state.get("likely_open_question"):
        lines.append(f"- Open question: {state['likely_open_question']}")
    elif state.get("last_meaningful_user"):
        lines.append(f"- Last ask: {state['last_meaningful_user']}")

    if state.get("suggested_next_prompt"):
        lines.append(f"- Next step: {state['suggested_next_prompt']}")

    lines.append("")
    return lines


def render_status_update_terminal(state: dict[str, object]) -> str:
    return "\n".join(line for line in render_status_update_markdown(state) if line)

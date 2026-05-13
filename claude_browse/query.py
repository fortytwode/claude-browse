"""Query parsing helpers for descriptive thread recall."""

from __future__ import annotations

import re
from dataclasses import dataclass

_GENERIC_RECALL_TERMS = frozenset(
    {
        "a",
        "about",
        "all",
        "an",
        "and",
        "asking",
        "asked",
        "around",
        "bring",
        "brief",
        "chat",
        "continue",
        "conversation",
        "describe",
        "exact",
        "find",
        "for",
        "from",
        "had",
        "help",
        "i",
        "in",
        "is",
        "it",
        "last",
        "look",
        "me",
        "my",
        "of",
        "old",
        "on",
        "or",
        "resume",
        "search",
        "show",
        "talk",
        "talked",
        "telling",
        "that",
        "the",
        "thread",
        "to",
        "topic",
        "want",
        "was",
        "we",
        "were",
        "where",
        "with",
        "work",
        "please",
        "pls",
        "plz",
    }
)

_RECENCY_TERMS = frozenset(
    {
        "final",
        "last",
        "latest",
        "newest",
        "recent",
    }
)

_LIFECYCLE_TERMS = frozenset(
    {
        "closeout",
        "closeouts",
        "debrief",
        "debriefs",
        "final",
        "finalise",
        "finalize",
        "handoff",
        "handoffs",
        "recap",
        "recaps",
        "summary",
        "summaries",
        "wrapup",
        "wrapups",
    }
)

_NOISY_WORKFLOW_TERMS = frozenset(
    {
        "chat",
        "chats",
        "conversation",
        "conversations",
        "discussed",
        "discussion",
        "discussions",
        "session",
        "sessions",
        "talked",
        "thread",
        "threads",
    }
)

_TOKEN_EDGE_RE = re.compile(r"(^[^\w*]+|[^\w*]+$)")
@dataclass(frozen=True)
class QueryPlan:
    """Typed interpretation of a user query.

    `fts_terms` are the literal terms we trust enough to drive lexical
    retrieval. The intent flags carry meaning like "latest" or "closeout"
    without forcing those words to behave like hard AND constraints.
    """

    raw_terms: tuple[str, ...]
    normalized_terms: tuple[str, ...]
    fts_terms: tuple[str, ...]
    anchor_terms: tuple[str, ...]
    highlight_terms: tuple[str, ...]
    descriptive: bool
    wants_recent: bool
    wants_closeout: bool
    low_confidence: bool


def parse_query_terms(query: str) -> list[str]:
    """Return bare-word and quoted-phrase terms in source order."""
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


def _normalize_token(term: str) -> str:
    text = term.strip().replace("’", "'")
    wildcard = text.endswith("*") and text.count("*") == 1 and " " not in text
    if wildcard:
        text = text[:-1]
    text = _TOKEN_EDGE_RE.sub("", text)
    text = text.lower()
    if text.endswith("'s"):
        text = text[:-2]
    text = text.replace("'", "").replace("-", "")
    return f"{text}*" if wildcard and text else text


def _normalize_term(term: str) -> str:
    parts = [_normalize_token(part) for part in term.split()]
    cleaned = [part for part in parts if part]
    return " ".join(cleaned)


def _looks_specific_word(term: str) -> bool:
    lowered = term.strip(".*").lower()
    if not lowered or lowered in _GENERIC_RECALL_TERMS:
        return False
    if any(ch.isdigit() for ch in lowered):
        return True
    return len(lowered) >= 3


def _dedupe_preserve_order(terms: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        if not term or term in seen:
            continue
        seen.add(term)
        ordered.append(term)
    return tuple(ordered)


def build_query_plan(query: str, max_terms: int = 5) -> QueryPlan:
    """Interpret a user query into lexical anchors plus intent signals."""
    raw_terms = tuple(parse_query_terms(query))
    normalized_terms = tuple(
        term for term in (_normalize_term(raw) for raw in raw_terms) if term
    )

    wants_recent = any(term in _RECENCY_TERMS for term in normalized_terms)
    wants_closeout = any(term in _LIFECYCLE_TERMS for term in normalized_terms)

    phrase_terms = [
        term
        for term in normalized_terms
        if " " in term and any(_looks_specific_word(word) for word in term.split())
    ]
    word_terms = [
        term
        for term in normalized_terms
        if " " not in term
        and term not in _GENERIC_RECALL_TERMS
        and term not in _RECENCY_TERMS
        and term not in _LIFECYCLE_TERMS
        and term not in _NOISY_WORKFLOW_TERMS
        and _looks_specific_word(term)
    ]

    if len(word_terms) > max_terms:
        ranked = sorted(
            enumerate(word_terms),
            key=lambda item: (-len(item[1].strip(".*")), item[0]),
        )
        keep = {term for _, term in ranked[:max_terms]}
        word_terms = [term for term in word_terms if term in keep]

    fts_terms = _dedupe_preserve_order([*phrase_terms, *word_terms])
    highlight_terms = _dedupe_preserve_order(
        [
            *fts_terms,
            *[
                term
                for term in normalized_terms
                if term in _LIFECYCLE_TERMS or term in _RECENCY_TERMS
            ],
        ]
    )
    descriptive = len(raw_terms) > 2 and normalized_terms != fts_terms
    low_confidence = bool(raw_terms) and not fts_terms

    return QueryPlan(
        raw_terms=raw_terms,
        normalized_terms=normalized_terms,
        fts_terms=fts_terms,
        anchor_terms=fts_terms,
        highlight_terms=highlight_terms,
        descriptive=descriptive,
        wants_recent=wants_recent,
        wants_closeout=wants_closeout,
        low_confidence=low_confidence,
    )


def significant_query_terms(query: str, max_terms: int = 5) -> list[str]:
    """Keep the specific anchors from a longer natural-language query."""
    raw_terms = parse_query_terms(query)
    if len(raw_terms) <= 2:
        return raw_terms
    return list(build_query_plan(query, max_terms=max_terms).fts_terms)


def is_descriptive_query(query: str) -> bool:
    """Whether the user is describing a thread instead of typing anchor tokens.

    Sentence-like queries such as "where I was asking Nevena about feedback"
    should keep the natural-language UX, but the backend needs to know these
    are descriptive lookups so it can relax strict AND-only retrieval and lean
    harder on matched exchanges. Short token searches like "pokpok" or
    "runna sca2" should stay exact and mechanical.
    """
    return build_query_plan(query).descriptive

"""Query parsing helpers for descriptive thread recall."""

from __future__ import annotations

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
    }
)


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


def _looks_specific_word(term: str) -> bool:
    lowered = term.strip(".*").lower()
    if not lowered or lowered in _GENERIC_RECALL_TERMS:
        return False
    if any(ch.isdigit() for ch in lowered):
        return True
    return len(lowered) >= 3


def significant_query_terms(query: str, max_terms: int = 5) -> list[str]:
    """Keep the specific anchors from a longer natural-language query.

    Short queries pass through unchanged. Longer descriptive queries drop
    generic filler like "find me the thread where" so search stays anchored on
    the specific names, brands, folders, and quoted phrases the user actually
    cares about.
    """
    raw_terms = parse_query_terms(query)
    if len(raw_terms) <= 2:
        return raw_terms

    phrases = [term for term in raw_terms if " " in term.strip()]
    words = [
        term
        for term in raw_terms
        if " " not in term.strip() and _looks_specific_word(term)
    ]

    if not phrases and not words:
        return raw_terms[:max_terms]

    if len(words) > max_terms:
        ranked = sorted(
            enumerate(words),
            key=lambda item: (-len(item[1].strip(".*")), item[0]),
        )
        keep = {term for _, term in ranked[:max_terms]}
        words = [term for term in words if term in keep]

    return [term for term in raw_terms if term in phrases or term in words]

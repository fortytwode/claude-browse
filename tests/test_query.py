from __future__ import annotations

from claude_browse.query import build_query_plan, term_spans


def test_query_plan_extracts_anchor_and_closeout_intent():
    plan = build_query_plan("last closeout session for Musopia")

    assert plan.anchor_terms == ("musopia",)
    assert plan.wants_recent is True
    assert plan.wants_closeout is True
    assert plan.descriptive is True
    assert plan.low_confidence is False


def test_query_plan_strips_punctuation_and_politeness():
    plan = build_query_plan("that we discussed, please?")

    assert plan.normalized_terms == ("that", "we", "discussed", "please")
    assert plan.anchor_terms == ()
    assert plan.low_confidence is True


def test_query_plan_keeps_specific_people_and_topic_words():
    plan = build_query_plan("where i was asking Nevena about feedback")

    assert plan.anchor_terms == ("nevena", "feedback")
    assert plan.exact_phrase_terms == ("nevena feedback",)
    assert plan.wants_recent is False
    assert plan.wants_closeout is False
    assert plan.descriptive is True


def test_query_plan_collapses_short_anchor_search_into_phrase():
    plan = build_query_plan("cfo update")

    assert plan.fts_terms == ("cfo", "update")
    assert plan.anchor_terms == ("cfo", "update")
    assert plan.exact_phrase_terms == ("cfo update",)
    assert plan.phrase_fallback_terms == ("cfo", "update")
    assert plan.highlight_terms[0] == "cfo update"
    assert plan.descriptive is False
    assert plan.implicit_phrase is True


def test_query_plan_collapses_three_specific_words_into_phrase():
    plan = build_query_plan("CFO update notes")

    assert plan.fts_terms == ("cfo", "update", "notes")
    assert plan.exact_phrase_terms == ("cfo update notes",)
    assert plan.phrase_fallback_terms == ("cfo", "update", "notes")
    assert plan.implicit_phrase is True
    assert plan.descriptive is False


def test_query_plan_keeps_wildcard_words_as_separate_terms():
    plan = build_query_plan("runna sca*")

    assert plan.fts_terms == ("runna", "sca*")
    assert plan.implicit_phrase is False


def test_query_plan_keeps_explicit_quote_plus_word_as_separate_terms():
    plan = build_query_plan('runna "sca2 v3"')

    assert plan.fts_terms == ("sca2 v3", "runna")
    assert plan.phrase_fallback_terms == ("sca2", "v3")
    assert plan.implicit_phrase is False


def test_query_plan_does_not_collapse_when_a_word_was_dropped():
    plan = build_query_plan("runna latest")

    assert plan.fts_terms == ("runna",)
    assert plan.wants_recent is True
    assert plan.implicit_phrase is False


def test_query_plan_tracks_implicit_phrase_for_short_descriptive_anchor_search():
    plan = build_query_plan("MaxRewards built me a list")

    assert plan.anchor_terms == ("maxrewards", "list")
    assert plan.exact_phrase_terms == ("maxrewards list",)
    assert plan.highlight_terms[0] == "maxrewards list"
    assert plan.descriptive is True


def test_query_plan_tracks_phrase_fallback_terms_for_quoted_sentence():
    plan = build_query_plan('"MaxRewards built me a list"')

    assert plan.anchor_terms == ("maxrewards built me a list",)
    assert plan.exact_phrase_terms == ("maxrewards built me a list",)
    assert plan.phrase_fallback_terms == ("maxrewards", "list")
    assert plan.descriptive is False


def test_query_plan_normalizes_possessives_and_hyphens():
    plan = build_query_plan("Neil's close-out feedback")

    assert plan.normalized_terms == ("neil", "closeout", "feedback")
    assert plan.anchor_terms == ("neil", "feedback")
    assert plan.wants_closeout is True


def test_query_plan_keeps_full_sentence_as_highlight_for_descriptive_queries():
    plan = build_query_plan("Pokpok does not need a re-invention")

    assert plan.anchor_terms == ("pokpok", "does", "not", "need", "reinvention")
    assert plan.highlight_terms[0] == "pokpok does not need a reinvention"


def test_term_spans_supports_prefix_terms():
    assert term_spans("Ayan and Kartik are in the review", "kar*") == [(9, 15)]


def test_short_bare_query_matches_every_word_typed_as_one_phrase():
    """`focus should work` means those three words, in that order."""
    plan = build_query_plan("focus should work")

    assert plan.implicit_phrase is True
    # The phrase keeps "should", which is not a specific-enough anchor to
    # survive into fts_terms. Filtering it out of the phrase matched threads
    # where the words were paragraphs apart.
    assert plan.implicit_phrase_text == "focus should work"
    assert "focus should work" in plan.exact_phrase_terms


def test_short_bare_query_keeps_stopwords_inside_the_phrase():
    plan = build_query_plan("click to focus")

    assert plan.implicit_phrase is True
    assert plan.implicit_phrase_text == "click to focus"


def test_sentence_length_query_stays_a_bag_of_anchors():
    plan = build_query_plan("the notifier app is broken")

    assert plan.implicit_phrase is False
    assert plan.implicit_phrase_text == ""


def test_quoted_span_beside_a_bare_word_is_never_one_implicit_phrase():
    plan = build_query_plan('say "hi"')

    assert plan.implicit_phrase is False


def test_recency_word_filters_rather_than_joining_the_phrase():
    plan = build_query_plan("runna latest")

    assert plan.implicit_phrase is False
    assert plan.wants_recent is True

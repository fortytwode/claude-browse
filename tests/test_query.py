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


def test_query_plan_tracks_implicit_phrase_for_short_anchor_search():
    plan = build_query_plan("cfo update")

    assert plan.anchor_terms == ("cfo", "update")
    assert plan.exact_phrase_terms == ("cfo update",)
    assert plan.phrase_fallback_terms == ()
    assert plan.highlight_terms[0] == "cfo update"
    assert plan.descriptive is False


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

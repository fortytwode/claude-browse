from __future__ import annotations

from eval.typesense_experiment import (
    _lifecycle_score,
    _project_typesense_search,
    _typesense_download_url,
    _window_text,
)


def test_project_typesense_search_uses_raw_query_by_default():
    params = _project_typesense_search("where i was asking nevena about feedback", "raw")

    assert params["q"] == "where i was asking nevena about feedback"
    assert params["sort_by"] == "_text_match:desc,thread_last_timestamp:desc"


def test_project_typesense_search_uses_anchor_terms_for_planned_mode():
    params = _project_typesense_search("where i was asking nevena about feedback", "planned")

    assert params["q"] == "nevena feedback"
    assert params["sort_by"] == "_text_match:desc,thread_last_timestamp:desc"


def test_project_typesense_search_promotes_closeout_intent():
    params = _project_typesense_search("last closeout session for Musopia", "planned")

    assert params["q"] == "musopia"
    assert params["sort_by"] == "_text_match:desc,lifecycle_score:desc,thread_last_timestamp:desc"


def test_project_typesense_search_low_confidence_query_falls_back_to_recent():
    params = _project_typesense_search("that we discussed, please?", "planned")

    assert params["q"] == "*"
    assert params["sort_by"] == "thread_last_timestamp:desc"


def test_lifecycle_score_counts_closeout_cues():
    score = _lifecycle_score("Final session handoff", "wrap up summary")

    assert score >= 3


def test_window_text_uses_local_context():
    segments = [
        {"role": "user", "text": "one"},
        {"role": "assistant", "text": "two"},
        {"role": "user", "text": "three"},
        {"role": "assistant", "text": "four"},
    ]

    text = _window_text(segments, 1, radius=1)

    assert "User: one" in text
    assert "Assistant: two" in text
    assert "User: three" in text


def test_typesense_download_url_looks_like_official_release_url(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")

    url = _typesense_download_url("30.2")

    assert url == "https://dl.typesense.org/releases/30.2/typesense-server-30.2-darwin-arm64.tar.gz"

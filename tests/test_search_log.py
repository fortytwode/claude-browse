from __future__ import annotations

import json

from claude_browse import search_log


def test_log_search_writes_jsonl_with_top_results(tmp_path, monkeypatch):
    path = tmp_path / "search.log.jsonl"
    monkeypatch.setenv("CLAUDE_BROWSE_LOG_PATH", str(path))
    monkeypatch.delenv("CLAUDE_BROWSE_LOG", raising=False)

    search_log.log_search(
        "ARTEM ASC",
        [
            {
                "session_id": "abc-123",
                "provider": "codex",
                "cwd": "/tmp/musopia",
                "name": "Continue the imported Claude session context",
                "context": "Sent the clarified reply to \x01Artem\x02 in the ASA/\x01ASC\x02 thread.",
                "match_timestamp": "2026-05-20T07:20:00Z",
                "match_segment_idx": 42,
                "current_cwd_score": 3.0,
                "prefix_fallback": True,
                "msg_count": 99,
            }
        ],
        ranker="v1",
        cwd_filter=None,
        current_cwd="/tmp/musopia",
        limit=10,
        elapsed_ms=12.345,
    )

    event = json.loads(path.read_text().strip())
    assert event["event"] == "search"
    assert event["query"] == "ARTEM ASC"
    assert event["anchor_terms"] == ["artem", "asc"]
    assert event["ranker"] == "v1"
    assert event["current_cwd"] == "/tmp/musopia"
    assert event["top"][0]["session_id"] == "abc-123"
    assert event["top"][0]["current_cwd_score"] == 3.0
    assert event["top"][0]["prefix_fallback"] is True
    assert event["top"][0]["context"] == (
        "Sent the clarified reply to Artem in the ASA/ASC thread."
    )


def _log_three_result_search(query: str) -> None:
    search_log.log_search(
        query,
        [
            {"session_id": "sid-one", "provider": "claude", "msg_count": 1},
            {"session_id": "sid-two", "provider": "claude", "msg_count": 2},
            {"session_id": "sid-three", "provider": "codex", "msg_count": 3},
        ],
        ranker="v1",
        cwd_filter=None,
        current_cwd="/tmp",
        limit=10,
        elapsed_ms=1.0,
    )


def test_log_selection_records_true_rank(tmp_path, monkeypatch):
    path = tmp_path / "search.log.jsonl"
    monkeypatch.setenv("CLAUDE_BROWSE_LOG_PATH", str(path))
    monkeypatch.delenv("CLAUDE_BROWSE_LOG", raising=False)

    _log_three_result_search("bounty")
    search_log.log_selection(
        "bounty",
        action="resume",
        target_provider="claude",
        session={"session_id": "sid-three", "provider": "codex"},
    )

    events = [json.loads(line) for line in path.read_text().splitlines()]
    selection = events[-1]
    assert selection["event"] == "selection"
    assert selection["selected"]["rank"] == 3


def test_log_selection_joins_against_matching_query_only(tmp_path, monkeypatch):
    path = tmp_path / "search.log.jsonl"
    monkeypatch.setenv("CLAUDE_BROWSE_LOG_PATH", str(path))
    monkeypatch.delenv("CLAUDE_BROWSE_LOG", raising=False)

    _log_three_result_search("bounty")
    # A later keystroke changed the list; the selection query must join
    # against its own search event, not the most recent one.
    _log_three_result_search("bounty coin")
    search_log.log_selection(
        "bounty",
        action="resume",
        target_provider="claude",
        session={"session_id": "sid-two", "provider": "claude"},
    )

    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert events[-1]["selected"]["rank"] == 2


def test_log_selection_rank_is_null_when_unknown(tmp_path, monkeypatch):
    path = tmp_path / "search.log.jsonl"
    monkeypatch.setenv("CLAUDE_BROWSE_LOG_PATH", str(path))
    monkeypatch.delenv("CLAUDE_BROWSE_LOG", raising=False)

    # Empty-query pick straight off the initial paint: no search event was
    # ever logged, so the rank is honestly unknown — never a hardcoded 1.
    search_log.log_selection(
        "",
        action="resume",
        target_provider="claude",
        session={"session_id": "sid-one", "provider": "claude"},
    )

    event = json.loads(path.read_text().strip())
    assert event["selected"]["rank"] is None


def test_log_event_can_be_disabled(tmp_path, monkeypatch):
    path = tmp_path / "search.log.jsonl"
    monkeypatch.setenv("CLAUDE_BROWSE_LOG_PATH", str(path))
    monkeypatch.setenv("CLAUDE_BROWSE_LOG", "0")

    search_log.log_event("search", {"query": "pokpok"})

    assert not path.exists()


def test_log_event_rotates_when_file_exceeds_limit(tmp_path, monkeypatch):
    path = tmp_path / "search.log.jsonl"
    path.write_text("x" * 20)
    monkeypatch.setenv("CLAUDE_BROWSE_LOG_PATH", str(path))
    monkeypatch.setenv("CLAUDE_BROWSE_LOG_MAX_BYTES", "10")

    search_log.log_event("search", {"query": "pokpok"})

    assert (tmp_path / "search.log.jsonl.1").exists()
    event = json.loads(path.read_text().strip())
    assert event["query"] == "pokpok"

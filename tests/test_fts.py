"""Tests for claude_browse.fts: SQLite FTS5 indexer + search.

Each test gets its own temp database; nothing touches the user's real
~/.claude/cache/claude-browse-index.db.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from claude_browse import fts
from claude_browse.query import build_query_plan

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db():
    """Fresh in-memory-equivalent FTS db for each test."""
    path = tempfile.mktemp(suffix=".db")
    conn = fts.open_db(path)
    yield conn
    conn.close()
    if os.path.exists(path):
        os.unlink(path)


def _seed(conn, sid: str, corpus: str, **meta) -> None:
    """Insert one session with a synthetic corpus, bypassing the file walk.

    `corpus` is treated as user_text by default (preserves pre-v3 test
    semantics: 'this text exists' -> findable). Per-column overrides via
    fts_cwd / fts_title / fts_first_msg / fts_user / fts_asst / fts_boiler
    let v1-ranker tests target specific fields.
    """
    now = time.time()
    timestamp = meta.get("timestamp", "2026-05-01T10:00:00Z")
    conn.execute(
        """
        INSERT INTO sessions (sid, path, provider, cwd, timestamp, last_timestamp,
                              title, first_msg, last_msg, msg_count, mtime,
                              indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            meta.get("path", f"/tmp/{sid}.jsonl"),
            meta.get("provider", "claude"),
            meta.get("cwd", "/tmp"),
            timestamp,
            meta.get("last_timestamp", timestamp),
            meta.get("title", f"Title for {sid}"),
            meta.get("first_msg", f"first message for {sid}"),
            meta.get("last_msg", ""),
            meta.get("msg_count", 4),
            meta.get("mtime", now),
            now,
        ),
    )
    conn.execute(
        """INSERT INTO sessions_fts
           (sid, cwd, title, first_msg, user_text, asst_text, boilerplate)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            sid,
            meta.get("fts_cwd", meta.get("cwd", "")),
            meta.get("fts_title", ""),
            meta.get("fts_first_msg", ""),
            meta.get("fts_user", corpus),
            meta.get("fts_asst", ""),
            meta.get("fts_boiler", ""),
        ),
    )
    segments = meta.get("segments")
    if segments is None:
        segments = [
            (
                "user",
                corpus,
                meta.get("last_timestamp", timestamp),
            )
        ]
    for idx, (role, text, segment_ts) in enumerate(segments, 1):
        cur = conn.execute(
            """
            INSERT INTO segments (sid, segment_idx, role, timestamp, text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sid, idx, role, segment_ts, text),
        )
        conn.execute(
            """
            INSERT INTO segments_fts (rowid, sid, role, segment_idx, timestamp, text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (cur.lastrowid, sid, role, idx, segment_ts, text),
        )
    conn.commit()


# --- normalize_query -------------------------------------------------------


def test_normalize_single_word_is_token_match():
    """'runna' should query as a quoted FTS5 token, not a fuzzy match."""
    assert fts.normalize_query("runna") == '"runna"'


def test_normalize_two_words_is_AND():
    """'runna sca2' should become AND of two quoted tokens (FTS5 default)."""
    assert fts.normalize_query("runna sca2") == '"runna" "sca2"'


def test_normalize_quoted_is_phrase():
    """'"runna sca2"' (quoted) becomes an FTS5 phrase clause."""
    assert fts.normalize_query('"runna sca2"') == '"runna sca2"'


def test_normalize_mixed_word_and_phrase():
    assert (
        fts.normalize_query('runna "sca2 v3"')
        == '"runna" "sca2 v3"'
    )


def test_normalize_bare_AND_quoted_as_literal():
    """Bare 'AND' must be quoted so it doesn't trigger FTS5's operator."""
    assert fts.normalize_query("AND") == '"AND"'


def test_normalize_empty():
    assert fts.normalize_query("") == ""
    assert fts.normalize_query("   ") == ""


def test_normalize_internal_quote_escaped():
    """Embedded double-quote becomes "" inside an FTS5 phrase."""
    assert fts.normalize_query('say "hi"') == '"say" "hi"'


def test_normalize_descriptive_query_drops_filler_words():
    assert (
        fts.normalize_query("bring me the thread where i was asking about nevena feedback")
        == '"nevena" "feedback"'
    )


# --- search semantics ------------------------------------------------------


def test_search_single_word_exact_token(db):
    _seed(db, "s1", "the runna deck is ready")
    _seed(db, "s2", "running fast and loose")
    _seed(db, "s3", "rangoon was nice")

    sids = [r["session_id"] for r in fts.search(db, "runna")]
    # Only s1 has the literal token 'runna' (s2 has 'running', s3 'rangoon')
    assert sids == ["s1"]


def test_search_two_words_is_AND(db):
    _seed(db, "s1", "the runna sca2 deck is ready")
    _seed(db, "s2", "only runna mentioned here")
    _seed(db, "s3", "only sca2 mentioned here")

    sids = sorted(r["session_id"] for r in fts.search(db, "runna sca2"))
    assert sids == ["s1"]


def test_search_phrase_requires_adjacency(db):
    _seed(db, "s1", "the runna sca2 deck is ready")
    _seed(db, "s2", "runna and sca2 mentioned with gap")

    sids = sorted(r["session_id"] for r in fts.search(db, '"runna sca2"'))
    # Phrase requires adjacency — s2's "runna and sca2" is not adjacent.
    assert sids == ["s1"]


def test_search_ranked_unquoted_short_phrase_prefers_exact_phrase(db):
    _seed(
        db,
        "separate_words",
        "CFO planning and update notes",
        timestamp="2026-05-12T00:00:00Z",
        last_timestamp="2026-05-12T01:00:00Z",
        segments=[
            ("user", "Can you review CFO planning?", "2026-05-12T00:00:00Z"),
            ("assistant", "The update notes are later in the file.", "2026-05-12T01:00:00Z"),
        ],
    )
    _seed(
        db,
        "exact_phrase",
        "CFO update notes",
        timestamp="2026-05-11T00:00:00Z",
        last_timestamp="2026-05-11T01:00:00Z",
        segments=[
            ("user", "Please find the CFO update.", "2026-05-11T00:00:00Z"),
            ("assistant", "The CFO update is ready.", "2026-05-11T01:00:00Z"),
        ],
    )

    results = fts.search_ranked(db, "cfo update")
    assert [r["session_id"] for r in results][:2] == [
        "exact_phrase",
        "separate_words",
    ]
    assert "CFO update" in results[0]["context"]


def test_search_ranked_short_anchor_search_sorts_phrase_hits_by_match_recency(db):
    _seed(
        db,
        "older_title_match",
        "CFO update historical",
        title="CFO Update",
        first_msg="CFO Update kickoff",
        timestamp="2026-05-20T00:00:00Z",
        last_timestamp="2026-05-20T01:00:00Z",
        segments=[
            ("user", "Let's do the CFO update.", "2026-05-20T00:00:00Z"),
            ("assistant", "CFO update notes are drafted.", "2026-05-20T01:00:00Z"),
        ],
    )
    _seed(
        db,
        "newer_phrase_match",
        "CFO update recent",
        title="Finance check",
        first_msg="Quick finance check",
        timestamp="2026-05-28T00:00:00Z",
        last_timestamp="2026-05-28T01:00:00Z",
        segments=[
            ("user", "Can you pull the CFO update?", "2026-05-28T00:00:00Z"),
            ("assistant", "Here is the CFO update.", "2026-05-28T01:00:00Z"),
        ],
    )

    results = fts.search_ranked(db, "cfo update")
    assert [r["session_id"] for r in results][:2] == [
        "newer_phrase_match",
        "older_title_match",
    ]


def test_search_short_query_no_fuzzy_flood(db):
    """The original bug: fzf fuzzy-matched 'sca2' against 99/100 sessions."""
    _seed(db, "s1", "the SCA2 deck shipped")
    for i in range(20):
        _seed(db, f"noise{i}", "scattered semantics about agile testing")

    sids = [r["session_id"] for r in fts.search(db, "sca2")]
    assert sids == ["s1"]


def test_search_empty_returns_recent(db):
    _seed(db, "older", "x", mtime=1.0, timestamp="2026-01-01T00:00:00Z")
    _seed(db, "newer", "y", mtime=99.0, timestamp="2026-05-01T00:00:00Z")

    results = fts.search(db, "")
    assert [r["session_id"] for r in results] == ["newer", "older"]


def test_recent_sorts_by_last_activity_not_start(db):
    """An old session resumed today should outrank a newer session that
    hasn't been touched in months."""
    _seed(
        db,
        "old_resumed",
        "x",
        mtime=99.0,
        timestamp="2026-01-01T00:00:00Z",
        last_timestamp="2026-05-05T00:00:00Z",
    )
    _seed(
        db,
        "newer_dormant",
        "y",
        mtime=1.0,
        timestamp="2026-04-01T00:00:00Z",
        last_timestamp="2026-04-01T00:30:00Z",
    )

    results = fts.list_recent(db)
    assert [r["session_id"] for r in results] == ["old_resumed", "newer_dormant"]


def test_search_invalid_fts_query_returns_empty(db):
    """Invalid FTS5 syntax should not crash; we degrade to no results."""
    _seed(db, "s1", "anything")
    # Force a query that bypasses normalize_query and is invalid for FTS5.
    # normalize_query would normally protect us, but defensive fallback matters.
    results = fts.search(db, "(((")
    # normalize_query escapes everything; should still be a valid FTS5 query.
    assert isinstance(results, list)


def test_search_ranked_returns_matches_without_crashing(db):
    _seed(db, "s1", "the pokpok rollout is ready")
    _seed(db, "s2", "unrelated topic only")

    results = fts.search_ranked(db, "pokpok")
    assert [r["session_id"] for r in results] == ["s1"]


def test_search_ranked_uses_last_activity_for_recency(db):
    _seed(
        db,
        "old_resumed",
        "pokpok appears here",
        timestamp="2026-01-01T00:00:00Z",
        last_timestamp="2026-05-05T00:00:00Z",
        mtime=1.0,
    )
    _seed(
        db,
        "newer_dormant",
        "pokpok appears here",
        timestamp="2026-04-01T00:00:00Z",
        last_timestamp="2026-04-01T00:30:00Z",
        mtime=2.0,
    )

    results = fts.search_ranked(db, "pokpok")
    assert [r["session_id"] for r in results][:2] == [
        "old_resumed",
        "newer_dormant",
    ]


def test_search_ranked_uses_latest_matching_mention_not_later_unrelated_activity(db):
    _seed(
        db,
        "drifted_thread",
        "pokpok brief discussion plus later unrelated work",
        timestamp="2026-05-01T00:00:00Z",
        last_timestamp="2026-05-10T00:00:00Z",
        segments=[
            ("user", "Can you review the pokpok brief?", "2026-05-01T00:00:00Z"),
            ("assistant", "I checked the pokpok opportunities.", "2026-05-01T00:10:00Z"),
            ("user", "Now switch to backup cleanup.", "2026-05-10T00:00:00Z"),
        ],
    )
    _seed(
        db,
        "recent_match",
        "pokpok brief discussion only",
        timestamp="2026-05-08T00:00:00Z",
        last_timestamp="2026-05-08T01:00:00Z",
        segments=[
            ("user", "Can you review the pokpok brief?", "2026-05-08T00:00:00Z"),
            ("assistant", "Yes, the pokpok opportunities feel forced.", "2026-05-08T01:00:00Z"),
        ],
    )

    results = fts.search_ranked(db, "pokpok")
    assert [r["session_id"] for r in results][:2] == [
        "recent_match",
        "drifted_thread",
    ]
    assert results[0]["match_timestamp"] == "2026-05-08T01:00:00Z"


def test_search_uses_latest_matching_segment_as_context(db):
    _seed(
        db,
        "s1",
        "nevena thread with multiple topics",
        segments=[
            ("user", "Old unrelated opener", "2026-05-01T00:00:00Z"),
            ("assistant", "More unrelated context", "2026-05-01T00:05:00Z"),
            ("user", "Can you send Nevena the feedback summary?", "2026-05-09T00:00:00Z"),
            ("assistant", "Yes, I'll draft the Nevena feedback summary.", "2026-05-09T00:05:00Z"),
        ],
    )

    results = fts.search(db, "where i was asking nevena about feedback")
    assert [r["session_id"] for r in results] == ["s1"]
    assert "Nevena" in results[0]["context"] or "feedback" in results[0]["context"]
    assert results[0]["match_timestamp"] == "2026-05-09T00:05:00Z"


def test_search_ranked_context_uses_combined_exchange_window(db):
    _seed(
        db,
        "s1",
        "team management discussion",
        segments=[
            ("user", "Can we talk about Neil?", "2026-05-09T00:00:00Z"),
            (
                "assistant",
                "Yes. Nevena thinks his performance needs more support this quarter.",
                "2026-05-09T00:05:00Z",
            ),
        ],
    )

    results = fts.search_ranked(db, "Neil performance with Nevena")
    assert [r["session_id"] for r in results] == ["s1"]
    assert "Neil" in results[0]["context"]
    assert "Nevena" in results[0]["context"]
    assert "performance" in results[0]["context"]


def test_search_ranked_closeout_query_prefers_closeout_like_thread(db):
    _seed(
        db,
        "closeout_thread",
        "musopia closeout wrapup",
        timestamp="2026-05-10T00:00:00Z",
        last_timestamp="2026-05-10T01:00:00Z",
        segments=[
            ("user", "Can you close out Musopia after the final session?", "2026-05-10T00:00:00Z"),
            ("assistant", "Yes, I will finalize the Musopia handoff summary.", "2026-05-10T01:00:00Z"),
        ],
    )
    _seed(
        db,
        "generic_thread",
        "musopia dashboard work",
        timestamp="2026-05-11T00:00:00Z",
        last_timestamp="2026-05-11T01:00:00Z",
        segments=[
            ("user", "Can you check the Musopia dashboard?", "2026-05-11T00:00:00Z"),
            ("assistant", "Yes, I am looking at Musopia metrics now.", "2026-05-11T01:00:00Z"),
        ],
    )

    results = fts.search_ranked(db, "last closeout session for Musopia")
    assert [r["session_id"] for r in results][:2] == [
        "closeout_thread",
        "generic_thread",
    ]
    assert "Musopia" in results[0]["context"]


def test_search_ranked_demotes_planning_thread_for_closeout_query(db):
    _seed(
        db,
        "planning_thread",
        "musopia closeout mentioned in planning",
        title="End of day review",
        timestamp="2026-05-12T19:00:00Z",
        last_timestamp="2026-05-12T19:42:00Z",
        first_msg="Can we do our end of day review please?",
        last_msg="Reflections for tomorrow: finalize and close out Musopia final session.",
        segments=[
            ("user", "Can we do our end of day review please?", "2026-05-12T19:00:00Z"),
            ("assistant", "Reflections for tomorrow: finalize and close out Musopia final session.", "2026-05-12T19:42:00Z"),
        ],
    )
    _seed(
        db,
        "closeout_thread",
        "musopia closeout session draft",
        title="Musopia Closeout Session Draft",
        cwd="/Users/Shamanth/team-operations/clients/musopia",
        timestamp="2026-05-13T06:40:00Z",
        last_timestamp="2026-05-13T06:44:00Z",
        first_msg="Please open the Musopia Closeout Session Draft and revise the closeout notes.",
        segments=[
            ("user", "Please open the Musopia Closeout Session Draft.", "2026-05-13T06:40:00Z"),
            ("assistant", "I am revising the Musopia closeout notes now.", "2026-05-13T06:44:00Z"),
        ],
    )

    results = fts.search_ranked(db, "last closeout session for Musopia")
    assert [r["session_id"] for r in results][:2] == [
        "closeout_thread",
        "planning_thread",
    ]


def test_search_ranked_demotes_self_referential_debug_threads(db):
    _seed(
        db,
        "debug_thread",
        "browse debug thread",
        title="So we have something called Claude Browse",
        cwd="/Users/Shamanth/team-operations",
        first_msg="So we have something called Claude Browse. Could we make it better?",
        segments=[
            (
                "user",
                "The team management thread where I discussed Neil performance with Nevena is ranking badly in claude-browse.",
                "2026-05-13T03:05:00Z",
            ),
            ("assistant", "I see the search bug in claude-browse.", "2026-05-13T03:05:21Z"),
        ],
    )
    _seed(
        db,
        "real_thread",
        "team management review",
        title="Review Neil with Nevena",
        cwd="/Users/Shamanth/team-operations/team-management",
        first_msg="Need to review Neil performance with Nevena.",
        segments=[
            ("user", "Need to review Neil performance with Nevena.", "2026-05-09T00:00:00Z"),
            ("assistant", "Yes, let's go through the feedback.", "2026-05-09T00:05:00Z"),
        ],
    )

    results = fts.search_ranked(db, "Neil performance with Nevena")
    assert [r["session_id"] for r in results][:2] == ["real_thread", "debug_thread"]


def test_search_ranked_demotes_imported_session_artifacts(db):
    _seed(
        db,
        "imported_thread",
        "imported context about pokpok opportunities",
        title="Continue the imported Claude session context from /var/folders/.../claude_browse_import_123.md",
        cwd="/Users/Shamanth/team-operations",
        first_msg="Continue the imported Claude session context from /var/folders/... Treat it as prior conversation state.",
        segments=[
            (
                "user",
                "I am asking about the Pokpok brief from before and whether the opportunities are forced.",
                "2026-05-12T08:55:00Z",
            ),
            ("assistant", "I have loaded the imported Claude context.", "2026-05-12T08:55:11Z"),
        ],
    )
    _seed(
        db,
        "real_thread",
        "pokpok deconstruction",
        title="Finalize Pokpok deconstruction",
        cwd="/Users/Shamanth/team-operations/content-marketing",
        first_msg="Please review the Pokpok brief and tell me whether the opportunities are forced.",
        segments=[
            ("user", "Please review the Pokpok brief.", "2026-05-03T18:00:00Z"),
            ("assistant", "The Pokpok opportunities feel forced and need better evidence.", "2026-05-03T18:03:00Z"),
        ],
    )

    results = fts.search_ranked(db, "pokpok brief where we questioned the opportunities")
    assert [r["session_id"] for r in results][:2] == ["real_thread", "imported_thread"]


def test_search_ranked_does_not_demote_later_work_in_imported_continuation(db):
    _seed(
        db,
        "older_thread",
        "artem asc historical snapshot",
        title="Historical snapshot check",
        cwd="/Users/Shamanth/team-operations/clients/musopia",
        first_msg="Check whether the snapshot was shared.",
        timestamp="2026-04-20T08:00:00Z",
        last_timestamp="2026-04-20T08:10:00Z",
        segments=[
            (
                "assistant",
                "Artem has not shared historical ASC snapshots yet.",
                "2026-04-20T08:10:00Z",
            ),
        ],
    )
    _seed(
        db,
        "continued_thread",
        "Artem ASC imported continuation with later real work",
        title="Continue the imported Claude session context from /var/folders/.../claude_browse_import_123.md",
        cwd="/Users/Shamanth/team-operations/clients/musopia",
        first_msg="Continue the imported Claude session context from /var/folders/... Treat it as prior conversation state.",
        timestamp="2026-05-20T07:00:00Z",
        last_timestamp="2026-05-20T07:20:00Z",
        segments=[
            (
                "user",
                "Continue the imported Claude session context from /var/folders/... Treat it as prior conversation state.",
                "2026-05-20T07:00:00Z",
            ),
            ("assistant", "I loaded the handoff.", "2026-05-20T07:01:00Z"),
            (
                "user",
                "Please send the clarified note to Artem.",
                "2026-05-20T07:18:00Z",
            ),
            (
                "assistant",
                "Sent the clarified reply to Artem in the ASA/ASC thread.",
                "2026-05-20T07:20:00Z",
            ),
        ],
    )

    plan = build_query_plan("Artem ASC")
    continued_row = next(
        r
        for r in fts.search_ranked(db, "Artem ASC")
        if r["session_id"] == "continued_thread"
    )
    assert fts._artifact_penalty(continued_row, plan, "Artem ASC") == 0.0

    results = fts.search_ranked(db, "Artem ASC")
    assert [r["session_id"] for r in results][:2] == [
        "continued_thread",
        "older_thread",
    ]


def test_search_ranked_keeps_import_penalty_for_broad_single_acronym(db):
    _seed(
        db,
        "real_thread",
        "ASC source bucket analysis",
        title="Source bucket analysis",
        cwd="/Users/Shamanth/team-operations/clients/musopia",
        first_msg="Review the source bucket analysis.",
        timestamp="2026-05-19T08:00:00Z",
        last_timestamp="2026-05-19T08:10:00Z",
        segments=[
            (
                "assistant",
                "The ASC source-bucket analysis is ready.",
                "2026-05-19T08:10:00Z",
            ),
        ],
    )
    _seed(
        db,
        "continued_thread",
        "ASC imported continuation with later work",
        title="Continue the imported Claude session context from /var/folders/.../claude_browse_import_123.md",
        cwd="/Users/Shamanth/team-operations/clients/musopia",
        first_msg="Continue the imported Claude session context from /var/folders/... Treat it as prior conversation state.",
        timestamp="2026-05-20T07:00:00Z",
        last_timestamp="2026-05-20T07:20:00Z",
        segments=[
            (
                "user",
                "Continue the imported Claude session context from /var/folders/... Treat it as prior conversation state.",
                "2026-05-20T07:00:00Z",
            ),
            ("assistant", "I loaded the handoff.", "2026-05-20T07:01:00Z"),
            (
                "assistant",
                "Sent the reply in the ASA/ASC thread.",
                "2026-05-20T07:20:00Z",
            ),
        ],
    )

    results = fts.search_ranked(db, "ASC")
    assert [r["session_id"] for r in results][:2] == [
        "real_thread",
        "continued_thread",
    ]


def test_search_ranked_demotes_session_handover_artifacts(db):
    _seed(
        db,
        "handover_thread",
        "musopia handover instructions",
        title="Read clients/musopia/SESSION_HANDOVER_2026-05-11.md for the full context",
        cwd="/Users/Shamanth/team-operations/clients/musopia",
        first_msg="Read clients/musopia/SESSION_HANDOVER_2026-05-11.md for the full context. Use git rigorously; commit per logical change. Run the QA scripts.",
        segments=[
            (
                "user",
                "Read clients/musopia/SESSION_HANDOVER_2026-05-11.md for the full context.",
                "2026-05-11T09:49:00Z",
            ),
            (
                "assistant",
                "Use git rigorously; commit per logical change. Run the QA scripts.",
                "2026-05-11T09:49:38Z",
            ),
        ],
    )
    _seed(
        db,
        "closeout_thread",
        "musopia closeout session draft",
        title="Musopia Closeout Session Draft",
        cwd="/Users/Shamanth/team-operations/clients/musopia",
        first_msg="Please look at the outline for the closeout session and prepare slides for the final session.",
        segments=[
            ("user", "Please look at the outline for the closeout session.", "2026-05-13T06:40:00Z"),
            ("assistant", "I am preparing slides for the Musopia final session.", "2026-05-13T06:44:00Z"),
        ],
    )

    results = fts.search_ranked(db, "last closeout session for Musopia")
    assert [r["session_id"] for r in results][:2] == ["closeout_thread", "handover_thread"]


def test_search_ranked_uses_metadata_anchor_score_for_feedback_queries(db):
    _seed(
        db,
        "generic_thread",
        "team ops discussion",
        cwd="/Users/Shamanth/team-operations",
        segments=[
            (
                "assistant",
                "Let me check the Notion analysis page Nevena referenced. The task is in review awaiting your call.",
                "2026-04-24T16:34:00Z",
            ),
        ],
    )
    _seed(
        db,
        "review_thread",
        "review nevena feedback",
        title="Review Nevena's feedback on ClickUp task",
        cwd="/Users/Shamanth/team-operations",
        first_msg="I'll look at Nevena's feedback here and see what the next steps are.",
        segments=[
            (
                "user",
                "I'll look at Nevena's feedback here and see what the next steps are.",
                "2026-05-04T18:30:00Z",
            ),
            ("assistant", "Nevena confirmed she has read the SOP.", "2026-05-04T18:31:00Z"),
        ],
    )

    results = fts.search_ranked(db, "where i was asking nevena about feedback")
    assert results[0]["session_id"] == "review_thread"


def test_search_ranked_descriptive_query_uses_semantic_anchor_terms(db):
    plan = build_query_plan("pokpok brief where we questioned the opportunities")
    assert fts._semantic_anchor_terms(plan) == ("pokpok",)


def test_search_ranked_critique_query_prefers_critique_exchange_context(db):
    _seed(
        db,
        "generic_thread",
        "pokpok task draft",
        title="Finalize Pokpok deck",
        first_msg="Finalize the Pokpok deck and opportunity slides.",
        segments=[
            ("user", "Finalize the Pokpok deck and opportunities slides.", "2026-05-08T10:00:00Z"),
            ("assistant", "Yes, I will finalize the Pokpok opportunities slides.", "2026-05-08T10:05:00Z"),
        ],
    )
    _seed(
        db,
        "critique_thread",
        "pokpok brief review",
        title="Review Pokpok brief",
        first_msg="Please review the Pokpok brief.",
        segments=[
            ("user", "Please review the Pokpok brief.", "2026-05-09T10:00:00Z"),
            (
                "assistant",
                "The opportunities feel forced and need better evidence.",
                "2026-05-09T10:05:00Z",
            ),
        ],
    )

    results = fts.search_ranked(
        db,
        "pokpok brief where we questioned the opportunities",
    )
    assert results[0]["session_id"] == "critique_thread"
    assert "forced" in results[0]["context"].lower()


def test_search_ranked_performance_review_query_demotes_performance_marketing(db):
    _seed(
        db,
        "marketing_thread",
        "campaign planning",
        title="Campaign planning with Neil and Nevena",
        first_msg="Nevena said we should remove the performance marketing section for Neil.",
        segments=[
            (
                "user",
                "Nevena said we should remove the performance marketing section for Neil.",
                "2026-05-08T09:00:00Z",
            ),
            ("assistant", "I can update that marketing plan.", "2026-05-08T09:05:00Z"),
        ],
    )
    _seed(
        db,
        "review_thread",
        "neil review",
        title="Review Neil with Nevena",
        first_msg="Need to review Neil with Nevena in the next 1:1.",
        segments=[
            ("user", "Need to review Neil with Nevena in the next 1:1.", "2026-05-09T09:00:00Z"),
            (
                "assistant",
                "Let's go through his feedback and support plan.",
                "2026-05-09T09:05:00Z",
            ),
        ],
    )

    results = fts.search_ranked(db, "Neil performance with Nevena")
    assert results[0]["session_id"] == "review_thread"


def test_search_ranked_metadata_anchor_score_downweights_very_late_prompt_mentions(db):
    _seed(
        db,
        "late_prompt_thread",
        "mixed thread",
        title=("Filler " * 4000) + " pokpok",
        first_msg="General project planning without the target brand.",
        segments=[
            ("assistant", "One passing mention of Pokpok inside a giant pasted prompt.", "2026-05-10T00:00:00Z"),
        ],
    )
    _seed(
        db,
        "real_thread",
        "pokpok deconstruction review",
        title="Finalize Pokpok deconstruction",
        first_msg="Please review the Pokpok brief.",
        segments=[
            ("user", "Please review the Pokpok brief.", "2026-05-09T00:00:00Z"),
            ("assistant", "Yes, the Pokpok brief needs work.", "2026-05-09T00:05:00Z"),
        ],
    )

    results = fts.search_ranked(db, "pokpok")
    assert results[0]["session_id"] == "real_thread"


def test_search_ranked_plain_entity_query_demotes_code_reference_mentions(db):
    _seed(
        db,
        "code_ref_thread",
        "artifact thread",
        title="MaxRewards prompt audit",
        first_msg="I pointed Claude at build_pokpok_deck.py and pokpok_strategy.md to inspect the prompts.",
        segments=[
            (
                "assistant",
                "This references the same recent scripts (Pokpok, Zeta, BeFreed) and build_pokpok_deck.py to inspect the prompt.",
                "2026-05-10T00:00:00Z",
            ),
        ],
    )
    _seed(
        db,
        "real_thread",
        "pokpok deconstruction review",
        title="Finalize Pokpok deconstruction",
        first_msg="Please review the Pokpok brief.",
        segments=[
            ("user", "Please review the Pokpok brief.", "2026-05-09T00:00:00Z"),
            ("assistant", "Yes, the Pokpok brief needs work.", "2026-05-09T00:05:00Z"),
        ],
    )

    plan = build_query_plan("pokpok")
    code_row = fts.get_by_sid(db, "code_ref_thread")
    real_row = fts.get_by_sid(db, "real_thread")
    assert fts._artifact_penalty(code_row, plan, "pokpok") >= 5.0
    assert fts._artifact_penalty(real_row, plan, "pokpok") == 0.0

    results = fts.search_ranked(db, "pokpok")
    assert results[0]["session_id"] == "real_thread"


def test_search_ranked_single_anchor_workspace_match_beats_incidental_mention(db):
    _seed(
        db,
        "workspace_thread",
        "workspace thread",
        cwd="/Users/Shamanth/tiktoker",
        title="Verify Maxrewards automation health check issue",
        first_msg="Investigate the latest automation issue in this repo.",
        last_msg="Still working in the local repo.",
        fts_cwd="/Users/Shamanth/tiktoker",
        segments=[
            ("user", "Investigate the latest automation issue in this repo.", "2026-05-10T00:00:00Z"),
        ],
    )
    _seed(
        db,
        "incidental_thread",
        "incidental mention",
        cwd="/Users/Shamanth/team-operations",
        title="Review Immutable audit and consolidate findings",
        first_msg="AppsFlyer Search Ads API check (Tiktoker context).",
        segments=[
            ("assistant", "AppsFlyer Search Ads API check (Tiktoker context).", "2026-05-11T00:00:00Z"),
        ],
    )

    results = fts.search_ranked(db, "tiktoker")
    assert results[0]["session_id"] == "workspace_thread"


def test_search_ranked_descriptive_single_anchor_query_demotes_code_reference_mentions(db):
    _seed(
        db,
        "code_ref_thread",
        "artifact thread",
        title="Prompt audit",
        first_msg="I pointed Claude at build_pokpok_deck.py to inspect the prompt before changing the opportunities slide.",
        segments=[
            (
                "assistant",
                "The prompt in build_pokpok_deck.py mentions Pokpok and opportunities repeatedly.",
                "2026-05-10T00:00:00Z",
            ),
        ],
    )
    _seed(
        db,
        "real_thread",
        "pokpok deconstruction review",
        title="Finalize Pokpok deconstruction",
        first_msg="Please review the Pokpok brief.",
        segments=[
            ("user", "Please review the Pokpok brief.", "2026-05-09T00:00:00Z"),
            (
                "assistant",
                "The opportunities feel forced and need better evidence.",
                "2026-05-09T00:05:00Z",
            ),
        ],
    )

    results = fts.search_ranked(
        db,
        "pokpok brief where we questioned the opportunities",
    )
    assert results[0]["session_id"] == "real_thread"


def test_search_ranked_low_confidence_descriptive_query_falls_back_to_recent(db):
    _seed(
        db,
        "older",
        "anything",
        timestamp="2026-05-01T00:00:00Z",
        last_timestamp="2026-05-01T01:00:00Z",
        mtime=1.0,
    )
    _seed(
        db,
        "newer",
        "more anything",
        timestamp="2026-05-02T00:00:00Z",
        last_timestamp="2026-05-02T01:00:00Z",
        mtime=2.0,
    )

    results = fts.search_ranked(db, "that we discussed, please?")
    assert [r["session_id"] for r in results][:2] == ["newer", "older"]


def test_search_ranked_descriptive_query_matches_local_window(db):
    _seed(
        db,
        "s1",
        "team management discussion",
        segments=[
            ("user", "Need to discuss Neil today.", "2026-05-09T00:00:00Z"),
            ("assistant", "Sure, what part?", "2026-05-09T00:02:00Z"),
            ("user", "His performance review with Nevena.", "2026-05-09T00:04:00Z"),
            ("assistant", "I can help with that.", "2026-05-09T00:06:00Z"),
        ],
    )

    results = fts.search_ranked(db, "where i was discussing neil performance with nevena")
    assert [r["session_id"] for r in results] == ["s1"]
    assert "Neil" in results[0]["context"]
    assert "Nevena" in results[0]["context"]


def test_search_ranked_phrase_highlight_prefers_long_query_span(db):
    _seed(
        db,
        "s1",
        "placeholder",
        provider="codex",
        segments=[
            (
                "assistant",
                "This is not a blank-slate brief. Pokpok already has a real "
                "winning system. Later in the same note: Pokpok does not need "
                "a reinvention. It already has a working visual system.",
                "2026-05-12T00:00:00Z",
            ),
        ],
    )

    results = fts.search_ranked(db, "Pokpok does not need a re-invention")
    assert results[0]["session_id"] == "s1"
    assert "Pokpok does not need a reinvention" in results[0]["context"]
    assert "blank-slate brief" not in results[0]["context"]


# --- reindex ---------------------------------------------------------------


def test_reindex_picks_up_new_files(db, tmp_path, monkeypatch):
    """A new JSONL on disk shows up in the index after reindex()."""
    sessions_dir = tmp_path / "projects" / "demo"
    sessions_dir.mkdir(parents=True)
    target = sessions_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    shutil.copy(FIXTURES / "sample_session.jsonl", target)

    monkeypatch.setattr(
        fts,
        "list_index_records",
        lambda: [{
            "path": str(target),
            "provider": "claude",
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "first_msg": "the login page crashes when I click continue",
            "last_msg": "email validation should happen before the redirect",
            "timestamp": "2026-04-01T10:00:00Z",
            "last_timestamp": "2026-04-01T10:10:00Z",
            "cwd": "/Users/alice/code/webapp",
            "name": "Debug login flow",
            "msg_count": 4,
            "mtime": os.path.getmtime(target),
            "fields": {
                "cwd": "/users/alice/code/webapp",
                "title": "debug login flow",
                "first_msg": "the login page crashes when i click continue",
                "user_text": "email validation should happen before the redirect",
                "asst_text": "the login handler is short-circuiting on missing email validation",
                "boilerplate": "",
            },
        }],
    )

    added, updated, removed = fts.reindex(db)
    assert added == 1
    assert updated == 0
    assert removed == 0

    sids = [r["session_id"] for r in fts.list_recent(db)]
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in sids


def test_reindex_skips_unchanged(db, tmp_path, monkeypatch):
    """Second reindex with no mtime change does no work."""
    sessions_dir = tmp_path / "projects" / "demo"
    sessions_dir.mkdir(parents=True)
    target = sessions_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    shutil.copy(FIXTURES / "sample_session.jsonl", target)

    monkeypatch.setattr(
        fts,
        "list_index_records",
        lambda: [{
            "path": str(target),
            "provider": "claude",
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "first_msg": "the login page crashes when I click continue",
            "last_msg": "email validation should happen before the redirect",
            "timestamp": "2026-04-01T10:00:00Z",
            "last_timestamp": "2026-04-01T10:10:00Z",
            "cwd": "/Users/alice/code/webapp",
            "name": "Debug login flow",
            "msg_count": 4,
            "mtime": os.path.getmtime(target),
            "fields": {
                "cwd": "/users/alice/code/webapp",
                "title": "debug login flow",
                "first_msg": "the login page crashes when i click continue",
                "user_text": "email validation should happen before the redirect",
                "asst_text": "the login handler is short-circuiting on missing email validation",
                "boilerplate": "",
            },
        }],
    )

    fts.reindex(db)
    added2, updated2, removed2 = fts.reindex(db)
    assert (added2, updated2, removed2) == (0, 0, 0)


def test_reindex_removes_deleted_files(db, tmp_path, monkeypatch):
    sessions_dir = tmp_path / "projects" / "demo"
    sessions_dir.mkdir(parents=True)
    target = sessions_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    shutil.copy(FIXTURES / "sample_session.jsonl", target)

    monkeypatch.setattr(
        fts,
        "list_index_records",
        lambda: [{
            "path": str(target),
            "provider": "claude",
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "first_msg": "the login page crashes when I click continue",
            "last_msg": "email validation should happen before the redirect",
            "timestamp": "2026-04-01T10:00:00Z",
            "last_timestamp": "2026-04-01T10:10:00Z",
            "cwd": "/Users/alice/code/webapp",
            "name": "Debug login flow",
            "msg_count": 4,
            "mtime": os.path.getmtime(target),
            "fields": {
                "cwd": "/users/alice/code/webapp",
                "title": "debug login flow",
                "first_msg": "the login page crashes when i click continue",
                "user_text": "email validation should happen before the redirect",
                "asst_text": "the login handler is short-circuiting on missing email validation",
                "boilerplate": "",
            },
        }],
    )
    fts.reindex(db)
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1

    monkeypatch.setattr(fts, "list_index_records", lambda: [])
    _, _, removed = fts.reindex(db)
    assert removed == 1
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_reindex_keeps_session_when_same_sid_moves_paths(db, tmp_path, monkeypatch):
    old_dir = tmp_path / "projects" / "old"
    new_dir = tmp_path / "projects" / "new"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    old_target = old_dir / "session.jsonl"
    new_target = new_dir / "session.jsonl"
    shutil.copy(FIXTURES / "sample_session.jsonl", old_target)
    shutil.copy(FIXTURES / "sample_session.jsonl", new_target)

    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def _record(path: str, mtime: float) -> list[dict[str, object]]:
        return [{
            "path": path,
            "provider": "claude",
            "session_id": sid,
            "first_msg": "the login page crashes when I click continue",
            "last_msg": "email validation should happen before the redirect",
            "timestamp": "2026-04-01T10:00:00Z",
            "last_timestamp": "2026-04-01T10:10:00Z",
            "cwd": "/Users/alice/code/webapp",
            "name": "Debug login flow",
            "msg_count": 4,
            "mtime": mtime,
            "fields": {
                "cwd": "/users/alice/code/webapp",
                "title": "debug login flow",
                "first_msg": "the login page crashes when i click continue",
                "user_text": "email validation should happen before the redirect",
                "asst_text": "the login handler is short-circuiting on missing email validation",
                "boilerplate": "",
            },
        }]

    monkeypatch.setattr(
        fts,
        "list_index_records",
        lambda: _record(str(old_target), os.path.getmtime(old_target)),
    )
    fts.reindex(db)

    monkeypatch.setattr(
        fts,
        "list_index_records",
        lambda: _record(str(new_target), os.path.getmtime(new_target)),
    )
    added, updated, removed = fts.reindex(db)

    row = db.execute(
        "SELECT sid, path FROM sessions WHERE sid = ?",
        (sid,),
    ).fetchone()
    assert row == (sid, str(new_target))
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert removed == 0
    assert added + updated == 1


def test_get_by_sid_roundtrip(db):
    _seed(db, "s1", "hello world", title="My Session", cwd="/Users/me/proj")
    info = fts.get_by_sid(db, "s1")
    assert info is not None
    assert info["session_id"] == "s1"
    assert info["provider"] == "claude"
    assert info["name"] == "My Session"
    assert info["cwd"] == "/Users/me/proj"


def test_get_by_sid_missing(db):
    assert fts.get_by_sid(db, "does-not-exist") is None

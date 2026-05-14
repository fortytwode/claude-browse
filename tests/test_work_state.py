"""Tests for structured restart-card extraction and repo-state overlays."""

from __future__ import annotations

from types import SimpleNamespace

from claude_browse import git_state, work_state


def test_build_work_state_prefers_end_of_thread_task_over_opening_topic(monkeypatch):
    turns = [
        ("user", "signup is broken on the staging deploy, can you investigate"),
        ("assistant", "Looking at the signup handler."),
        (
            "user",
            "ok signup is fine now. switching to a totally different topic, "
            "need help drafting a Q3 hiring plan for the platform team",
        ),
        (
            "assistant",
            "Sure. For the platform team headcount expansion, I'd start with "
            "a staffing matrix.",
        ),
    ]
    monkeypatch.setattr(
        work_state,
        "get_provider",
        lambda provider: SimpleNamespace(
            display_name="Claude",
            assistant_turns_available=True,
            transcript_turns=lambda path, session_id: turns,
        ),
    )
    monkeypatch.setattr(
        work_state,
        "inspect_repo_state",
        lambda cwd: {"summary": "Branch `main` with a clean working tree."},
    )

    state = work_state.build_work_state(
        {
            "provider": "claude",
            "path": "/tmp/session.jsonl",
            "session_id": "abc-123",
            "cwd": "/home/alice/webapp",
            "name": "Q3 platform hiring plan",
            "first_msg": "signup is broken on the staging deploy, can you investigate",
        },
        "signup",
    )

    assert state["current_task"] == "Q3 platform hiring plan"
    assert state["topic_shifted"] is True
    assert "hiring plan" in str(state["last_meaningful_user"]).lower()
    assert "staffing matrix" in str(state["latest_assistant"]).lower()
    assert any(
        text.startswith("signup is broken on the staging deploy")
        for _, text in state["matching_turns"]
    )
    assert state["matched_exchange"] == [
        ("user", "signup is broken on the staging deploy, can you investigate"),
        ("assistant", "Looking at the signup handler."),
    ]
    assert state["thread_continued_after_match"] is True
    assert any(
        "hiring plan" in text.lower()
        for _, text in state["post_match_recent_turns"]
    )


def test_build_work_state_marks_last_user_turn_as_open_question(monkeypatch):
    monkeypatch.setattr(
        work_state,
        "get_provider",
        lambda provider: SimpleNamespace(
            display_name="CodeX",
            assistant_turns_available=False,
            transcript_turns=lambda path, session_id: [
                ("user", "can you investigate why the deploy is failing?"),
            ],
        ),
    )
    monkeypatch.setattr(
        work_state,
        "inspect_repo_state",
        lambda cwd: {"summary": "Branch `release` with 3 uncommitted files."},
    )

    state = work_state.build_work_state(
        {
            "provider": "codex",
            "path": "codex://abc-123",
            "session_id": "abc-123",
            "cwd": "/home/alice/release",
            "name": "Deploy debugging",
            "first_msg": "can you investigate why the deploy is failing?",
        }
    )

    assert state["likely_open_question"] == (
        "can you investigate why the deploy is failing?"
    )
    assert "unresolved request" in str(state["suggested_next_prompt"])


def test_build_work_state_centers_matched_exchange_on_long_query_span(monkeypatch):
    turns = [
        ("user", "Can you tighten the Pokpok strategic close?"),
        (
            "assistant",
            "Here is the draft. This is not a blank-slate brief. Pokpok already "
            "has a real winning system in-market. Later in the same note: Pokpok "
            "does not need a reinvention. It already has a working visual system.",
        ),
    ]
    monkeypatch.setattr(
        work_state,
        "get_provider",
        lambda provider: SimpleNamespace(
            display_name="CodeX",
            assistant_turns_available=True,
            transcript_turns=lambda path, session_id: turns,
        ),
    )
    monkeypatch.setattr(
        work_state,
        "inspect_repo_state",
        lambda cwd: {"summary": "Branch `main` with a clean working tree."},
    )

    state = work_state.build_work_state(
        {
            "provider": "codex",
            "path": "/tmp/codex-session.jsonl",
            "session_id": "abc-123",
            "cwd": "/home/alice/release",
            "name": "Pokpok strategic close",
            "first_msg": "Can you tighten the Pokpok strategic close?",
        },
        "Pokpok does not need a re-invention",
    )

    assistant_excerpt = state["matched_exchange"][1][1]
    assert "Pokpok does not need a reinvention" in assistant_excerpt
    assert "This is not a blank-slate brief." not in assistant_excerpt


def test_render_restart_card_terminal_surfaces_repo_state_and_matches():
    text = work_state.render_restart_card_terminal(
        {
            "match_label": "primary subject",
            "match_timestamp": "2026-05-12T14:54:28Z",
            "match_confidence": "high",
            "recommended_action": "Ctrl-T",
            "recommended_action_reason": "Re-enter the earlier matched topic; the thread later moved to newer work.",
            "current_task": "Q3 platform hiring plan",
            "topic_shifted": True,
            "opening_topic": "Investigate signup flow regression",
            "session_title": "Q3 platform hiring plan",
            "repo_state": {
                "summary": "Branch `main` with 2 uncommitted files.",
            },
            "last_meaningful_user": "Draft the hiring plan",
            "latest_assistant": "Start with a staffing matrix.",
            "likely_open_question": "",
            "suggested_next_prompt": "Continue the work in webapp.",
            "matched_exchange": [("user", "Can you review the pokpok brief?")],
            "thread_continued_after_match": True,
            "post_match_recent_turns": [("user", "Now archive the backups after that.")],
            "matching_turns": [("assistant", "I checked the Sherlock output.")],
            "recent_turns": [("user", "Draft the hiring plan")],
        }
    )

    assert "Restart Card" in text
    assert "Why this surfaced:" in text
    assert "Match type: primary subject" in text
    assert "Matched on: 2026-05-12 14:54:28" in text
    assert "Match confidence: high" in text
    assert "Best action: Ctrl-T" in text
    assert "Current repo state: Branch `main` with 2 uncommitted files." in text
    assert "Last matching exchange:" in text
    assert text.index("Why this surfaced:") < text.index("Last matching exchange:")
    assert text.index("Last matching exchange:") < text.index("Current task:")
    assert "Thread continued afterward on another topic." in text
    assert "Later turns after the match (latest first):" in text
    assert "Recent turns (latest first):" in text


def test_build_work_state_recommends_reenter_when_query_matches_older_topic(monkeypatch):
    turns = [
        ("user", "what wins the deal"),
        (
            "assistant",
            "Pricing should feel modular and frugal: likely $4K-$6K depending on mix. Keep the big retainer out of the first close unless he pulls you there.",
        ),
        ("user", "Now write the AppsFlyer Notion draft."),
        ("assistant", "The page is created in Notion and ready to record."),
    ]

    spec = type(
        "Spec",
        (),
        {
            "display_name": "CodeX",
            "assistant_turns_available": True,
            "transcript_turns": staticmethod(lambda path, session_id: turns),
        },
    )()

    monkeypatch.setattr(work_state, "get_provider", lambda provider: spec)
    monkeypatch.setattr(
        work_state,
        "inspect_repo_state",
        lambda cwd: {"summary": "Branch `main` clean."},
    )

    state = work_state.build_work_state(
        {
            "provider": "codex",
            "path": "/tmp/session.jsonl",
            "cwd": "/tmp/sales",
            "name": "Jisoo + Elise tasks",
            "first_msg": "Send the calendar invite to Jisoo and prep the deal dossier.",
            "match_label": "primary subject",
            "match_timestamp": "2026-05-12T14:54:28Z",
            "match_confidence": "high",
        },
        "Pricing should feel modular and frugal: likely $4K-$6K depending on mix",
    )

    assert state["recommended_action"] == "Ctrl-T"
    assert "earlier matched topic" in state["recommended_action_reason"]


def test_render_status_update_terminal_surfaces_task_progress_and_next_step():
    text = work_state.render_status_update_terminal(
        {
            "current_task": "Q3 platform hiring plan",
            "repo_state": {
                "summary": "Branch `main` with 2 uncommitted files.",
            },
            "latest_assistant": "Start with a staffing matrix.",
            "likely_open_question": "Should we hire two engineers or three?",
            "suggested_next_prompt": "Continue the work in webapp.",
        }
    )

    assert "Status Update" in text
    assert "- Working on: Q3 platform hiring plan" in text
    assert "- Repo state: Branch `main` with 2 uncommitted files." in text
    assert "- Latest progress: Start with a staffing matrix." in text
    assert "- Open question: Should we hire two engineers or three?" in text
    assert "- Next step: Continue the work in webapp." in text


def test_inspect_repo_state_parses_branch_and_dirty(monkeypatch, tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    class Result:
        returncode = 0
        stdout = "## feature/restart-card\n M README.md\n?? tests/test_work_state.py\n"

    monkeypatch.setattr(git_state.subprocess, "run", lambda *args, **kwargs: Result())

    state = git_state.inspect_repo_state(str(repo_dir))

    assert state["is_git"] is True
    assert state["branch"] == "feature/restart-card"
    assert state["dirty"] is True
    assert state["changed_files"] == 2

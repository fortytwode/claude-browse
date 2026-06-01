from __future__ import annotations

import json

from claude_browse import codex_mobile_json as mobile


def test_build_cmd_starts_with_stdin_prompt(tmp_path):
    last = tmp_path / "last.txt"

    cmd = mobile._build_cmd("hello", last, real_codex="/bin/codex")

    assert cmd == [
        "/bin/codex",
        "exec",
        "--json",
        "--disable",
        "apps",
        "--disable",
        "enable_mcp_apps",
        "-o",
        str(last),
        "--color",
        "never",
        "-",
    ]


def test_build_cmd_resumes_with_stdin_prompt(tmp_path):
    last = tmp_path / "last.txt"

    cmd = mobile._build_cmd("hello", last, session_id="abc-123", real_codex="/bin/codex")

    assert cmd == [
        "/bin/codex",
        "exec",
        "resume",
        "--json",
        "--disable",
        "apps",
        "--disable",
        "enable_mcp_apps",
        "-o",
        str(last),
        "abc-123",
        "-",
    ]


def test_build_cmd_adds_yolo_flag(tmp_path):
    last = tmp_path / "last.txt"

    cmd = mobile._build_cmd("hello", last, real_codex="/bin/codex", yolo=True)

    assert mobile.YOLO_FLAG in cmd


def test_render_event_handles_thread_and_agent_message(capsys):
    thread_id = mobile._render_event(
        {"type": "thread.started", "thread_id": "019e-test"}
    )
    mobile._render_event(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "full assistant text"},
        }
    )

    out = capsys.readouterr().out
    assert thread_id == "019e-test"
    assert "thread 019e-test" in out
    assert "ASSISTANT" in out
    assert "full assistant text" in out


def test_render_event_handles_failed_tool(capsys):
    mobile._render_event(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "status": "failed",
                "exit_code": 7,
                "aggregated_output": "before\n",
            },
        }
    )

    out = capsys.readouterr().out
    assert "tool failed" in out
    assert "exit 7" in out
    assert "before" in out


def test_render_history_text_reads_codex_session(tmp_path, monkeypatch):
    session = tmp_path / "rollout-2026-05-29T00-00-00-019e-test.jsonl"
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hi back"}],
            },
        },
    ]
    session.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    monkeypatch.setattr(mobile, "_find_session_file", lambda session_id: session)

    text = mobile._render_history_text("019e-test")

    assert "Session file:" in text
    assert "USER" in text
    assert "hello" in text
    assert "ASSISTANT" in text
    assert "hi back" in text


def test_render_history_text_handles_missing_session(monkeypatch):
    monkeypatch.setattr(mobile, "_find_session_file", lambda session_id: None)

    assert mobile._render_history_text("missing") == "No Codex session file found.\n"


def test_parse_args_supports_resume_and_start():
    assert mobile._parse_args(["resume", "abc", "hello", "there"]) == (
        "abc",
        "hello there",
        False,
    )
    assert mobile._parse_args(["start", "hello"]) == (None, "hello", False)
    assert mobile._parse_args(["hello"]) == (None, "hello", False)
    assert mobile._parse_args(["--yolo", "resume", "abc", "hello"]) == (
        "abc",
        "hello",
        True,
    )


def test_main_one_shot_non_tty_does_not_enter_repl(monkeypatch, capsys):
    class NonTty:
        def isatty(self) -> bool:
            return False

        def read(self) -> str:
            return ""

    calls = []
    monkeypatch.setattr(mobile.sys, "stdin", NonTty())
    monkeypatch.setattr(
        mobile,
        "run_turn",
        lambda prompt, session_id=None, yolo=False: (
            calls.append((prompt, session_id, yolo)) or "new-id"
        ),
    )
    monkeypatch.setattr(mobile, "_last_exit_code", 0)

    assert mobile.main(["hello"]) == 0

    assert calls == [("hello", None, False)]
    assert "you>" not in capsys.readouterr().out


def test_main_reads_piped_prompt_when_no_arg(monkeypatch):
    class NonTty:
        def isatty(self) -> bool:
            return False

        def read(self) -> str:
            return "from pipe"

    calls = []
    monkeypatch.setattr(mobile.sys, "stdin", NonTty())
    monkeypatch.setattr(
        mobile,
        "run_turn",
        lambda prompt, session_id=None, yolo=False: (
            calls.append((prompt, session_id, yolo)) or "new-id"
        ),
    )
    monkeypatch.setattr(mobile, "_last_exit_code", 0)

    assert mobile.main([]) == 0

    assert calls == [("from pipe", None, False)]


def test_main_repl_turn_does_not_reprint_user_block(monkeypatch, capsys):
    class Tty:
        def isatty(self) -> bool:
            return True

    prompts = iter(["hello", None])
    calls = []
    monkeypatch.setattr(mobile.sys, "stdin", Tty())
    monkeypatch.setattr(mobile, "read_prompt", lambda: next(prompts))
    monkeypatch.setattr(
        mobile,
        "run_turn",
        lambda prompt, session_id=None, show_user_block=True, yolo=False: (
            calls.append((prompt, session_id, show_user_block, yolo)) or "new-id"
        ),
    )

    assert mobile.main([]) == 0

    assert calls == [("hello", None, False, False)]
    assert "USER" not in capsys.readouterr().out


def test_main_one_shot_returns_codex_exit_code(monkeypatch):
    class NonTty:
        def isatty(self) -> bool:
            return False

        def read(self) -> str:
            return ""

    def fake_run_turn(prompt, session_id=None, show_user_block=True, yolo=False):
        mobile._last_exit_code = 7
        return session_id

    monkeypatch.setattr(mobile.sys, "stdin", NonTty())
    monkeypatch.setattr(mobile, "run_turn", fake_run_turn)
    monkeypatch.setattr(mobile, "_last_exit_code", 0)

    assert mobile.main(["resume", "bad-id", "hello"]) == 7

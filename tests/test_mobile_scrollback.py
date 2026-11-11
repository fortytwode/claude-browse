"""Mobile launches must keep full answers reachable and collapse tool details."""

from claude_browse import browse


def test_mobile_launch_owns_scrollable_history_instead_of_partial_terminal_scrollback(monkeypatch):
    monkeypatch.setattr(browse, "_use_codex_inline_mode", lambda provider: provider == "codex")
    command = browse._board_managed_title_cmd(["codex", "resume", "thread-id"], "codex")
    assert "--no-alt-screen" not in command
    assert 'tui.alternate_screen="always"' in command
    assert "tui.fullscreen_transcript=true" in command
    assert "tui.raw_output_mode=false" in command


def test_mobile_launch_collapses_tool_details_even_with_explicit_inline_flag(monkeypatch):
    monkeypatch.setattr(browse, "_use_codex_inline_mode", lambda provider: provider == "codex")
    command = browse._board_managed_title_cmd(
        ["codex", "--no-alt-screen", "resume", "thread-id"], "codex"
    )
    assert command.count("--no-alt-screen") == 1
    assert "tui.raw_output_mode=false" in command


def test_mobile_launch_keeps_explicit_rendering_override_last(monkeypatch):
    monkeypatch.setattr(browse, "_use_codex_inline_mode", lambda provider: provider == "codex")
    command = browse._board_managed_title_cmd(
        ["codex", "-c", "tui.raw_output_mode=true", "resume", "thread-id"], "codex"
    )
    assert command.index("tui.raw_output_mode=false") < command.index("tui.raw_output_mode=true")


def test_desktop_launch_keeps_normal_rendering(monkeypatch):
    monkeypatch.setattr(browse, "_use_codex_inline_mode", lambda provider: False)
    monkeypatch.delenv("AGENT_BOARD_MANAGED_TERMINAL_TITLE", raising=False)
    command = ["codex", "resume", "thread-id"]
    assert browse._board_managed_title_cmd(command, "codex") == command

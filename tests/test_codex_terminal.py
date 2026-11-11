from __future__ import annotations

import pytest

from claude_browse import codex_terminal


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], []),
        (["resume", "--last"], ["resume", "--last"]),
        (["--yolo", "fork", "thread-id"], ["--yolo", "fork", "thread-id"]),
        (["start", "a prompt with spaces"], ["a prompt with spaces"]),
        (["--yolo", "start", "hello"], ["--yolo", "hello"]),
    ],
)
def test_compatibility_launcher_executes_native_inline_codex(monkeypatch, args, expected):
    calls = []
    monkeypatch.setenv("CODEX_REAL_BINARY", "/opt/bin/codex")
    monkeypatch.setattr(codex_terminal.os, "execvp", lambda *call: calls.append(call))

    codex_terminal.main(args)

    assert calls == [
        (
            "/opt/bin/codex",
            [
                "/opt/bin/codex",
                "-c",
                'tui.alternate_screen="always"',
                "-c",
                "tui.fullscreen_transcript=true",
                "-c",
                "tui.raw_output_mode=false",
                *expected,
            ],
        )
    ]


def test_legacy_flag_does_not_silently_change_clients(monkeypatch, capsys):
    monkeypatch.setattr(codex_terminal.os, "execvp", lambda *args: pytest.fail("must not exec"))
    assert codex_terminal.main(["--legacy"]) == 2
    assert "codex-mobile-json" in capsys.readouterr().err

import socket
import tempfile
from pathlib import Path

import pytest

from claude_browse import browse


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("AGENT_BOARD_MANAGED_TERMINAL_TITLE", raising=False)
    path = tmp_path / "app-server-control" / "app-server-control.sock"
    path.parent.mkdir()
    return path


def test_broken_daemon_socket_uses_private_server(endpoint, capsys):
    endpoint.symlink_to(endpoint.parent / "deleted" / "socket")
    command = ["codex", "fork", "saved-thread"]
    result = browse._board_managed_title_cmd(command, "codex")
    assert result == ["codex", "--no-daemon", "fork", "saved-thread"]
    assert command == ["codex", "fork", "saved-thread"]
    assert "active sessions" in capsys.readouterr().err


def test_missing_endpoint_keeps_normal_daemon_startup(endpoint):
    command = ["codex", "resume", "saved-thread"]
    assert browse._board_managed_title_cmd(command, "codex") == command


def test_working_daemon_symlink_is_unchanged(endpoint):
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="cb-") as directory, socket.socket(socket.AF_UNIX) as server:
        target = Path(directory) / "live.sock"
        server.bind(str(target))
        endpoint.symlink_to(target)
        command = ["codex", "resume", "saved-thread"]
        assert browse._board_managed_title_cmd(command, "codex") == command


@pytest.mark.parametrize("options", [["--no-daemon"], ["--remote", "unix://elsewhere"], ["--remote=unix://elsewhere"]])
def test_explicit_connection_choice_is_preserved(endpoint, options):
    endpoint.symlink_to(endpoint.parent / "deleted")
    command = ["codex", *options, "resume", "saved-thread"]
    assert browse._board_managed_title_cmd(command, "codex") == command


def test_other_providers_are_unchanged(endpoint):
    endpoint.symlink_to(endpoint.parent / "deleted")
    command = ["claude", "--resume", "saved-thread"]
    assert browse._board_managed_title_cmd(command, "claude") == command

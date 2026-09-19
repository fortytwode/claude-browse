"""The board services must run the revision that was just installed."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def relay_installer(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "install_board_relay", REPO_ROOT / "scripts/install_board_relay.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["install_board_relay"] = module
    spec.loader.exec_module(module)

    home = tmp_path / "home"
    (home / "Library/LaunchAgents").mkdir(parents=True)
    state = home / ".claude/agent-board"
    state.mkdir(parents=True)
    (state / "mission-control-relay.token").write_text("token")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return module


def _run_install(module, monkeypatch, *, already_loaded: bool, kickstart_rc: int = 0):
    calls: list[list[str]] = []

    def fake_run(command, *args, **kwargs):
        calls.append(list(command))
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(command, 0 if already_loaded else 1)
        if command[:2] == ["launchctl", "kickstart"]:
            return subprocess.CompletedProcess(command, kickstart_rc)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object())
    module.install()
    return calls


def test_running_services_are_restarted_onto_the_new_revision(
    relay_installer, monkeypatch, capsys
):
    calls = _run_install(relay_installer, monkeypatch, already_loaded=True)

    kickstarted = [c for c in calls if c[:3] == ["launchctl", "kickstart", "-k"]]
    # A launchd service keeps running the code it started with, so
    # `git pull && ./install.sh` used to leave the previous revision live in
    # memory until the next reboot.
    assert len(kickstarted) == 2
    assert any("agent-board-backend" in c[-1] for c in kickstarted)
    assert any("agent-board-relay" in c[-1] for c in kickstarted)
    assert not [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]
    assert "restarted on the current revision" in capsys.readouterr().out


def test_unloaded_services_are_bootstrapped_rather_than_kickstarted(
    relay_installer, monkeypatch
):
    calls = _run_install(relay_installer, monkeypatch, already_loaded=False)

    assert len([c for c in calls if c[:2] == ["launchctl", "bootstrap"]]) == 2
    assert not [c for c in calls if c[:2] == ["launchctl", "kickstart"]]


def test_a_failed_restart_says_so_instead_of_claiming_success(
    relay_installer, monkeypatch, capsys
):
    _run_install(relay_installer, monkeypatch, already_loaded=True, kickstart_rc=1)

    assert "restart it to pick up this revision" in capsys.readouterr().out

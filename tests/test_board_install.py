"""Tests for scripts/install_agent_board.py: idempotent wiring, dedupe of
stale/duplicate hook commands, --check, and Codex hooks.json."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "install_agent_board.py"


@pytest.fixture
def installer(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("install_agent_board", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_agent_board"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    (tmp_path / ".codex").mkdir()
    # Pretend the board-sync venv doesn't exist yet (system python variant).
    monkeypatch.setattr(mod, "VENV_PYTHON", tmp_path / "no-such-venv" / "bin" / "python")
    monkeypatch.setattr(mod, "_overlap_check", lambda settings: None)
    return mod


def _cmds(settings, event):
    return [h["command"] for g in settings["hooks"][event] for h in g["hooks"]]


def _read(path):
    return json.loads(Path(path).read_text())


def test_fresh_install_wires_claude_once_and_codex_once(installer, tmp_path):
    assert installer.run() == 0

    settings = _read(installer._settings_path())
    assert _cmds(settings, "SessionStart") == [installer.HOOK_CMD]
    assert _cmds(settings, "Stop") == [installer.HOOK_CMD, installer.sync_cmd()]
    stop_entries = settings["hooks"]["Stop"][0]["hooks"]
    assert stop_entries[1] == {"type": "command", "command": installer.sync_cmd(),
                               "timeout": 15, "async": True}
    assert settings["statusLine"]["command"] == installer.STATUSLINE_CMD
    assert settings["agentPushNotifEnabled"] is False

    codex = _read(installer._codex_hooks_path())
    assert set(codex) == {"hooks"}  # HooksFile shape: top-level "hooks" wrapper
    assert set(codex["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop",
                                   "PermissionRequest", "SessionEnd"}
    stop = codex["hooks"]["Stop"][0]["hooks"]
    assert stop[0] == {"type": "command", "command": installer.CODEX_HOOK_CMD, "timeout_sec": 10}
    assert stop[1] == {"type": "command", "command": installer.sync_cmd(),
                       "timeout_sec": 15, "async": True}
    assert "timeout" not in stop[0]  # Codex spells it timeout_sec


def test_second_run_is_a_no_op(installer, capsys):
    installer.run()
    before_claude = installer._settings_path().read_text()
    before_codex = installer._codex_hooks_path().read_text()
    capsys.readouterr()

    installer.run()

    out = capsys.readouterr().out
    assert "already wired cleanly" in out
    assert installer._settings_path().read_text() == before_claude
    assert installer._codex_hooks_path().read_text() == before_codex
    assert not list(installer._settings_path().parent.glob("*.bak-*"))  # no backup on no-op


def test_creating_the_venv_replaces_the_stale_sync_variant_instead_of_appending(
    installer, tmp_path, monkeypatch, capsys
):
    """The live bug: system-python sync cmd wired first, venv created later,
    re-run appended the venv variant and BOTH fired -> every Slack alert twice."""
    installer.run()
    old_sync = installer.sync_cmd()

    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    monkeypatch.setattr(installer, "VENV_PYTHON", venv_python)
    new_sync = installer.sync_cmd()
    assert new_sync != old_sync

    capsys.readouterr()
    installer.run()
    out = capsys.readouterr().out

    settings = _read(installer._settings_path())
    for event in ("Stop", "Notification", "SessionEnd"):
        cmds = _cmds(settings, event)
        assert cmds == [installer.HOOK_CMD, new_sync], event
        assert old_sync not in cmds
    assert f"Removed Stop hook: {old_sync}" in out

    codex = _read(installer._codex_hooks_path())
    for event in ("Stop", "PermissionRequest", "SessionEnd"):
        cmds = [h["command"] for g in codex["hooks"][event] for h in g["hooks"]]
        assert cmds == [installer.CODEX_HOOK_CMD, new_sync], event


def test_exact_duplicate_registrations_are_collapsed_and_foreign_hooks_kept(installer):
    path = installer._settings_path()
    path.parent.mkdir(parents=True)
    sync = installer.sync_cmd()
    path.write_text(json.dumps({
        "hooks": {
            "Stop": [
                {"hooks": [
                    {"type": "command", "command": installer.HOOK_CMD, "timeout": 10},
                    {"type": "command", "command": sync, "timeout": 15, "async": True},
                    {"type": "command", "command": "say 'done'", "timeout": 5},
                ]},
                {"hooks": [
                    {"type": "command", "command": sync, "timeout": 15, "async": True},
                    {"type": "command", "command": installer.HOOK_CMD, "timeout": 10},
                ]},
            ],
        },
    }))

    assert installer.run(include_codex=False) == 0

    settings = _read(path)
    assert _cmds(settings, "Stop") == [installer.HOOK_CMD, sync, "say 'done'"]
    assert len(settings["hooks"]["Stop"]) == 1  # emptied duplicate group dropped


def test_check_mode_reports_drift_without_writing(installer, capsys):
    path = installer._settings_path()
    path.parent.mkdir(parents=True)
    sync = installer.sync_cmd()
    stale = "/old/venv/bin/python " + installer.AGENT_BOARD + " sync push"
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": installer.HOOK_CMD},
        {"type": "command", "command": sync, "async": True},
        {"type": "command", "command": sync, "async": True},
        {"type": "command", "command": stale, "async": True},
    ]}]}}))
    before = path.read_text()

    rc = installer.run(check_only=True)
    out = capsys.readouterr().out

    assert rc == 1
    assert "duplicate registration (2x)" in out
    assert "stale sync variant" in out
    assert "SessionStart: missing" in out
    assert path.read_text() == before  # never writes in --check


def test_check_mode_clean_exit_zero(installer, capsys):
    installer.run()
    capsys.readouterr()
    assert installer.run(check_only=True) == 0
    out = capsys.readouterr().out
    assert "OK: hooks + statusLine wired once" in out
    assert "OK: Codex hooks wired once" in out


def test_codex_skipped_when_codex_home_missing(installer, tmp_path, capsys):
    import shutil

    shutil.rmtree(tmp_path / ".codex")
    assert installer.run() == 0
    assert "Codex home" in capsys.readouterr().out
    assert not installer._codex_hooks_path().exists()


def test_codex_feature_flag_off_is_reported(installer, tmp_path, capsys):
    (tmp_path / ".codex" / "config.toml").write_text('[features]\nhooks = false\n')
    installer.run()
    assert "hooks = false" in capsys.readouterr().out


def test_codex_existing_foreign_hooks_survive(installer):
    path = installer._codex_hooks_path()
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "shell", "hooks": [
        {"type": "command", "command": "/usr/local/bin/guard.sh", "timeout_sec": 5}]}]}}))

    installer.run()

    codex = _read(path)
    assert codex["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/usr/local/bin/guard.sh"
    assert codex["hooks"]["PreToolUse"][0]["matcher"] == "shell"
    assert "Stop" in codex["hooks"]

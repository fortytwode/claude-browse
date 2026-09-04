"""Tests for canonical Claude and Codex Agent Board hook installation."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "install_agent_board.py"
README = REPO_ROOT / "README.md"
INSTALL_SH = REPO_ROOT / "install.sh"


@pytest.fixture
def installer(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("install_agent_board", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_agent_board"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    (tmp_path / ".codex").mkdir()
    monkeypatch.setattr(mod, "_overlap_check", lambda settings: None)
    return mod


def _entries(settings, event):
    return [entry for group in settings["hooks"][event] for entry in group["hooks"]]


def _read(path):
    return json.loads(Path(path).read_text())


def test_fresh_install_wires_one_post_commit_hook_per_event(installer, capsys):
    assert installer.run() == 0

    settings = _read(installer._settings_path())
    assert set(settings["hooks"]) == {
        "SessionStart", "UserPromptSubmit", "Stop", "Notification", "SessionEnd",
    }
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "Notification", "SessionEnd"):
        assert settings["hooks"][event] == [{"hooks": [{
            "type": "command", "command": installer.HOOK_CMD, "timeout": 10,
        }]}]
    assert settings["statusLine"] == {
        "type": "command",
        "command": installer.STATUSLINE_CMD,
        "padding": 0,
        "refreshInterval": 5,
    }
    assert settings["agentPushNotifEnabled"] is False

    codex = _read(installer._codex_hooks_path())
    assert set(codex) == {"hooks"}
    assert set(codex["hooks"]) == {
        "SessionStart", "UserPromptSubmit", "Stop", "PermissionRequest", "Interrupt",
        "SessionEnd",
    }
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "PermissionRequest"):
        assert codex["hooks"][event] == [{"hooks": [{
            "type": "command", "command": installer.CODEX_HOOK_CMD, "timeout": 10,
        }]}]
    for event in ("Interrupt", "SessionEnd"):
        assert codex["hooks"][event] == [{"hooks": [{
            "type": "command", "command": installer.CODEX_HOOK_CMD, "timeout": 3,
        }]}]
    assert "sync push" not in json.dumps(settings)
    assert "sync push" not in json.dumps(codex)
    assert "async" not in json.dumps(codex)
    assert "timeout_sec" not in json.dumps(codex)
    assert "ACTION REQUIRED" in capsys.readouterr().out


def test_second_run_is_byte_identical_and_creates_no_backup(installer, capsys):
    installer.run()
    before_claude = installer._settings_path().read_bytes()
    before_codex = installer._codex_hooks_path().read_bytes()
    backups_before = list(installer._settings_path().parent.glob("*.bak-*"))
    backups_before += list(installer._codex_hooks_path().parent.glob("*.bak-*"))
    capsys.readouterr()

    installer.run()

    out = capsys.readouterr().out
    assert "already wired cleanly" in out
    assert "ACTION REQUIRED" not in out
    assert installer._settings_path().read_bytes() == before_claude
    assert installer._codex_hooks_path().read_bytes() == before_codex
    backups_after = list(installer._settings_path().parent.glob("*.bak-*"))
    backups_after += list(installer._codex_hooks_path().parent.glob("*.bak-*"))
    assert backups_after == backups_before


@pytest.mark.parametrize(
    "bad_entry",
    [
        {"type": "command", "command": "CODEX_HOOK", "timeout_sec": 10},
        {"type": "command", "command": "CODEX_HOOK", "timeout": 999},
        {"type": "command", "command": "CODEX_HOOK", "timeout": 10, "async": True},
    ],
)
def test_codex_option_drift_is_reported_and_repaired(installer, bad_entry, capsys):
    bad_entry = {**bad_entry, "command": installer.CODEX_HOOK_CMD}
    path = installer._codex_hooks_path()
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [bad_entry]}]}}))
    before = path.read_text()

    assert installer.run(check_only=True) == 1
    assert "Stop" in capsys.readouterr().out
    assert path.read_text() == before

    assert installer.run() == 0
    codex = _read(path)
    assert codex["hooks"]["Stop"] == [{"hooks": [{
        "type": "command", "command": installer.CODEX_HOOK_CMD, "timeout": 10,
    }]}]


def test_codex_interrupt_drift_is_reported_and_repaired_without_changing_claude(
    installer, capsys
):
    installer.run()
    capsys.readouterr()
    claude_before = installer._settings_path().read_bytes()
    path = installer._codex_hooks_path()
    codex = _read(path)
    codex["hooks"]["Interrupt"] = [{
        "matcher": "tool",
        "hooks": [{
            "type": "command", "command": installer.CODEX_HOOK_CMD, "timeout": 10,
        }],
    }]
    path.write_text(json.dumps(codex))

    assert installer.run(check_only=True) == 1
    assert "Interrupt" in capsys.readouterr().out
    assert installer.run() == 0

    repaired = _read(path)
    assert repaired["hooks"]["Interrupt"] == [
        {"matcher": "tool", "hooks": []},
        {"hooks": [{
            "type": "command", "command": installer.CODEX_HOOK_CMD, "timeout": 3,
        }]},
    ]
    assert len(_entries(repaired, "Interrupt")) == 1
    assert installer._settings_path().read_bytes() == claude_before


def test_managed_hook_moves_out_of_matcher_but_foreign_matched_group_stays_exact(
    installer, capsys
):
    path = installer._codex_hooks_path()
    foreign = {"type": "command", "command": "/usr/local/bin/guard", "timeout": 4}
    path.write_text(json.dumps({
        "custom": {"preserved": True},
        "hooks": {
            "Stop": [{
                "matcher": "shell",
                "description": "keep this group",
                "hooks": [foreign, {
                    "type": "command", "command": installer.CODEX_HOOK_CMD, "timeout": 10,
                }],
            }],
            "PreToolUse": [{"matcher": "python", "hooks": [foreign]}],
        },
    }))

    assert installer.run(check_only=True) == 1
    assert "Stop" in capsys.readouterr().out
    assert installer.run() == 0

    codex = _read(path)
    assert codex["custom"] == {"preserved": True}
    assert codex["hooks"]["PreToolUse"] == [{"matcher": "python", "hooks": [foreign]}]
    assert codex["hooks"]["Stop"] == [
        {"matcher": "shell", "description": "keep this group", "hooks": [foreign]},
        {"hooks": [{
            "type": "command", "command": installer.CODEX_HOOK_CMD, "timeout": 10,
        }]},
    ]


def test_old_paths_sync_variants_duplicates_and_wrong_provider_are_removed(installer):
    path = installer._settings_path()
    path.parent.mkdir(parents=True)
    foreign = {"type": "command", "command": "say done", "timeout": 5}
    path.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [
            {"type": "command", "command": "/old/repo/agent-board hook", "timeout": 10},
            {"type": "command", "command": installer.CODEX_HOOK_CMD, "timeout": 10},
            {"type": "command", "command": (
                "/old/venv/python /old/repo/agent-board sync push"
            ), "timeout": 15, "async": True},
            foreign,
        ]},
        {"hooks": [{"type": "command", "command": installer.HOOK_CMD, "timeout": 999}]},
    ]}}))

    assert installer.run(include_codex=False) == 0

    settings = _read(path)
    assert settings["hooks"]["Stop"] == [
        {"hooks": [foreign]},
        {"hooks": [{"type": "command", "command": installer.HOOK_CMD, "timeout": 10}]},
    ]
    assert sum("agent-board" in entry["command"] for entry in _entries(settings, "Stop")) == 1


def test_check_mode_clean_definitions_warns_that_trust_is_unverifiable(installer, capsys):
    installer.run()
    capsys.readouterr()

    assert installer.run(check_only=True) == 0

    out = capsys.readouterr().out
    assert "OK: Codex hook definitions match" in out
    assert "trust cannot be verified" in out
    assert "/hooks" in out


def test_check_mode_reports_hooks_false_and_exits_nonzero(installer, tmp_path, capsys):
    installer.run()
    (tmp_path / ".codex" / "config.toml").write_text("[features]\nhooks = false\n")
    capsys.readouterr()

    assert installer.run(check_only=True) == 1

    out = capsys.readouterr().out
    assert "hooks = false" in out
    assert "/hooks" in out


def test_install_warns_but_does_not_edit_hooks_false(installer, tmp_path, capsys):
    config = tmp_path / ".codex" / "config.toml"
    config.write_text("[features]\nhooks = false\n")

    assert installer.run() == 0

    assert config.read_text() == "[features]\nhooks = false\n"
    assert "Codex will ignore" in capsys.readouterr().out


def test_codex_skipped_when_codex_home_missing(installer, tmp_path, capsys):
    import shutil

    shutil.rmtree(tmp_path / ".codex")
    assert installer.run() == 0
    assert "Codex home" in capsys.readouterr().out
    assert not installer._codex_hooks_path().exists()


def test_readme_documents_hook_contract_and_trust_workflow():
    text = README.read_text()
    agent_board = text[text.index("## Agent Board") :]

    assert "`timeout`" in agent_board
    assert "`timeout_sec`" not in agent_board
    assert "one hook" in agent_board.lower()
    assert "detached" in agent_board.lower()
    assert "SessionEnd" in agent_board and "3 seconds" in agent_board
    assert "`/hooks`" in agent_board
    assert "trust" in agent_board.lower()
    assert "cannot verify" in agent_board.lower()


def test_sync_setup_documents_automatic_venv_selection_without_hook_rewiring():
    installer_text = INSTALL_SH.read_text()
    readme_text = README.read_text()

    assert "automatically uses it on the next hook event" in installer_text
    assert "update the async sync hook commands" not in installer_text
    assert "re-run install.sh after creating the venv" not in installer_text
    assert "automatically uses `.venv/bin/python`" in readme_text
    assert "re-run so detached publication" not in readme_text

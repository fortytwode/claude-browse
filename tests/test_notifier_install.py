"""Tests for the dedicated macOS Agent Board notification app installer."""

from __future__ import annotations

import importlib.util
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts/install_notifier_app.py"


@pytest.fixture
def notifier_installer(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("install_notifier_app", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["install_notifier_app"] = module
    spec.loader.exec_module(module)
    monkeypatch.setenv("AGENT_BOARD_NOTIFIER_INSTALL_DIR", str(tmp_path / "Applications"))
    return module


def test_info_plist_has_stable_dedicated_notification_identity(notifier_installer):
    info = notifier_installer._info_plist("hash")
    assert info["CFBundleIdentifier"] == "com.fortytwode.agent-board-notifier"
    assert info["CFBundleDisplayName"] == "Agent Board"
    assert info["LSUIElement"] is True
    command = notifier_installer._codesign_command(Path("Agent Board.app"))
    assert "--requirements" in command
    assert notifier_installer.SIGNING_REQUIREMENT == (
        '=designated => identifier "com.fortytwode.agent-board-notifier"'
    )


def test_current_install_requires_matching_source_hash_and_executable(
    notifier_installer,
):
    bundle = notifier_installer.app_path()
    executable = notifier_installer.executable_path(bundle)
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    executable.chmod(0o755)
    with (bundle / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump(
            {"AgentBoardBuildHash": notifier_installer._build_hash()}, handle
        )

    assert notifier_installer.is_current(bundle)
    executable.unlink()
    assert not notifier_installer.is_current(bundle)


def test_current_install_rejects_non_executable_helper(notifier_installer):
    bundle = notifier_installer.app_path()
    executable = notifier_installer.executable_path(bundle)
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    executable.chmod(0o644)
    with (bundle / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump(
            {"AgentBoardBuildHash": notifier_installer._build_hash()}, handle
        )

    assert not notifier_installer.is_current(bundle)


def test_non_macos_install_skips_cleanly(notifier_installer, monkeypatch):
    monkeypatch.setattr(notifier_installer.sys, "platform", "linux")
    ok, status = notifier_installer.install()
    assert not ok
    assert "macOS only" in status


def test_missing_compiler_reports_fallback(notifier_installer, monkeypatch):
    monkeypatch.setattr(notifier_installer.sys, "platform", "darwin")
    monkeypatch.setattr(notifier_installer, "_swiftc", lambda: None)
    ok, status = notifier_installer.install()
    assert not ok
    assert "Script Editor" in status


def test_install_builds_signs_and_atomically_replaces_bundle(notifier_installer, monkeypatch):
    monkeypatch.setattr(notifier_installer.sys, "platform", "darwin")
    monkeypatch.setattr(notifier_installer, "_swiftc", lambda: "/usr/bin/swiftc")
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[0] == "/usr/bin/swiftc":
            target = Path(command[command.index("-o") + 1])
            target.write_text("binary")
            target.chmod(0o755)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(notifier_installer.subprocess, "run", run)

    ok, status = notifier_installer.install()

    assert ok
    assert "installed dedicated notifier" in status
    assert notifier_installer.is_current()
    assert any(command[0] == "codesign" for command in commands)
    assert any(command[0] == "open" for command in commands)


def test_failed_bundle_swap_restores_previous_install(notifier_installer, monkeypatch):
    monkeypatch.setattr(notifier_installer.sys, "platform", "darwin")
    monkeypatch.setattr(notifier_installer, "_swiftc", lambda: "/usr/bin/swiftc")
    destination = notifier_installer.app_path()
    destination.mkdir(parents=True)
    marker = destination / "old-install"
    marker.write_text("preserve me")

    def run(command, **_kwargs):
        if command[0] == "/usr/bin/swiftc":
            target = Path(command[command.index("-o") + 1])
            target.write_text("binary")
            target.chmod(0o755)
        return subprocess.CompletedProcess(command, 0, "", "")

    original_rename = Path.rename

    def fail_staged_swap(path, target):
        if path.name == notifier_installer.APP_NAME and ".agent-board-notifier-" in str(path.parent):
            raise OSError("swap failed")
        return original_rename(path, target)

    monkeypatch.setattr(notifier_installer.subprocess, "run", run)
    monkeypatch.setattr(Path, "rename", fail_staged_swap)

    ok, status = notifier_installer.install()

    assert not ok
    assert "swap failed" in status
    assert marker.read_text() == "preserve me"


def test_install_sh_runs_dedicated_notifier_installer():
    text = (REPO_ROOT / "install.sh").read_text()
    assert 'python3 "$SCRIPT_DIR/scripts/install_notifier_app.py"' in text

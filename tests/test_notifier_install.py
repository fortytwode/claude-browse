"""Tests for the dedicated macOS Agent Board notification app installer."""

from __future__ import annotations

import importlib.util
import plistlib
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


def test_install_sh_runs_dedicated_notifier_installer():
    text = (REPO_ROOT / "install.sh").read_text()
    assert 'python3 "$SCRIPT_DIR/scripts/install_notifier_app.py"' in text

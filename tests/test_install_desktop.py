"""Focused tests for the native Agent Board app install paths."""

from __future__ import annotations

import importlib.util
import plistlib
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_desktop.py"


@pytest.fixture
def installer(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("install_desktop", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["install_desktop"] = module
    spec.loader.exec_module(module)
    applications = tmp_path / "Applications"
    applications.mkdir()
    home_applications = tmp_path / "home" / "Applications"
    home_applications.mkdir(parents=True)
    monkeypatch.setattr(module, "APPLICATIONS_DIR", applications)
    monkeypatch.setattr(module, "HOME_APPLICATIONS_DIR", home_applications)
    return module


def _make_bundle(installer, path: Path) -> None:
    contents = path / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": installer.BUNDLE_ID}, handle)


def test_app_path_prefers_applications_and_falls_back_when_not_writable(installer, monkeypatch):
    assert installer.app_path() == installer.APPLICATIONS_DIR / installer.APP_NAME

    monkeypatch.setattr(installer.os, "access", lambda *_args: False)

    assert installer.app_path() == installer.HOME_APPLICATIONS_DIR / installer.APP_NAME


def test_home_bundle_migrates_to_compatibility_link_and_preserves_shortcut(installer):
    canonical = installer.APPLICATIONS_DIR / installer.APP_NAME
    home_app = installer.home_compatibility_path()
    shortcut = installer.HOME_APPLICATIONS_DIR / "Agent Browse.app"
    _make_bundle(installer, canonical)
    _make_bundle(installer, home_app)
    shortcut.symlink_to(home_app)

    ok, status = installer._ensure_home_compatibility_link(canonical)

    assert ok, status
    assert str(canonical) in status
    assert home_app.is_symlink()
    assert home_app.resolve() == canonical
    assert shortcut.resolve() == canonical
    assert not list(installer.HOME_APPLICATIONS_DIR.glob(".*.migration-*"))


def test_fresh_install_creates_missing_home_applications_directory(installer):
    canonical = installer.APPLICATIONS_DIR / installer.APP_NAME
    installer.HOME_APPLICATIONS_DIR.rmdir()
    _make_bundle(installer, canonical)

    ok, status = installer._ensure_home_compatibility_link(canonical)

    assert ok, status
    assert installer.home_compatibility_path().is_symlink()
    assert installer.home_compatibility_path().resolve() == canonical


def test_home_migration_refuses_to_replace_an_unrelated_app(installer):
    canonical = installer.APPLICATIONS_DIR / installer.APP_NAME
    home_app = installer.home_compatibility_path()
    _make_bundle(installer, canonical)
    home_app.mkdir()

    ok, status = installer._ensure_home_compatibility_link(canonical)

    assert not ok
    assert "unrelated app" in status
    assert home_app.is_dir()

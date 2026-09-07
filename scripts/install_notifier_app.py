#!/usr/bin/env python3
"""Build and install Agent Board's dedicated macOS notification helper."""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SOURCE = REPO_DIR / "claude_browse/board/macos/AgentBoardNotifier.swift"
APP_NAME = "Agent Board Notifier.app"
EXECUTABLE_NAME = "AgentBoardNotifier"
BUNDLE_ID = "com.fortytwode.agent-board-notifier"
BUILD_SCHEMA = "2"
SIGNING_REQUIREMENT = f'=designated => identifier "{BUNDLE_ID}"'


def install_root() -> Path:
    override = os.environ.get("AGENT_BOARD_NOTIFIER_INSTALL_DIR")
    return Path(override).expanduser() if override else Path.home() / "Applications"


def app_path() -> Path:
    return install_root() / APP_NAME


def executable_path(bundle: Path | None = None) -> Path:
    return (bundle or app_path()) / "Contents/MacOS" / EXECUTABLE_NAME


def _build_hash() -> str:
    digest = hashlib.sha256()
    digest.update(SOURCE.read_bytes())
    digest.update(BUILD_SCHEMA.encode())
    return digest.hexdigest()


def _info_plist(build_hash: str) -> dict:
    return {
        "AgentBoardBuildHash": build_hash,
        "CFBundleDisplayName": "Agent Board",
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Agent Board",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": True,
    }


def _installed_hash(bundle: Path) -> str | None:
    try:
        with (bundle / "Contents/Info.plist").open("rb") as handle:
            return plistlib.load(handle).get("AgentBoardBuildHash")
    except (OSError, plistlib.InvalidFileException):
        return None


def is_current(bundle: Path | None = None) -> bool:
    bundle = bundle or app_path()
    executable = executable_path(bundle)
    return (
        executable.is_file()
        and os.access(executable, os.X_OK)
        and _installed_hash(bundle) == _build_hash()
    )


def _codesign_command(bundle: Path) -> list[str]:
    # A plain ad-hoc signature's designated requirement is its cdhash, which
    # changes whenever the Swift source changes. Pinning the requirement to
    # the stable bundle ID preserves macOS Notification/Focus identity across
    # locally compiled upgrades.
    return [
        "codesign",
        "--force",
        "--sign",
        "-",
        "--timestamp=none",
        "--requirements",
        SIGNING_REQUIREMENT,
        str(bundle),
    ]


def _swiftc() -> str | None:
    direct = shutil.which("swiftc")
    if direct:
        return direct
    try:
        result = subprocess.run(
            ["xcrun", "--find", "swiftc"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def install() -> tuple[bool, str]:
    """Return ``(dedicated_app_available, human-readable status)``."""
    if sys.platform != "darwin":
        return False, "dedicated notifier skipped (macOS only)"

    destination = app_path()
    if is_current(destination):
        return True, f"dedicated notifier already current at {destination}"

    compiler = _swiftc()
    if not compiler:
        return False, "Swift compiler not found; notifications will use Script Editor"

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=".agent-board-notifier-", dir=destination.parent))
    staged = stage_root / APP_NAME
    contents = staged / "Contents"
    macos_dir = contents / "MacOS"
    macos_dir.mkdir(parents=True)
    target_executable = executable_path(staged)

    try:
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump(_info_plist(_build_hash()), handle)
        subprocess.run(
            [compiler, "-O", str(SOURCE), "-o", str(target_executable)],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        subprocess.run(
            _codesign_command(staged),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

        backup = destination.with_name(destination.name + ".previous")
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            destination.rename(backup)
        try:
            staged.rename(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = exc.stderr.strip().splitlines()[-1]
        return False, f"dedicated notifier build failed ({detail}); using Script Editor"
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)

    subprocess.run(
        ["open", "-gj", "-n", str(destination), "--args", "--request-permission"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    return True, f"installed dedicated notifier at {destination}"


def check() -> tuple[bool, str]:
    destination = app_path()
    if sys.platform != "darwin":
        return True, "dedicated notifier not required on this platform"
    if not destination.exists():
        return False, f"dedicated notifier is not installed at {destination}"
    if not is_current(destination):
        return False, f"dedicated notifier at {destination} is stale or incomplete"
    return True, f"dedicated notifier is current at {destination}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ok, status = check() if "--check" in argv else install()
    print(f"  {status}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

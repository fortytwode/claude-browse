#!/usr/bin/env python3
"""Build and install Agent Board's native macOS desktop app."""

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
SOURCE = REPO_DIR / "claude_browse/board/macos/AgentBoardDesktop.swift"
APP_NAME = "Agent Board.app"
EXECUTABLE_NAME = "AgentBoardDesktop"
BUNDLE_ID = "com.fortytwode.agent-board-desktop"
BUILD_SCHEMA = "1"
SIGNING_REQUIREMENT = f'=designated => identifier "{BUNDLE_ID}"'
APPLICATIONS_DIR = Path("/Applications")
HOME_APPLICATIONS_DIR = Path.home() / "Applications"


def app_path() -> Path:
    directory = APPLICATIONS_DIR if os.access(APPLICATIONS_DIR, os.W_OK) else HOME_APPLICATIONS_DIR
    return directory / APP_NAME


def home_compatibility_path() -> Path:
    return HOME_APPLICATIONS_DIR / APP_NAME


def executable_path(bundle: Path | None = None) -> Path:
    return (bundle or app_path()) / "Contents/MacOS" / EXECUTABLE_NAME


def _python_path() -> str:
    candidate = REPO_DIR / ".venv/bin/python"
    if candidate.is_file():
        try:
            result = subprocess.run(
                [str(candidate), "-c", "import claude_browse.web"],
                cwd=REPO_DIR,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return str(candidate.absolute())
        except (OSError, subprocess.SubprocessError):
            pass
    return str(Path(sys.executable).resolve())


def _build_hash() -> str:
    digest = hashlib.sha256()
    digest.update(SOURCE.read_bytes())
    digest.update(BUILD_SCHEMA.encode())
    digest.update(REPO_DIR.as_posix().encode())
    digest.update(_python_path().encode())
    return digest.hexdigest()


def _info_plist(build_hash: str) -> dict:
    return {
        "AgentBoardBuildHash": build_hash,
        "AgentBoardPython": _python_path(),
        "AgentBoardRepository": str(REPO_DIR),
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
        "NSAppleEventsUsageDescription": (
            "Agent Board uses Apple Events to focus the terminal for a session."
        ),
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
    return executable.is_file() and os.access(executable, os.X_OK) and _installed_hash(bundle) == _build_hash()


def _bundle_id(bundle: Path) -> str | None:
    try:
        with (bundle / "Contents/Info.plist").open("rb") as handle:
            return plistlib.load(handle).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException):
        return None


def _ensure_home_compatibility_link(canonical: Path) -> tuple[bool, str]:
    compatibility = home_compatibility_path()
    if compatibility.is_symlink():
        try:
            if compatibility.resolve(strict=True) == canonical.resolve(strict=True):
                return True, f"compatibility link at {compatibility} is current"
        except OSError:
            pass
        return False, f"refusing to replace unrelated link at {compatibility}"
    if compatibility.exists() and _bundle_id(compatibility) != BUNDLE_ID:
        return False, f"refusing to replace unrelated app at {compatibility}"

    backup = compatibility.with_name(f".{APP_NAME}.migration-{os.getpid()}")
    try:
        compatibility.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            return False, f"migration backup already exists at {backup}"
        if compatibility.exists():
            compatibility.rename(backup)
        compatibility.symlink_to(canonical)
        if compatibility.resolve(strict=True) != canonical.resolve(strict=True):
            raise OSError("compatibility link did not resolve to the installed app")
        if backup.exists():
            shutil.rmtree(backup)
    except OSError as exc:
        if compatibility.is_symlink():
            compatibility.unlink()
        if backup.exists() and not compatibility.exists():
            backup.rename(compatibility)
        return False, f"could not migrate home app path ({exc})"
    return True, f"home path now links to {canonical}"


def _swiftc() -> str | None:
    compiler = shutil.which("swiftc")
    if compiler:
        return compiler
    try:
        result = subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _codesign_command(bundle: Path) -> list[str]:
    return [
        "codesign", "--force", "--sign", "-", "--timestamp=none",
        "--requirements", SIGNING_REQUIREMENT, str(bundle),
    ]


def install(open_after: bool = False) -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "native desktop app installation is macOS only"
    if not SOURCE.is_file():
        return False, f"desktop app source is missing: {SOURCE}"
    destination = app_path()
    if is_current(destination):
        if destination.parent == APPLICATIONS_DIR:
            ok, migration_status = _ensure_home_compatibility_link(destination)
            if not ok:
                return False, migration_status
        else:
            migration_status = f"using fallback install at {destination}"
        if open_after:
            subprocess.run(["open", str(destination)], check=False)
        return True, f"desktop app current at {destination}; {migration_status}"
    compiler = _swiftc()
    if not compiler:
        return False, "Swift compiler not found"

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=".agent-board-desktop-", dir=destination.parent))
    staged = stage_root / APP_NAME
    contents = staged / "Contents"
    macos_dir = contents / "MacOS"
    macos_dir.mkdir(parents=True)
    try:
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump(_info_plist(_build_hash()), handle)
        subprocess.run(
            [compiler, "-O", str(SOURCE), "-framework", "AppKit", "-framework", "WebKit", "-o", str(executable_path(staged))],
            capture_output=True, text=True, timeout=180, check=True,
        )
        subprocess.run(_codesign_command(staged), capture_output=True, text=True, timeout=30, check=True)

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
        return False, f"desktop app build failed ({detail})"
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)

    if open_after:
        subprocess.run(["open", str(destination)], check=False)
    if destination.parent == APPLICATIONS_DIR:
        ok, migration_status = _ensure_home_compatibility_link(destination)
        if not ok:
            return False, migration_status
    else:
        migration_status = f"using fallback install at {destination}"
    return True, f"installed desktop app at {destination}; {migration_status}. If already running, quit and reopen to use the update."


def check() -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "native desktop app check is macOS only"
    destination = app_path()
    if not destination.exists():
        return False, f"desktop app is not installed at {destination}"
    if destination.parent == APPLICATIONS_DIR and destination.is_symlink():
        return False, f"canonical app at {destination} must be a real bundle"
    if not is_current(destination):
        return False, f"desktop app at {destination} is stale or incomplete"
    if destination.parent == APPLICATIONS_DIR:
        compatibility = home_compatibility_path()
        if not compatibility.is_symlink() or compatibility.resolve() != destination.resolve():
            return False, f"home compatibility path at {compatibility} is not linked to the canonical app"
    return True, f"desktop app is current at {destination}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--check" in argv:
        ok, status = check()
    else:
        ok, status = install(open_after="--open" in argv)
    print(f"  {status}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

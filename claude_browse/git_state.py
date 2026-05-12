"""Lightweight local repo-state inspection for restart cards.

This stays intentionally narrow: one cheap git status probe gives enough
context to tell whether the original working directory still exists, whether
it's a git repo, which branch it is on, and whether there are uncommitted
changes.
"""

from __future__ import annotations

import os
import subprocess


def inspect_repo_state(cwd: str | None) -> dict[str, object]:
    """Return a small restart-oriented view of the current repo state."""
    if not cwd:
        return {
            "cwd_exists": False,
            "is_git": False,
            "branch": "",
            "dirty": False,
            "changed_files": 0,
            "summary": "Original working directory is unknown.",
        }

    if not os.path.isdir(cwd):
        return {
            "cwd_exists": False,
            "is_git": False,
            "branch": "",
            "dirty": False,
            "changed_files": 0,
            "summary": "Original working directory no longer exists.",
        }

    try:
        result = subprocess.run(
            ["git", "-C", cwd, "status", "--short", "--branch"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "cwd_exists": True,
            "is_git": False,
            "branch": "",
            "dirty": False,
            "changed_files": 0,
            "summary": "Current folder exists but git state could not be inspected.",
        }

    if result.returncode != 0:
        return {
            "cwd_exists": True,
            "is_git": False,
            "branch": "",
            "dirty": False,
            "changed_files": 0,
            "summary": "Current folder exists but is not inside a git work tree.",
        }

    lines = [line.rstrip("\n") for line in result.stdout.splitlines()]
    branch = ""
    if lines and lines[0].startswith("## "):
        branch = lines[0][3:]
        if "..." in branch:
            branch = branch.split("...", 1)[0]

    changed_files = sum(1 for line in lines[1:] if line.strip())
    dirty = changed_files > 0
    if branch:
        if dirty:
            summary = (
                f"Branch `{branch}` with {changed_files} uncommitted file"
                f"{'' if changed_files == 1 else 's'}."
            )
        else:
            summary = f"Branch `{branch}` with a clean working tree."
    else:
        summary = "Git work tree detected."

    return {
        "cwd_exists": True,
        "is_git": True,
        "branch": branch,
        "dirty": dirty,
        "changed_files": changed_files,
        "summary": summary,
    }

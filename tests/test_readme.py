"""README regression checks for documented browser behavior."""

from __future__ import annotations

from pathlib import Path


README = Path(__file__).resolve().parents[1] / "README.md"


def _readme_text() -> str:
    return README.read_text()


def test_readme_documents_target_app_browsers():
    text = _readme_text()
    assert "codex-browse" in text
    assert "opens everything in Claude" in text
    assert "opens everything in CodeX" in text
    assert "Cross-app open is not a true native resume." in text


def test_readme_no_longer_documents_claude_resume():
    text = _readme_text()
    assert "claude-resume" not in text

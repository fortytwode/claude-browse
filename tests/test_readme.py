"""README regression checks for documented browser behavior."""
from __future__ import annotations

from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"


def _readme_text() -> str:
    return README.read_text()


def test_readme_documents_target_app_browsers():
    text = _readme_text()
    assert "codex-browse" in text
    assert "gemini-browse" in text
    assert "copilot-browse" in text
    assert "cursor-browse" in text
    assert "opens everything in Claude" in text
    assert "opens everything in CodeX" in text
    assert "opens everything in Gemini" in text
    assert "opens everything in Copilot" in text
    assert "opens everything in Cursor" in text
    assert "Cross-provider open is not a true native resume." in text
    assert "Resume software work across Claude Code" in text
    assert "restart card" in text.lower()
    assert "Ctrl-Y" in text
    assert "Ctrl-B" in text
    assert "Ctrl-T" in text
    assert "Find thread where..." in text
    assert "where i was asking nevena about feedback" in text
    assert "last closeout session for musopia" in text
    assert "Looking for: musopia + closeout" in text
    assert "add one anchor" in text.lower()


def test_readme_no_longer_documents_removed_browser_shortcuts():
    text = _readme_text()
    assert "Ctrl-X" not in text


def test_readme_no_longer_documents_claude_resume():
    text = _readme_text()
    assert "claude-resume" not in text


def test_readme_documents_experimental_external_providers():
    text = _readme_text()
    assert "CLAUDE_BROWSE_PROVIDER_MODULES" in text
    assert "CLAUDE_BROWSE_PROVIDER_DIRS" in text
    assert "--list-providers" in text
    assert "PROVIDER_API_VERSION" in text
    assert "experimental" in text.lower()

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
    assert "Ctrl-O" in text
    assert "Ctrl-T" in text
    assert "Ctrl-H" in text
    assert "Ctrl-U" in text
    assert "Find thread where..." in text
    assert "where i was asking about teammate feedback" in text
    assert "last closeout session for client" in text
    assert "Looking for: client + closeout" in text
    assert "add one anchor" in text.lower()
    assert "local concept cues" in text.lower()
    assert "trust/provenance tags" in text.lower()
    assert "why this surfaced" in text.lower()
    assert "match confidence" in text.lower()
    assert "folder match" in text
    assert "opening match" in text
    assert "mentioned later" in text
    assert "primary subject" in text
    assert "drifted" in text


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


def test_readme_documents_automatic_session_backed_work_board():
    text = " ".join(_readme_text().split())
    assert "Every hook-observed Claude or CodeX terminal session becomes one work row" in text
    assert "Active, Today, By Project, and Done & Archived" in text
    assert "Work status" in text
    assert "Terminal state" in text
    assert "Done returns to Active only when that same session receives a new prompt" in text
    assert "Archived stays archived until you manually restore it" in text
    assert "Full access" in text
    assert "on by default" in text
    assert "same-provider action uses the native guarded resume policy" in text
    assert "cross-provider action starts a new context and therefore a new work row" in text
    assert "CSRF and DNS-rebinding control" in text
    assert "same-user local processes are trusted" in text
    assert "Work metadata, due dates, archive state, and transcripts remain local" in text
    assert "Cross-Mac work metadata and Mission Control rendering are deferred" in text
    assert "Add a standalone task" not in text
    assert "save an existing thread" not in text
    assert "POST /api/tasks` | Add" not in text

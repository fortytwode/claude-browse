"""README regression checks for documented handoff behavior."""

from __future__ import annotations

from pathlib import Path


README = Path(__file__).resolve().parents[1] / "README.md"


def _readme_text() -> str:
    return README.read_text()


def test_readme_documents_other_app_handoff():
    text = _readme_text()
    assert "Continue the selected thread in the other app" in text
    assert "starts a fresh session in the other app" in text


def test_readme_documents_provider_aware_flag_passthrough():
    text = _readme_text()
    assert "example Claude flag when the selected session is a Claude thread" in text
    assert "example CodeX flag when the selected session is a CodeX thread" in text
    assert "extra flags passed through to claude" not in text.lower()

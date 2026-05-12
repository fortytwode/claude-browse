"""Cursor CLI target adapter."""

from __future__ import annotations

from .base import ProviderSpec


def list_index_records() -> list[dict[str, object]]:
    return []


def preview_messages(path: str, session_id: str) -> list[tuple[int, str]]:
    del path, session_id
    return []


def transcript_turns(path: str, session_id: str) -> list[tuple[str, str]]:
    del path, session_id
    return []


PROVIDER = ProviderSpec(
    provider_id="cursor",
    display_name="Cursor",
    binary="cursor-agent",
    native_resume_prefix=("cursor-agent", "--resume"),
    list_index_records_reader=list_index_records,
    preview_messages_reader=preview_messages,
    transcript_turns_reader=transcript_turns,
    native_yolo_flag="--force",
    handoff_yolo_flag="--force",
    add_dir_flag=None,
    handoff_via_file=False,
    can_native_resume=True,
    assistant_turns_available=False,
    source_capable=False,
    target_capable=True,
    install_hint="Install Cursor Agent CLI and ensure `cursor-agent` is on PATH.",
    login_hint="Run `cursor-agent login` or set `CURSOR_API_KEY` before launching Cursor.",
)

"""Provider registry primitives."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    IndexRecord = dict[str, object]
    PreviewMessage = tuple[int, str]
    TranscriptTurn = tuple[str, str]
else:
    IndexRecord = dict
    PreviewMessage = tuple
    TranscriptTurn = tuple

PROVIDER_API_VERSION = 1


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    display_name: str
    binary: str
    native_resume_prefix: tuple[str, ...]
    list_index_records_reader: Callable[[], list[IndexRecord]]
    preview_messages_reader: Callable[[str, str], list[PreviewMessage]]
    transcript_turns_reader: Callable[[str, str], list[TranscriptTurn]]
    has_local_state_reader: Callable[[], bool] | None = None
    session_info_reader: Callable[[str], dict | None] | None = None
    fielded_corpus_reader: Callable[[str], dict[str, str]] | None = None
    session_files_reader: Callable[[], list[str]] | None = None
    availability_reader: Callable[[], bool] | None = None
    auth_status_reader: Callable[[], str | None] | None = None
    native_yolo_flag: str | None = None
    handoff_yolo_flag: str | None = None
    add_dir_flag: str | None = "--add-dir"
    handoff_prompt_flag: str | None = None
    handoff_via_file: bool = True
    can_native_resume: bool = True
    assistant_turns_available: bool = True
    source_capable: bool = True
    target_capable: bool = True
    experimental: bool = False
    install_hint: str | None = None
    login_hint: str | None = None

    def native_resume_cmd(self, session_id: str, yolo: bool) -> list[str]:
        cmd = list(self.native_resume_prefix) + [session_id]
        if yolo and self.native_yolo_flag:
            cmd.append(self.native_yolo_flag)
        return cmd

    def handoff_cmd(
        self,
        import_dir: str | None,
        prompt: str,
        yolo: bool,
    ) -> list[str]:
        cmd = [self.binary]
        if yolo and self.handoff_yolo_flag:
            cmd.append(self.handoff_yolo_flag)
        if self.add_dir_flag and import_dir:
            cmd.extend([self.add_dir_flag, import_dir])
        if self.handoff_prompt_flag:
            cmd.extend([self.handoff_prompt_flag, prompt])
        else:
            cmd.append(prompt)
        return cmd

    def list_index_records(self) -> list[IndexRecord]:
        return self.list_index_records_reader()

    def has_local_state(self) -> bool:
        if self.has_local_state_reader is None:
            return False
        return self.has_local_state_reader()

    def is_available(self) -> bool:
        if self.availability_reader is not None:
            return self.availability_reader()
        return shutil.which(self.binary) is not None

    def auth_status(self) -> str | None:
        if self.auth_status_reader is None:
            return None
        return self.auth_status_reader()

    def preview_messages(
        self, path: str, session_id: str
    ) -> list[PreviewMessage]:
        return self.preview_messages_reader(path, session_id)

    def transcript_turns(
        self, path: str, session_id: str
    ) -> list[TranscriptTurn]:
        return self.transcript_turns_reader(path, session_id)

    def transcript_excerpt(
        self, path: str, session_id: str, limit: int = 24
    ) -> list[TranscriptTurn]:
        return self.transcript_turns(path, session_id)[-limit:]

    def session_info(self, path: str) -> dict | None:
        if self.session_info_reader is None:
            return None
        return self.session_info_reader(path)

    def fielded_corpus(self, path: str) -> dict[str, str]:
        if self.fielded_corpus_reader is None:
            return {
                "cwd": "",
                "title": "",
                "first_msg": "",
                "user_text": "",
                "asst_text": "",
                "boilerplate": "",
            }
        return self.fielded_corpus_reader(path)

    def session_files(self) -> list[str]:
        if self.session_files_reader is None:
            return []
        return self.session_files_reader()

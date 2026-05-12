"""Built-in provider registry for browser launch behavior.

This is intentionally internal-only. It centralizes provider metadata and
command construction so future adapters do not have to accrete inside
`browse.py`, but it is not a public plugin API yet.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    display_name: str
    binary: str
    native_resume_prefix: tuple[str, ...]
    native_yolo_flag: str | None = None
    handoff_yolo_flag: str | None = None
    add_dir_flag: str = "--add-dir"

    def native_resume_cmd(self, session_id: str, yolo: bool) -> list[str]:
        cmd = list(self.native_resume_prefix) + [session_id]
        if yolo and self.native_yolo_flag:
            cmd.append(self.native_yolo_flag)
        return cmd

    def handoff_cmd(self, import_dir: str, prompt: str, yolo: bool) -> list[str]:
        cmd = [self.binary]
        if yolo and self.handoff_yolo_flag:
            cmd.append(self.handoff_yolo_flag)
        cmd.extend([self.add_dir_flag, import_dir, prompt])
        return cmd


_PROVIDERS: dict[str, ProviderSpec] = {
    "claude": ProviderSpec(
        provider_id="claude",
        display_name="Claude",
        binary="claude",
        native_resume_prefix=("claude", "--resume"),
        native_yolo_flag="--dangerously-skip-permissions",
        handoff_yolo_flag="--dangerously-skip-permissions",
    ),
    "codex": ProviderSpec(
        provider_id="codex",
        display_name="CodeX",
        binary="codex",
        native_resume_prefix=("codex", "resume"),
        native_yolo_flag="--dangerously-bypass-approvals-and-sandbox",
        handoff_yolo_flag="--dangerously-bypass-approvals-and-sandbox",
    ),
}


def get_provider(provider: str | None) -> ProviderSpec:
    key = (provider or "claude").lower()
    return _PROVIDERS.get(key, _PROVIDERS["claude"])


def provider_ids() -> tuple[str, ...]:
    return tuple(_PROVIDERS)


def alternate_provider(provider: str | None) -> str:
    current = get_provider(provider).provider_id
    if current == "codex":
        return "claude"
    return "codex"

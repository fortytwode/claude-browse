"""Shared parsing helpers for built-in session providers."""

from __future__ import annotations

import getpass
import os
import re
from datetime import datetime, timezone

_BOILERPLATE_RE = re.compile(
    r"^\s*[-*]\s*[\w][\w\s.&'-]*?:\s*\d+(?:\.\d+)?\s*h\s*$"
)

_NOISE_PREFIXES = (
    "<local-command",
    "<command",
    "<task-notification",
    "<system-reminder",
    "Caveat:",
    "[Request interrupted",
    "This session is being continued from a previous conversation",
)

_CONFIRMATION_WORDS = frozenset({
    "yes", "yep", "yeah", "ok", "okay", "k", "sure",
    "go ahead", "go ahead please", "yes please",
    "yes go ahead", "yes go ahead please",
    "please go ahead", "please do", "please",
    "sounds good", "looks good", "great", "perfect",
    "nice", "thanks", "thank you", "done", "cool",
    "yes thanks", "ok thanks", "yep thanks",
    "all good", "got it", "noted",
})


def extract_text(content) -> str:
    """Pull plain text from a message content field, regardless of shape."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("text"):
                if part.get("type") in (None, "text"):
                    parts.append(part["text"])
        return " ".join(parts)
    return ""


def flatten_text(text: str) -> str:
    return text.replace("\n", " ").strip()


def is_noise_text(cleaned: str) -> bool:
    return any(cleaned.startswith(prefix) for prefix in _NOISE_PREFIXES)


def is_substantive_text(cleaned: str) -> bool:
    stripped = cleaned.lower().strip(".,!?;: ")
    return (
        stripped not in _CONFIRMATION_WORDS
        and len(cleaned) >= 25
        and not is_noise_text(cleaned)
    )


def split_boilerplate(text: str) -> tuple[str, list[str]]:
    keep_lines: list[str] = []
    boilerplate: list[str] = []
    for line in text.split("\n"):
        if _BOILERPLATE_RE.match(line):
            boilerplate.append(line.strip())
        else:
            keep_lines.append(line)
    return ("\n".join(keep_lines).strip(), boilerplate)


def epoch_ms_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(
            value / 1000.0, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def epoch_s_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(
            float(value), tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def canonicalize_path(path: str | None) -> str | None:
    """Normalize a cwd across machines so one project has one display path."""
    if not path:
        return path

    home = os.path.expanduser("~")
    user = os.environ.get("USER") or getpass.getuser()

    for prefix in (f"/Users/{user}", f"/home/{user}"):
        if path == prefix:
            return home
        if path.startswith(prefix + "/"):
            return home + path[len(prefix):]

    lower = path.lower()
    for prefix in (f"/users/{user.lower()}", f"/home/{user.lower()}"):
        if lower == prefix:
            return home
        if lower.startswith(prefix + "/"):
            return home + path[len(prefix):]

    aliases = os.environ.get("CLAUDE_BROWSE_PATH_ALIASES", "")
    if aliases:
        for pair in aliases.split(":"):
            if "=" not in pair:
                continue
            src, dst = pair.split("=", 1)
            src, dst = src.strip(), dst.strip()
            if path == src:
                return dst
            if path.startswith(src + "/"):
                return dst + path[len(src):]

    return path

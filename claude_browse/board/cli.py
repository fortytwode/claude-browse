"""`agent-board board` / `jobs` -- one terminal glance at every active session
with a copy-paste resume command for each."""

from __future__ import annotations

import os
import time

from claude_browse.board import store
from claude_browse.providers import get_provider

_UNATTENDED_ICON = "⏳"


def _resume_cmd(provider, session_id: str) -> str:
    # yolo=True: the board's resume commands are for quickly re-entering your
    # own sessions, so include each provider's skip-permissions flag
    # (--dangerously-skip-permissions for claude, the equivalent for codex)
    # for one-paste resumption. User-requested.
    return " ".join(provider.native_resume_cmd(session_id, yolo=True))


def _provider_for(row: dict, cache: dict):
    """Resolve the row's provider spec once per provider id, never per row."""
    provider_id = store.provider_of(row)
    if provider_id not in cache:
        try:
            cache[provider_id] = get_provider(provider_id)
        except Exception:
            cache[provider_id] = get_provider(store.DEFAULT_PROVIDER)
    return cache[provider_id]


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h".replace(".0h", "h")
    return f"{seconds / 86400:.1f}d".replace(".0d", "d")


def render_board(max_age_hours: float = 24) -> str:
    rows = store.active(max_age_hours=max_age_hours)
    if not rows:
        return "no active sessions"

    providers: dict = {}
    now = time.time()

    enriched = []
    for row in rows:
        state = store.display_state(row)
        enriched.append((state, row))
    enriched.sort(key=lambda pair: store.STATE_ORDER.get(pair[0], 5))

    lines = []

    # "Finished, not picked up" leads the board: it is the one list the user
    # actually needs from a glance, everything below is context.
    waiting = [row for state, row in enriched if state != "gone" and store.is_unattended(row)]
    waiting.sort(key=lambda r: r.get("done_at") or 0)
    if waiting:
        lines.append(f"{_UNATTENDED_ICON} finished, not picked up ({len(waiting)}):")
        for row in waiting:
            name = row.get("name") or os.path.basename(row.get("cwd") or "") or row["session_id"]
            age = _age(now - float(row.get("done_at") or now))
            resume = _resume_cmd(_provider_for(row, providers), row["session_id"])
            lines.append(f"  {_UNATTENDED_ICON} {name:<32} {age + ' ago':<12} {resume}")
        lines.append("")

    for state, row in enriched:
        name = row.get("name") or os.path.basename(row.get("cwd") or "") or row["session_id"]
        host = row.get("host") or "?"
        cwd_base = os.path.basename(row.get("cwd") or "") or (row.get("cwd") or "")
        icon = store.STATE_ICON.get(state, "?")
        marker = _UNATTENDED_ICON if store.is_unattended(row) and state != "gone" else " "
        resume = _resume_cmd(_provider_for(row, providers), row["session_id"])
        lines.append(f"{icon}{marker}{name:<32} {state:<12} {host:<10} {cwd_base:<20} {resume}")

    return "\n".join(lines)


def main() -> None:
    print(render_board())


if __name__ == "__main__":
    main()

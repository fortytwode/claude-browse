"""Explicit persistence boundary for already-open terminal conversations."""

from __future__ import annotations

from claude_browse.board import presence, store, work_items


def _insert_missing_runtime(session: dict[str, str]) -> bool:
    """Record observed facts once without competing with hook-owned state."""
    with store.get_conn() as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO sessions
               (session_id, host, cwd, provider, transcript_path)
               VALUES (?, ?, ?, ?, ?)""",
            (
                session["session_id"],
                presence._hostname(),
                session.get("cwd", ""),
                session["provider"],
                session.get("path"),
            ),
        )
    return cursor.rowcount == 1


def capture_live_sessions() -> int:
    """Enroll verified local roots missed by older hooks.

    ``INSERT OR IGNORE`` preserves a concurrent hook's richer runtime values.
    Work items are only initialized where neither the current canonical task
    nor a prior task-session link already owns the observed session.
    """
    captured = 0
    for session in presence.live_sessions():
        session_id = session["session_id"]
        inserted = _insert_missing_runtime(session)
        # Ownership and pending-launch checks share the enrollment writer
        # transaction; separate reads cannot exclude a racing SessionStart.
        enrolled = work_items.capture_discovered_session(session_id)
        if inserted or enrolled:
            captured += 1
    return captured

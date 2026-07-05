"""Real multi-process tests for the reindex writer election.

Unlike the monkeypatched lock tests in test_browse.py, these spawn actual
subprocesses against one real database file: the flock election, the
blocking wait, crash-released locks, and WAL crash-safety are exercised
for real, not simulated.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from claude_browse import fts

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def _make_records(sessions_dir: Path, count: int) -> list[dict]:
    """Synthetic claude-style session files + matching index records."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for i in range(count):
        sid = f"aaaaaaaa-bbbb-cccc-dddd-{i:012d}"
        path = sessions_dir / f"{sid}.jsonl"
        turns = [
            {
                "sessionId": sid,
                "cwd": "/Users/alice/code/webapp",
                "timestamp": "2026-04-01T10:00:00Z",
                "message": {
                    "role": "user",
                    "content": f"debug the login flow number {i} crashing on continue",
                },
            },
            {
                "sessionId": sid,
                "timestamp": "2026-04-01T10:05:00Z",
                "message": {
                    "role": "assistant",
                    "content": f"the login handler {i} short-circuits on missing email validation",
                },
            },
        ]
        path.write_text("\n".join(json.dumps(t) for t in turns) + "\n")
        records.append({
            "path": str(path),
            "provider": "claude",
            "session_id": sid,
            "first_msg": f"debug the login flow number {i} crashing on continue",
            "last_msg": "email validation should happen before the redirect",
            "timestamp": "2026-04-01T10:00:00Z",
            "last_timestamp": "2026-04-01T10:10:00Z",
            "cwd": "/Users/alice/code/webapp",
            "name": f"Debug login flow {i}",
            "msg_count": 2,
            "mtime": os.path.getmtime(path),
            "fields": {
                "cwd": "/users/alice/code/webapp",
                "title": f"debug login flow {i}",
                "first_msg": f"debug the login flow number {i} crashing on continue",
                "user_text": "email validation should happen before the redirect",
                "asst_text": f"the login handler {i} short-circuits on missing email validation",
                "boilerplate": "",
            },
        })
    return records


_WORKER = """
import json, os, sys, time
sys.path.insert(0, {repo!r})
from claude_browse import fts

records = json.load(open({records_json!r}))
fts.list_index_records = lambda known_sessions=None: records

if os.environ.get("SLOW_REINDEX") == "1":
    _real = fts._reindex_locked
    def _slow(conn):
        time.sleep(float(os.environ.get("REINDEX_SLEEP", "3")))
        return _real(conn)
    fts._reindex_locked = _slow

conn = fts.open_db({db!r})
result = fts.reindex(conn, block=os.environ.get("BLOCK") == "1")
conn.close()
print("WON" if result is not None else "LOST", flush=True)
"""


def _spawn_worker(db: str, records_json: str, env_extra: dict[str, str]):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.Popen(
        [sys.executable, "-c", _WORKER.format(
            repo=REPO_ROOT, records_json=records_json, db=db
        )],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_two_processes_elect_exactly_one_reindexer(tmp_path):
    db = str(tmp_path / "index.db")
    records = _make_records(tmp_path / "sessions", 5)
    records_json = str(tmp_path / "records.json")
    Path(records_json).write_text(json.dumps(records))

    # p1 holds the lock for ~3s inside reindex; p2 starts inside that
    # window and must lose the election immediately (block defaults off).
    p1 = _spawn_worker(db, records_json, {"SLOW_REINDEX": "1"})
    time.sleep(1.0)
    p2 = _spawn_worker(db, records_json, {})

    out2, err2 = p2.communicate(timeout=30)
    out1, err1 = p1.communicate(timeout=30)

    assert p1.returncode == 0, err1
    assert p2.returncode == 0, err2
    assert out1.strip() == "WON"
    assert out2.strip() == "LOST"

    conn = fts.open_db(db)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 5
    conn.close()


def test_blocking_loser_waits_for_winner_then_wins(tmp_path):
    db = str(tmp_path / "index.db")
    records = _make_records(tmp_path / "sessions", 5)
    records_json = str(tmp_path / "records.json")
    Path(records_json).write_text(json.dumps(records))

    p1 = _spawn_worker(db, records_json, {"SLOW_REINDEX": "1"})
    time.sleep(1.0)
    p2 = _spawn_worker(db, records_json, {"BLOCK": "1"})

    out2, err2 = p2.communicate(timeout=30)
    out1, err1 = p1.communicate(timeout=30)

    assert p1.returncode == 0, err1
    assert p2.returncode == 0, err2
    assert out1.strip() == "WON"
    # The blocking loser waits out the winner, then acquires and runs its
    # own (no-op) reindex -- it must report a real result, not None.
    assert out2.strip() == "WON"


def test_sigkill_mid_reindex_releases_lock_and_leaves_recoverable_db(tmp_path):
    db = str(tmp_path / "index.db")
    records = _make_records(tmp_path / "sessions", 300)
    records_json = str(tmp_path / "records.json")
    Path(records_json).write_text(json.dumps(records))

    # Real reindex over 300 sessions: the kill lands between the
    # per-10-session commits, exactly the production failure mode.
    p1 = _spawn_worker(db, records_json, {})
    time.sleep(1.5)
    os.kill(p1.pid, signal.SIGKILL)
    p1.wait(timeout=10)

    # flock died with the process: this acquire must succeed instantly.
    fd = fts.acquire_reindex_lock(db)
    assert fd is not None and fd >= 0
    fts.release_reindex_lock(fd)

    # WAL + synchronous=NORMAL: the file must be structurally intact and a
    # fresh reindex must complete the interrupted build.
    conn = fts.open_db(db)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    import claude_browse.fts as fts_mod

    original = fts_mod.list_index_records
    fts_mod.list_index_records = lambda known_sessions=None: records
    try:
        result = fts.reindex(conn)
    finally:
        fts_mod.list_index_records = original
    assert result is not None
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 300
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()

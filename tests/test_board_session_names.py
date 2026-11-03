"""Provider naming uses fixture metadata only, never a user's real sessions."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys

import pytest

from claude_browse.board import session_names
from claude_browse.providers import claude

SID = "99999999-aaaa-bbbb-cccc-dddddddddddd"
OTHER = "11111111-aaaa-bbbb-cccc-dddddddddddd"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "claude" / "projects"
    root.mkdir(parents=True)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(claude, "SESSIONS_DIR", str(root))
    monkeypatch.setattr(session_names.store, "get", lambda _sid: None)
    # Each Codex test must explicitly opt in to a stub or fixture subprocess.
    monkeypatch.setattr(session_names, "_CodexRPC", lambda *_args: pytest.fail("Unexpected native RPC"))
    return root


def transcript(root, sid=SID, project="project", identity=SID):
    path = root / project / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "user", "sessionId": identity,
        "message": {"role": "user", "content": "Original conversation"},
    }) + "\n", encoding="utf-8")
    return path


def test_claude_appends_native_title_and_preserves_existing_bytes(isolated):
    path = transcript(isolated)
    before = path.read_bytes()
    title = 'Release "mañana" 🚀'
    result = session_names.rename_session(SID, "claude", title)
    assert result["updated"] is True
    assert "in-memory" in result["reason"]
    assert path.read_bytes().startswith(before)
    assert json.loads(path.read_bytes()[len(before):]) == {
        "type": "custom-title", "customTitle": title, "sessionId": SID,
    }
    assert claude.get_session_info(str(path))["name"] == title
    session_names.rename_session(SID, "claude", "Newer")
    assert claude.get_session_info(str(path))["name"] == "Newer"


@pytest.mark.parametrize("sid,provider,title", [
    ("../escape", "claude", "Title"), (None, "codex", "Title"),
    (SID, "unknown", "Title"), (SID, "claude", ""),
    (SID, "claude", " \t"), (SID, "claude", "line\nbreak"),
    (SID, "claude", "escape\x1b]0;title"), (SID, "claude", "x" * 501),
    (SID, "codex", None),
])
def test_invalid_input_never_writes(isolated, monkeypatch, sid, provider, title):
    monkeypatch.setattr(session_names.os, "open", lambda *_: pytest.fail("Must not open"))
    result = session_names.rename_session(sid, provider, title)
    assert result["updated"] is False
    assert result["reason"]
    assert not list(isolated.rglob("*.jsonl"))


def test_claude_missing_session_does_not_create_history(isolated):
    assert session_names.rename_session(SID, "claude", "Missing")["updated"] is False
    assert list(isolated.iterdir()) == []


@pytest.mark.parametrize("problem", ["incomplete", "mismatch", "duplicate", "symlink", "locked"])
def test_claude_unsafe_transcript_is_unchanged(isolated, problem):
    import fcntl

    path = transcript(isolated, identity=OTHER if problem == "mismatch" else SID)
    locked = None
    if problem == "incomplete":
        with path.open("ab") as handle:
            handle.write(b'{"type":"assistant"')
    if problem == "duplicate":
        transcript(isolated, project="second")
    if problem == "symlink":
        original = path.with_suffix(".original")
        path.rename(original)
        path.symlink_to(original)
    if problem == "locked":
        locked = path.open("rb")
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = path.read_bytes()
    try:
        result = session_names.rename_session(SID, "claude", "Unsafe")
    finally:
        if locked:
            locked.close()
    assert result["updated"] is False
    assert result["reason"]
    assert path.read_bytes() == before


def test_claude_respects_config_root(isolated, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "alternate"))
    original = transcript(isolated)
    before = original.read_bytes()
    alternate = transcript(tmp_path / "alternate" / "projects")
    assert session_names.rename_session(SID, "claude", "Alternate")["updated"] is True
    assert claude.get_session_info(str(alternate))["name"] == "Alternate"
    assert original.read_bytes() == before


def test_claude_oversized_encoded_record_is_rejected_before_write(isolated):
    path = transcript(isolated)
    before = path.read_bytes()
    assert session_names.rename_session(SID, "claude", "🚀" * 500)["updated"] is False
    assert path.read_bytes() == before


def test_claude_short_write_is_unconfirmed_and_never_truncates_history(isolated, monkeypatch):
    path = transcript(isolated)
    before = path.read_bytes()
    original_write = session_names.os.write
    writes = []

    def short_write(fd, record):
        writes.append(record)
        return original_write(fd, record[:12])

    monkeypatch.setattr(session_names.os, "write", short_write)
    monkeypatch.setattr(
        session_names.os, "ftruncate",
        lambda *_args: pytest.fail("Must not truncate a concurrent transcript"),
    )
    result = session_names.rename_session(SID, "claude", "Title")
    assert result["updated"] is False
    assert "Unconfirmed metadata write" in result["reason"]
    assert "Original transcript bytes were preserved" in result["reason"]
    assert len(writes) == 1
    assert path.read_bytes() == before + writes[0][:12]


@pytest.mark.parametrize("error", [PermissionError("Denied"), OSError("Disk full"), RuntimeError("SDK failed")])
def test_provider_failures_are_structured(monkeypatch, error):
    def fail(*_args):
        raise error
    monkeypatch.setattr(session_names, "_rename_codex", fail)
    result = session_names.rename_session(SID, "codex", "Title")
    assert result["updated"] is False
    assert str(error) in result["reason"]


def test_stale_request_persists_latest_board_title(monkeypatch):
    calls = []
    monkeypatch.setattr(session_names.store, "get", lambda _: {"name": "Newer"})
    monkeypatch.setattr(session_names, "_rename_codex", lambda sid, title: calls.append((sid, title)))
    result = session_names.rename_session(SID, "codex", "Older")
    assert calls == [(SID, "Newer")]
    assert result == {"updated": False, "reason": "A newer session title replaced this rename."}


def test_mid_write_rename_converges_to_latest_board_title(monkeypatch):
    calls = []
    names = iter(["First", "Latest", "Latest", "Latest"])
    monkeypatch.setattr(session_names.store, "get", lambda _: {"name": next(names)})
    monkeypatch.setattr(session_names, "_rename_codex", lambda _, title: calls.append(title))
    result = session_names.rename_session(SID, "codex", "First")
    assert calls == ["First", "Latest"]
    assert result["updated"] is False
    assert "newer" in result["reason"]


def test_continually_changing_title_has_bounded_retries(monkeypatch):
    names = iter([f"Name {i}" for i in range(6)])
    calls = []
    monkeypatch.setattr(session_names.store, "get", lambda _: {"name": next(names)})
    monkeypatch.setattr(session_names, "_rename_codex", lambda _, title: calls.append(title))
    result = session_names.rename_session(SID, "codex", "Original")
    assert len(calls) == 3
    assert result["updated"] is False
    assert "repeatedly" in result["reason"]


class FakeRPC:
    def __init__(self, command, deadline):
        self.command = command
        self.calls = []
        self.closed = False
        self.name = "Original"

    def send(self, message):
        self.calls.append((message["method"], {}))

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/name/set":
            self.name = params["name"]
        if method == "thread/read":
            return {"thread": {"id": SID, "name": self.name}}
        return {}

    def close(self):
        self.closed = True


def fake_rpc(monkeypatch, cls=FakeRPC):
    instances = []
    def create(command, deadline):
        rpc = cls(command, deadline)
        instances.append(rpc)
        return rpc
    monkeypatch.setattr(session_names, "_CodexRPC", create)
    return instances


def test_codex_uses_native_metadata_api_without_resuming_or_turns(monkeypatch):
    instances = fake_rpc(monkeypatch)
    title = "literal '$(touch /tmp/should-not-run)'"
    assert session_names.rename_session(SID, "codex", title) == {"updated": True, "reason": ""}
    rpc, = instances
    assert rpc.closed
    assert rpc.command == ["codex", "app-server", "proxy"]
    assert [method for method, _ in rpc.calls] == [
        "initialize", "initialized", "thread/read", "thread/name/set", "thread/read",
    ]
    assert rpc.calls[3][1] == {"threadId": SID, "name": title}


@pytest.mark.parametrize("failure", [TimeoutError("Timeout"), TypeError("Invalid config"), RuntimeError("No proxy")])
def test_codex_initialization_failure_always_closes_candidate(monkeypatch, failure):
    class Failing(FakeRPC):
        def call(self, method, params):
            raise failure
    instances = fake_rpc(monkeypatch, Failing)
    result = session_names.rename_session(SID, "codex", "Title")
    assert result["updated"] is False
    assert all(rpc.closed for rpc in instances)
    assert len(instances) == (1 if isinstance(failure, TimeoutError) else 2)


def test_codex_no_fallback_after_unconfirmed_mutation(monkeypatch):
    class Failing(FakeRPC):
        def call(self, method, params):
            if method == "thread/name/set":
                raise TimeoutError("Name may already be persisted")
            return super().call(method, params)
    instances = fake_rpc(monkeypatch, Failing)
    assert session_names.rename_session(SID, "codex", "Title")["updated"] is False
    assert len(instances) == 1
    assert instances[0].closed


def test_codex_falls_back_only_when_proxy_initialization_fails(monkeypatch):
    class NoDaemon(FakeRPC):
        def call(self, method, params):
            if self.command[-1] == "proxy":
                raise RuntimeError("No running daemon")
            return super().call(method, params)
    instances = fake_rpc(monkeypatch, NoDaemon)
    assert session_names.rename_session(SID, "codex", "Title")["updated"] is True
    assert len(instances) == 2
    assert instances[1].command == ["codex", "app-server", "--listen", "stdio://"]
    assert all(rpc.closed for rpc in instances)


@pytest.mark.parametrize("problem", ["wrong-id", "wrong-readback", "missing"])
def test_codex_requires_identity_and_name_readback(monkeypatch, problem):
    class Invalid(FakeRPC):
        def call(self, method, params):
            result = super().call(method, params)
            if method == "thread/read":
                if problem == "wrong-id":
                    result["thread"]["id"] = OTHER
                if problem == "wrong-readback":
                    result["thread"]["name"] = "Old"
                if problem == "missing":
                    return {}
            return result
    instances = fake_rpc(monkeypatch, Invalid)
    assert session_names.rename_session(SID, "codex", "Title")["updated"] is False
    assert instances[0].closed
    if problem != "wrong-readback":
        assert not any(method == "thread/name/set" for method, _ in instances[0].calls)


def test_real_stdio_transport_handles_notifications_and_cleans_up(monkeypatch):
    # Exercise actual pipe framing without launching a provider or model.
    script = """
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({'method': 'notification'}), flush=True)
    print(json.dumps({'id': request['id'], 'result': {'ok': True}}), flush=True)
"""
    rpc = _REAL_RPC([sys.executable, "-u", "-c", script], session_names.time.monotonic() + 2)
    try:
        assert rpc.call("initialize", {}) == {"ok": True}
        assert rpc.call("thread/read", {}) == {"ok": True}
    finally:
        rpc.close()
    assert rpc.process.poll() is not None
    assert rpc.process.stdin.closed and rpc.process.stdout.closed


def test_real_stdio_transport_timeout_kills_its_child():
    rpc = _REAL_RPC(
        [sys.executable, "-u", "-c", "import sys; sys.stdin.read()"],
        session_names.time.monotonic() + 0.1,
    )
    try:
        with pytest.raises(TimeoutError):
            rpc.call("initialize", {})
    finally:
        rpc.close()
    assert rpc.process.poll() is not None


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is not installed")
def test_native_codex_renames_fixture_index_and_title_store_only(tmp_path, monkeypatch):
    """Real provider API; isolated home, synthetic rollout, no agent/model turn."""
    root = tmp_path / "codex"
    rollout = root / "sessions" / "2026" / "11" / "03" / f"rollout-2026-11-03T10-00-00-{SID}.jsonl"
    rollout.parent.mkdir(parents=True)
    records = [
        {"timestamp": "2026-11-03T10:00:00Z", "type": "session_meta", "payload": {
            "id": SID, "timestamp": "2026-11-03T10:00:00Z", "cwd": str(tmp_path),
            "originator": "codex_cli_rs", "cli_version": "0.166.0",
            "source": "cli", "model_provider": "openai",
        }},
        {"timestamp": "2026-11-03T10:00:01Z", "type": "event_msg", "payload": {
            "type": "user_message", "message": "Synthetic session only",
        }},
    ]
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
    before = rollout.read_bytes()
    monkeypatch.setenv("CODEX_HOME", str(root))
    # Force a private app-server: never connect this integration test to an
    # existing daemon, regardless of installed proxy discovery behavior.
    monkeypatch.setattr(session_names, "_CodexRPC", lambda _command, deadline: _REAL_RPC(
        ["codex", "app-server", "--listen", "stdio://"], deadline,
    ))
    result = session_names.rename_session(SID, "codex", "Fixture native rename")
    assert result == {"updated": True, "reason": ""}
    index = [json.loads(line) for line in (root / "session_index.jsonl").read_text().splitlines()]
    assert index[-1]["id"] == SID
    assert index[-1]["thread_name"] == "Fixture native rename"
    assert index[-1]["updated_at"]
    db = sqlite3.connect(f"file:{root / 'state_5.sqlite'}?mode=ro", uri=True)
    try:
        assert db.execute("SELECT title FROM threads WHERE id = ?", (SID,)).fetchone() == (
            "Fixture native rename",
        )
    finally:
        db.close()
    assert rollout.read_bytes() == before


_REAL_RPC = session_names._CodexRPC

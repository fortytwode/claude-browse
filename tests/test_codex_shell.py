import json
import os
import pty
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "shell" / "codex-inline.zsh"

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is required")


@pytest.fixture
def fake_codex(tmp_path):
    output = tmp_path / "argv.json"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CODEX_TEST_ARGV'], 'w') as f:\n"
        "    json.dump(sys.argv[1:], f)\n"
    )
    executable.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["CODEX_TEST_ARGV"] = str(output)
    env.pop("SSH_CONNECTION", None)
    env.pop("CODEX_NO_MOBILE", None)
    return env, output


def run_codex(fake_codex, args, *, tty=True, ssh=False):
    env, output = fake_codex
    if ssh:
        env["SSH_CONNECTION"] = "test-connection"
    command = f"source {shlex.quote(str(LAUNCHER))}; codex {shlex.join(args)}"
    if tty:
        master, slave = pty.openpty()
        try:
            subprocess.run(["zsh", "-f", "-c", command], stdin=slave, stdout=slave,
                           stderr=slave, env=env, check=True, timeout=10)
        finally:
            os.close(slave)
            os.close(master)
    else:
        subprocess.run(["zsh", "-f", "-c", command], input="", text=True, capture_output=True,
                       env=env, check=True, timeout=10)
    return json.loads(output.read_text())


@pytest.mark.parametrize("args", [[], ["resume", "--last"], ["fork", "thread-id"], ["Build", "the", "app"]])
def test_local_interactive_commands_use_private_daemon(fake_codex, args):
    assert run_codex(fake_codex, args) == ["--no-daemon", *args]


@pytest.mark.parametrize("args", [
    ["--remote", "unix:///tmp/codex.sock", "resume", "--last"],
    ["--remote=unix:///tmp/codex.sock", "fork", "thread-id"],
    ["--remote-auth-token-env", "CODEX_TOKEN", "resume", "--last"],
    ["--remote-auth-token-env=CODEX_TOKEN", "fork", "thread-id"],
    ["--no-daemon", "resume", "--last"],
    ["exec", "Build the app"], ["e", "Build the app"], ["app-server"],
    ["exec-server"], ["review", "--uncommitted"], ["mcp", "list"],
    ["--yolo", "exec", "Build the app"],
    ["--help"], ["--version"],
])
def test_explicit_remote_and_noninteractive_commands_are_unchanged(fake_codex, args):
    assert run_codex(fake_codex, args) == args


def test_non_tty_command_is_unchanged(fake_codex):
    assert run_codex(fake_codex, ["resume", "--last"], tty=False) == ["resume", "--last"]


@pytest.mark.parametrize("args", [[], ["resume", "--last"], ["fork", "thread-id"]])
def test_ssh_behavior_remains_unchanged(fake_codex, args):
    assert run_codex(fake_codex, args, ssh=True) == [
        "-c", 'tui.alternate_screen="always"', "-c", "tui.fullscreen_transcript=true",
        "-c", "tui.raw_output_mode=false", *args,
    ]


def test_permission_flags_are_forwarded_unchanged(fake_codex):
    args = ["--dangerously-bypass-approvals-and-sandbox", "resume", "--last"]
    assert run_codex(fake_codex, args) == ["--no-daemon", *args]


@pytest.mark.parametrize("args", [
    ["--approve-for-me", "resume", "--last"],
    ["-s", "workspace-write", "resume", "--last"],
])
def test_current_interactive_flags_are_preserved_while_classifying(fake_codex, args):
    assert run_codex(fake_codex, args) == ["--no-daemon", *args]


def test_launcher_is_sourceable_in_zsh():
    subprocess.run(["zsh", "-n", str(LAUNCHER)], check=True, timeout=10)
    subprocess.run(["zsh", "-f", "-c", f"source {shlex.quote(str(LAUNCHER))}"],
                   check=True, timeout=10)

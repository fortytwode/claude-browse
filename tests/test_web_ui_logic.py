"""Run the dependency-free JavaScript UI logic suite under pytest."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_web_ui_logic_node_suite():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")

    suite = Path(__file__).with_name("web_ui_logic.test.cjs")
    result = subprocess.run(
        [node, "--test", str(suite)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

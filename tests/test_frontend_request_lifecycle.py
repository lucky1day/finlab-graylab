from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_frontend_request_lifecycle() -> None:
    """在 Node 浏览器桩环境中执行前端请求生命周期回归。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for frontend lifecycle tests")
    test_files = [
        Path(__file__).with_name("frontend_factor_lab_http.test.js"),
        Path(__file__).with_name("frontend_aifin_shell.test.js"),
    ]
    result = subprocess.run(
        [node, "--test", *(str(path) for path in test_files)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

"""编译扩展模块必须在启动子进程前通过解释器 ABI 守卫。"""

from __future__ import annotations

import importlib.machinery
import sys
from pathlib import Path

import pytest

from shared.source_runtime_database import (
    assert_source_interpreter_supports_package,
)


def _native_suffix() -> str:
    """当前解释器接受的、带 ABI 标签的扩展后缀。"""
    for suffix in importlib.machinery.EXTENSION_SUFFIXES:
        if suffix.startswith(".cpython-"):
            return suffix
    pytest.skip("当前解释器没有带 ABI 标签的扩展后缀")


@pytest.mark.parametrize("tagged_only", [False, True], ids=["native", "tagged-only"])
def test_matching_interpreter_passes(tmp_path: Path, tagged_only: bool) -> None:
    suffix = _native_suffix()
    (tmp_path / "pkg").mkdir()
    (tmp_path / f"runtime_common{suffix}").touch()
    (tmp_path / "pkg" / f"engine{suffix}").touch()
    (tmp_path / "pkg" / "libthing.so").touch()
    # 限定探测响应只声明 ABI 后缀，普通 .so 不能增加解释器要求。
    command = (
        [sys.executable, "-c", f"print({suffix!r})", "--"]
        if tagged_only else [sys.executable]
    )
    assert_source_interpreter_supports_package(
        command, tmp_path, label="t"
    )


def test_mismatched_abi_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "engine.cpython-299-nonesuch.so").touch()
    (tmp_path / "pkg" / "duplicate.cpython-299-nonesuch.so").touch()
    (tmp_path / "pkg" / "other.cpython-298-other.so").touch()
    with pytest.raises(RuntimeError) as excinfo:
        assert_source_interpreter_supports_package(
            [sys.executable], tmp_path, label="daily 0629"
        )
    message = str(excinfo.value)
    assert "daily 0629" in message
    assert "['.cpython-298-other.so', '.cpython-299-nonesuch.so']" in message
    assert "cannot be rebuilt" in message


def test_interpreter_probe_failures_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "engine.cpython-313-darwin.so").touch()
    for command, message in (
        (["/nonexistent/python"], "could not probe"),
        ([sys.executable, "-c", "raise SystemExit(3)", "--"], "probe failed"),
    ):
        with pytest.raises(RuntimeError, match=message):
            assert_source_interpreter_supports_package(
                command,
                tmp_path,
                label="t",
            )

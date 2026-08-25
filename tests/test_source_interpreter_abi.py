"""编译扩展模块必须在启动子进程前通过解释器 ABI 守卫。"""

from __future__ import annotations

import importlib.machinery
import sys
from pathlib import Path

import pytest

from shared.source_runtime_database import (
    assert_source_interpreter_supports_package,
    compiled_extension_suffixes,
)


def _native_suffix() -> str:
    """当前解释器接受的、带 ABI 标签的扩展后缀。"""
    for suffix in importlib.machinery.EXTENSION_SUFFIXES:
        if suffix.startswith(".cpython-"):
            return suffix
    pytest.skip("当前解释器没有带 ABI 标签的扩展后缀")


def test_suffixes_are_collected_from_the_tree(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "runtime_common.cpython-313-darwin.so").touch()
    (tmp_path / "pkg" / "engine.cpython-313-darwin.so").touch()
    (tmp_path / "pkg" / "other.cpython-311-x86_64-linux-gnu.so").touch()
    (tmp_path / "pkg" / "libthing.so").touch()

    assert compiled_extension_suffixes(tmp_path) == {
        ".cpython-313-darwin.so",
        ".cpython-311-x86_64-linux-gnu.so",
    }


def test_tree_without_compiled_modules_skips_the_probe(tmp_path: Path) -> None:
    (tmp_path / "predict.py").touch()
    # 解释器命令故意不可执行：无编译产物时不应探测解释器
    assert_source_interpreter_supports_package(
        ["/nonexistent/python"], tmp_path, label="t"
    )


def test_matching_interpreter_passes(tmp_path: Path) -> None:
    (tmp_path / f"engine{_native_suffix()}").touch()
    assert_source_interpreter_supports_package(
        [sys.executable], tmp_path, label="t"
    )


def test_mismatched_abi_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "engine.cpython-299-nonesuch.so").touch()
    with pytest.raises(RuntimeError) as excinfo:
        assert_source_interpreter_supports_package(
            [sys.executable], tmp_path, label="daily 0629"
        )
    message = str(excinfo.value)
    assert "daily 0629" in message
    assert ".cpython-299-nonesuch.so" in message
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


_BATCHES = ("daily_0629", "monthly_0629", "model_muti_0529")


@pytest.mark.parametrize("batch", _BATCHES)
def test_archived_packages_pin_a_single_abi(batch: str) -> None:
    root = Path(__file__).resolve().parents[1]
    package = root / "source_evidence" / "benchmark_batches" / batch
    if not package.is_dir():
        pytest.skip(f"{batch} 归档不在本工作区")

    suffixes = compiled_extension_suffixes(package)
    assert suffixes == {".cpython-313-darwin.so"}, (
        f"{batch} 的编译 ABI 发生变化：{sorted(suffixes)}"
    )

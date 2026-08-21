"""不可变 release 上 harness 不得向 project_root 读写运行期状态。

生产 release 目录只读，且所有服务以 root 运行（root 绕过 mode bits），因此写入不会失败而会
静默污染 release 树、破坏 source_tree_sha256。本模块用「生产形状」断言把这类问题在开发工作树
里暴露出来：设定生产部署目标与外置 runtime root 后执行真实 Gate，比对 project_root 整树摘要。

字节码写入由运维合同的 `python -B` / `PYTHONDONTWRITEBYTECODE` 负责，不属于本模块范围；
本模块只覆盖 harness 自身的运行期状态读写。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_GATE_SCHEME_ID = "liwei_0616_cons_sda_k3_div_k10"


def _tree_digest(root: Path) -> str:
    """整树内容摘要，语义与 release 安装器的 source tree digest 一致。

    不 import 安装器，避免测试与发布工具耦合。
    """
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink() or path.is_dir():
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "executable": bool(stat.S_IMODE(path.stat().st_mode) & 0o111),
                "sha256": digest.hexdigest(),
            }
        )
    canonical = json.dumps(
        entries,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _release_tree(tmp_path: Path) -> Path:
    """用 git archive 生成与真实 release 同构的只读源码树。"""
    release_root = tmp_path / "release"
    release_root.mkdir()
    archive = subprocess.run(
        ["git", "archive", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    subprocess.run(
        ["tar", "-x", "-C", str(release_root)],
        input=archive,
        check=True,
        capture_output=True,
    )
    return release_root


def _production_environ(runtime_root: Path) -> dict[str, str]:
    return {
        "BFL_DEPLOYMENT_TARGET": "aliyun-gray",
        "BFL_RUNTIME_ROOT": str(runtime_root),
    }


# --------------------------------------------------------------------------
# 集成：真实 Gate 执行后 release 树必须逐字节不变
# --------------------------------------------------------------------------


def test_static_gate_run_leaves_release_tree_unchanged(tmp_path: Path) -> None:
    """在真实 release 树上执行 StaticGate 并写报告，release 树必须逐字节不变。

    覆盖范围：默认 report_dir 解析 + Gate 执行 + 报告落盘。不覆盖 dry-run/backtest 这类
    在子进程中执行算法的 Gate（它们需要数据库与算法环境），那部分由运维的 `-B` 合同和
    ECS 上「受控 harness 运行前后重算 digest」的现场验收兜底。
    """
    from harness.cli import default_report_dir
    from harness.context import GateContext
    from harness.gates.static_gate import StaticGate
    from harness.report.writer import write_gate_result

    release_root = _release_tree(tmp_path)
    runtime_root = tmp_path / "state"
    before = _tree_digest(release_root)

    with patch.dict(os.environ, _production_environ(runtime_root), clear=False):
        report_dir = default_report_dir(release_root, STATIC_GATE_SCHEME_ID)
        ctx = GateContext(
            scheme_id=STATIC_GATE_SCHEME_ID,
            predict_date="static",
            project_root=release_root,
            report_dir=report_dir,
            config=None,
        )
        result = StaticGate().run(ctx)
        report_path = write_gate_result(ctx, result)

    assert _tree_digest(release_root) == before, (
        "StaticGate 在生产形状下改变了 release 树；运行期状态必须落在 BFL_RUNTIME_ROOT 下"
    )
    assert report_path.is_relative_to(runtime_root.resolve()), (
        "Gate 报告未写入外置 runtime root"
    )


# --------------------------------------------------------------------------
# 单元：四个路径点在生产形状下必须落在 runtime root
# --------------------------------------------------------------------------


def test_used_tokens_path_uses_runtime_root(tmp_path: Path) -> None:
    from harness.authorization import used_tokens_path

    runtime_root = tmp_path / "state"
    project_root = tmp_path / "release"

    with patch.dict(os.environ, _production_environ(runtime_root), clear=False):
        resolved = used_tokens_path(project_root)

    assert resolved.is_relative_to(runtime_root.resolve())
    assert not resolved.is_relative_to(project_root)


def test_used_tokens_path_fails_closed_without_runtime_root(
    tmp_path: Path,
) -> None:
    from harness.authorization import used_tokens_path

    project_root = tmp_path / "release"
    project_root.mkdir()
    before = _tree_digest(project_root)

    environ = {"BFL_DEPLOYMENT_TARGET": "aliyun-gray"}
    with patch.dict(os.environ, environ, clear=False):
        os.environ.pop("BFL_RUNTIME_ROOT", None)
        with pytest.raises(RuntimeError):
            used_tokens_path(project_root)

    assert _tree_digest(project_root) == before, "fail-closed 不得留下文件系统副作用"


def test_used_tokens_path_preserves_development_default(tmp_path: Path) -> None:
    from harness.authorization import used_tokens_path

    project_root = tmp_path / "worktree"

    environ = dict(os.environ)
    environ.pop("BFL_DEPLOYMENT_TARGET", None)
    environ.pop("BFL_RUNTIME_ROOT", None)
    with patch.dict(os.environ, environ, clear=True):
        resolved = used_tokens_path(project_root)

    assert resolved == (
        project_root / "reports" / "harness" / ".used_authorization_tokens.json"
    )


def test_backtest_baseline_uses_runtime_root(tmp_path: Path) -> None:
    from harness.gates.backtest_gate import backtest_baseline_path

    runtime_root = tmp_path / "state"
    project_root = tmp_path / "release"

    with patch.dict(os.environ, _production_environ(runtime_root), clear=False):
        resolved = backtest_baseline_path(project_root, STATIC_GATE_SCHEME_ID)

    assert resolved.is_relative_to(runtime_root.resolve())
    assert resolved.name == "backtest_no_persist.json"


def test_backtest_baseline_preserves_development_default(tmp_path: Path) -> None:
    from harness.gates.backtest_gate import backtest_baseline_path

    project_root = tmp_path / "worktree"

    environ = dict(os.environ)
    environ.pop("BFL_DEPLOYMENT_TARGET", None)
    environ.pop("BFL_RUNTIME_ROOT", None)
    with patch.dict(os.environ, environ, clear=True):
        resolved = backtest_baseline_path(project_root, STATIC_GATE_SCHEME_ID)

    assert resolved == (
        project_root
        / "reports"
        / "refactor_baseline"
        / STATIC_GATE_SCHEME_ID
        / "backtest_no_persist.json"
    )


def test_signal_gap_fill_report_root_uses_runtime_root(tmp_path: Path) -> None:
    from harness.cli import signal_gap_fill_report_root

    runtime_root = tmp_path / "state"
    project_root = tmp_path / "release"

    with patch.dict(os.environ, _production_environ(runtime_root), clear=False):
        resolved = signal_gap_fill_report_root(project_root)

    assert resolved.is_relative_to(runtime_root.resolve())


def test_gate_report_dir_default_uses_runtime_root(tmp_path: Path) -> None:
    from harness.cli import default_report_dir

    runtime_root = tmp_path / "state"
    project_root = tmp_path / "release"

    with patch.dict(os.environ, _production_environ(runtime_root), clear=False):
        resolved = default_report_dir(project_root, STATIC_GATE_SCHEME_ID)

    assert resolved.is_relative_to(runtime_root.resolve())


def test_signal_gap_fill_report_root_preserves_development_default(
    tmp_path: Path,
) -> None:
    from harness.cli import signal_gap_fill_report_root

    project_root = tmp_path / "worktree"

    environ = dict(os.environ)
    environ.pop("BFL_DEPLOYMENT_TARGET", None)
    environ.pop("BFL_RUNTIME_ROOT", None)
    with patch.dict(os.environ, environ, clear=True):
        resolved = signal_gap_fill_report_root(project_root)

    assert resolved == project_root / "reports" / "harness" / "signal-gap-fill"


def test_gate_report_dir_default_preserves_development_default(
    tmp_path: Path,
) -> None:
    from harness.cli import default_report_dir

    project_root = tmp_path / "worktree"

    environ = dict(os.environ)
    environ.pop("BFL_DEPLOYMENT_TARGET", None)
    environ.pop("BFL_RUNTIME_ROOT", None)
    with patch.dict(os.environ, environ, clear=True):
        resolved = default_report_dir(project_root, STATIC_GATE_SCHEME_ID)

    expected_parent = project_root / "reports" / "harness" / STATIC_GATE_SCHEME_ID
    assert resolved.parent == expected_parent
    # 时间戳形如 20260822T004500Z
    assert re.fullmatch(r"\d{8}T\d{6}Z", resolved.name)


# --------------------------------------------------------------------------
# 授权重放保护必须跨 release 存活
# --------------------------------------------------------------------------


def test_authorization_replay_store_survives_release_change(
    tmp_path: Path,
) -> None:
    """同一 runtime root 下，换 project_root 不得让已用 token 复活。"""
    from harness.authorization import used_tokens_path

    runtime_root = tmp_path / "state"
    release_a = tmp_path / "releases" / "aaaa"
    release_b = tmp_path / "releases" / "bbbb"

    with patch.dict(os.environ, _production_environ(runtime_root), clear=False):
        path_a = used_tokens_path(release_a)
        path_b = used_tokens_path(release_b)

    assert path_a == path_b, (
        "授权重放存储随 release 变化会使一次性 token 在切换 release 后复活"
    )


# --------------------------------------------------------------------------
# DataBridge provenance 必须从 runtime root 读取
# --------------------------------------------------------------------------


def test_data_bridge_provenance_reads_runtime_root(tmp_path: Path) -> None:
    from harness.blackbox_v2.gates import _data_bridge_provenance

    runtime_root = tmp_path / "state"
    project_root = tmp_path / "release"
    project_root.mkdir()

    ctx = SimpleNamespace(project_root=project_root)
    snapshot = SimpleNamespace(manifest_path=tmp_path / "manifest.json")

    with patch.dict(os.environ, _production_environ(runtime_root), clear=False):
        with pytest.raises(ValueError) as excinfo:
            _data_bridge_provenance(ctx, snapshot)

    message = str(excinfo.value)
    assert "backtest_artifacts" not in message, (
        "provenance 仍在读取开发兜底路径；生产 release 中该目录不存在"
    )
    assert str(runtime_root) in message

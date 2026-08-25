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
    """在真实 release 树上执行 StaticGate，release 树必须逐字节不变。

    覆盖范围：默认 report_dir 解析 + Gate 执行。不覆盖 dry-run/backtest 这类
    在子进程中执行算法的 Gate（它们需要数据库与算法环境），那部分由运维的 `-B` 合同和
    ECS 上「受控 harness 运行前后重算 digest」的现场验收兜底。
    """
    from harness.cli import default_report_dir
    from harness.context import GateContext
    from harness.gates.static_gate import StaticGate

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

    assert _tree_digest(release_root) == before, (
        "StaticGate 在生产形状下改变了 release 树；运行期状态必须落在 BFL_RUNTIME_ROOT 下"
    )
    assert result.gate_name == "static"
    assert report_dir.is_relative_to(runtime_root.resolve()), (
        "Harness 运行期目录未解析到外置 runtime root"
    )
    assert not report_dir.exists(), "StaticGate 不应再生成通用本地 JSON 报告"


# --------------------------------------------------------------------------
# 单元：四个路径点在生产形状下必须落在 runtime root
# --------------------------------------------------------------------------


def test_backtest_baseline_uses_runtime_root(tmp_path: Path) -> None:
    from harness.gates.backtest_gate import backtest_baseline_path

    runtime_root = tmp_path / "state"
    project_root = tmp_path / "release"

    with patch.dict(os.environ, _production_environ(runtime_root), clear=False):
        resolved = backtest_baseline_path(project_root, STATIC_GATE_SCHEME_ID)

    assert resolved.is_relative_to(runtime_root.resolve())
    assert resolved.name == "backtest_no_persist.json"




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

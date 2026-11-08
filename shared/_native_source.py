"""存量 Native 来源摘要、私有运行副本与解释器选择的内部实现。"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from shared import source_runtime_database as _database


def source_package_tree_sha256(path: Path) -> str:
    """计算 0629 source package 的内容与相对路径摘要。"""
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if (
            item.is_dir()
            or "__pycache__" in item.parts
            or item.suffix == ".pyc"
        ):
            continue
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class _SourceRuntime:
    """建立独立的源包副本，核对冻结身份后才注入私有数据库配置。"""

    def __init__(
        self,
        source_package_path: Path,
        expected_source_package_sha256: str,
        *,
        temporary_prefix: str,
        tree_sha256: Callable[[Path], str],
        mismatch_message: str,
        database_config: _database.SourceRuntimeDatabaseConfig | None = None,
    ) -> None:
        self.source_package_path = source_package_path
        self.expected_source_package_sha256 = expected_source_package_sha256
        self.temporary_prefix = temporary_prefix
        self.tree_sha256 = tree_sha256
        self.mismatch_message = mismatch_message
        self.database_config = database_config
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.source_root: Path | None = None

    def __enter__(self) -> Path:
        self.tempdir = tempfile.TemporaryDirectory(prefix=self.temporary_prefix)
        self.source_root = Path(self.tempdir.name) / "forecast_project"
        try:
            _database.assert_source_package_tree_safe(self.source_package_path)
            shutil.copytree(self.source_package_path, self.source_root)
            _database.prepare_private_source_runtime_tree(self.source_root)
            _database.clear_macos_quarantine(self.source_root)
            copied_sha256 = self.tree_sha256(self.source_root)
            if copied_sha256 != self.expected_source_package_sha256:
                raise RuntimeError(self.mismatch_message)
            config = self.database_config or _database.load_source_runtime_database_config()
            _database.install_source_runtime_database_config(self.source_root, config)
        except BaseException:
            self.tempdir.cleanup()
            self.tempdir = None
            self.source_root = None
            raise
        return self.source_root

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.tempdir is not None:
            self.tempdir.cleanup()


def _source_python_command(environment_name: str) -> list[str]:
    """按显式环境变量、本机固定解释器、conda 命令的原顺序选择执行环境。"""
    explicit = os.environ.get(environment_name)
    if explicit:
        return [explicit]
    local = Path.home() / "miniconda3" / "envs" / "forecast_env" / "bin" / "python"
    if local.exists():
        return [str(local)]
    return ["conda", "run", "--no-capture-output", "-n", "forecast_env", "python"]

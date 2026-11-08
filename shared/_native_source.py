"""存量 Native 来源证据的内部实现。"""

from __future__ import annotations

import hashlib
from pathlib import Path


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

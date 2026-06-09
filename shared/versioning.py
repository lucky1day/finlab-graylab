from __future__ import annotations

import hashlib
from pathlib import Path


def compute_code_hash(scheme_dir: str | Path) -> str:
    """计算方案代码哈希，覆盖 predict.py 与 core/**/*.py。"""
    root = Path(scheme_dir)
    paths = [root / "predict.py"]
    core_dir = root / "core"
    if core_dir.exists():
        paths.extend(sorted(core_dir.rglob("*.py")))
    return _hash_files(root, (path for path in paths if path.exists()))


def compute_config_hash(config_path: str | Path) -> str:
    """计算 config.yaml 内容哈希。"""
    return _hash_bytes(Path(config_path).read_bytes())


def compute_manifest_hash(scheme_dir: str | Path) -> str | None:
    """计算可选 manifest 文件哈希；缺失则返回 None。"""
    root = Path(scheme_dir)
    for name in ("manifest.yaml", "manifest.yml", "manifest.json"):
        path = root / name
        if path.exists():
            return _hash_bytes(path.read_bytes())
    return None


def compute_scheme_version(code_hash: str) -> str:
    """从代码哈希派生稳定方案版本。"""
    return code_hash[:12]


def _hash_files(root: Path, paths) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(item) for item in paths):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DAILY_0629_SOURCE_ROLE = "source_original_daily_0629_algorithm"
PLATFORM_CURRENT_DAILY_0629_ROLE = "platform_current_daily_0629_adapter"
DAILY_0629_SOURCE_BATCH = "daily_0629"


@dataclass(frozen=True)
class Daily0629SourceEvidence:
    """日度 0629 原始二进制算法证据。"""

    scheme_id: str
    source_role: str
    generator: str
    manifest_path: Path
    source_package_path: Path
    source_package_hash: str
    runner_module: str
    live_runner_module: str
    frequency: str
    target_tenor: str
    final_select_id: str
    candidate_id: str
    target_col: str
    model_id: str


def daily_0629_source_batch_dir(project_root: Path) -> Path:
    return project_root / "source_evidence" / "benchmark_batches" / DAILY_0629_SOURCE_BATCH


def require_daily_0629_source_evidence(
    scheme_id: str,
    *,
    project_root: Path | None = None,
) -> Daily0629SourceEvidence:
    """读取并校验日度 0629 source-original 二进制 runner 证据。"""
    root = project_root or Path(__file__).resolve().parents[1]
    batch_dir = daily_0629_source_batch_dir(root)
    manifest_path = batch_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"daily 0629 source evidence missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schemes = manifest.get("schemes")
    if not isinstance(schemes, dict) or scheme_id not in schemes:
        raise RuntimeError(f"daily 0629 source evidence missing scheme entry: {scheme_id}")
    entry = schemes[scheme_id]
    if not isinstance(entry, dict):
        raise RuntimeError(f"daily 0629 source evidence entry must be an object: {scheme_id}")

    source_role = str(entry.get("source_role") or manifest.get("source_role") or "").strip()
    if source_role != DAILY_0629_SOURCE_ROLE:
        raise RuntimeError(
            f"{scheme_id}: daily 0629 source_role must be {DAILY_0629_SOURCE_ROLE!r}, got {source_role!r}"
        )
    source_package = _required_relative_dir(manifest, "source_package", batch_dir, scheme_id)
    runner_module = str(manifest.get("runner_module") or "daily.run_backtest").strip()
    live_runner_module = str(manifest.get("live_runner_module") or "daily.run_daily").strip()
    frequency = str(entry.get("frequency") or "").strip()
    target_tenor = str(entry.get("target_tenor") or "").strip()
    final_select_id = str(entry.get("final_select_id") or "").strip()
    candidate_id = str(entry.get("candidate_id") or "").strip()
    target_col = str(entry.get("target_col") or "").strip()
    model_id = str(entry.get("model_id") or candidate_id).strip()
    generator = str(entry.get("generator") or f"source_package.{runner_module}:{frequency}").strip()
    _reject_unsupported_daily_scheme(scheme_id, frequency, target_tenor, final_select_id, target_col)
    if not all((runner_module, live_runner_module, frequency, target_tenor, final_select_id, candidate_id, target_col)):
        raise RuntimeError(f"{scheme_id}: daily 0629 source evidence missing runner/target/model metadata")

    return Daily0629SourceEvidence(
        scheme_id=scheme_id,
        source_role=source_role,
        generator=generator,
        manifest_path=manifest_path,
        source_package_path=source_package,
        source_package_hash=source_package_tree_sha256(source_package),
        runner_module=runner_module,
        live_runner_module=live_runner_module,
        frequency=frequency,
        target_tenor=target_tenor,
        final_select_id=final_select_id,
        candidate_id=candidate_id,
        target_col=target_col,
        model_id=model_id,
    )


def _required_relative_dir(manifest: dict[str, Any], key: str, batch_dir: Path, scheme_id: str) -> Path:
    value = str(manifest.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"{scheme_id}: daily 0629 source manifest missing {key}")
    path = (batch_dir / value).resolve()
    if not path.is_dir():
        raise RuntimeError(f"{scheme_id}: daily 0629 source manifest {key} not found: {path}")
    return path


def _reject_unsupported_daily_scheme(
    scheme_id: str,
    frequency: str,
    target_tenor: str,
    final_select_id: str,
    target_col: str,
) -> None:
    allowed = {
        "daily_1y_xgb_1y13_0629": ("D1Y", "1Y", "1Y13", "TB1YWI0C"),
        "daily_5y_lgbm_5y10_0629": ("D5Y", "5Y", "5Y10", "TB5YWI0C"),
        "daily_10y_lgbm_10y04_0629": ("D10Y", "10Y", "10Y04", "TB0YWI0C"),
    }
    if scheme_id not in allowed:
        return
    expected = allowed[scheme_id]
    actual = (frequency, target_tenor, final_select_id, target_col)
    if actual != expected:
        raise RuntimeError(f"{scheme_id}: expected daily 0629 source {expected}, got {actual}")


def source_package_tree_sha256(path: Path) -> str:
    """计算 0629 source package 的内容与相对路径摘要。"""
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_dir():
            continue
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()

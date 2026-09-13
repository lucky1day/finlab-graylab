from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.source_runtime_database import (
    require_manifest_source_package_sha256,
)


WEEKLY_AVERAGE_SOURCE_ROLE = "source_original_weekly_average_algorithm"
WEEKLY_AVERAGE_SOURCE_BATCH = "weekly_average_0529"

WEEKLY_AVERAGE_POINT_SCHEMES: dict[str, str] = {
    "weekly_avg_5y_direct_0529": "weekly_5y_direct_0529",
    "weekly_avg_7y_cross_d_overlay_0529": "weekly_7y_cross_d_overlay_0529",
    "weekly_avg_10y_d_overlay_0529": "weekly_10y_d_overlay_0529",
}


@dataclass(frozen=True)
class WeeklyAverageSourceEvidence:
    """周平均原始算法证据。"""

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
    target_column: str
    model_id: str


def weekly_average_source_batch_dir(project_root: Path) -> Path:
    return project_root / "source_evidence" / "benchmark_batches" / "model_muti_0529" / WEEKLY_AVERAGE_SOURCE_BATCH


def require_weekly_average_source_evidence(
    scheme_id: str,
    *,
    project_root: Path | None = None,
) -> WeeklyAverageSourceEvidence:
    """读取并校验周平均原始算法证据，缺失或指向周度单点时 fail-closed。"""
    root = project_root or Path(__file__).resolve().parents[1]
    batch_dir = weekly_average_source_batch_dir(root)
    manifest_path = batch_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(
            "weekly average source evidence missing: "
            f"{manifest_path}; cannot reuse weekly point algorithm for {scheme_id}"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schemes = manifest.get("schemes")
    if not isinstance(schemes, dict) or scheme_id not in schemes:
        raise RuntimeError(f"weekly average source evidence missing scheme entry: {scheme_id}")
    entry = schemes[scheme_id]
    if not isinstance(entry, dict):
        raise RuntimeError(f"weekly average source evidence entry must be an object: {scheme_id}")

    source_role = str(entry.get("source_role") or manifest.get("source_role") or "").strip()
    if source_role != WEEKLY_AVERAGE_SOURCE_ROLE:
        raise RuntimeError(
            f"{scheme_id}: weekly average source evidence source_role must be "
            f"{WEEKLY_AVERAGE_SOURCE_ROLE!r}, got {source_role!r}"
        )

    source_package = _required_relative_dir(manifest, "source_package", batch_dir, scheme_id)
    source_package_hash = require_manifest_source_package_sha256(
        manifest,
        source_package,
        tree_sha256=source_package_tree_sha256,
        label="weekly average",
    )
    runner_module = str(manifest.get("runner_module") or "weekly.run_backtest").strip()
    live_runner_module = str(manifest.get("live_runner_module") or "weekly.run_weekly").strip()
    frequency = str(entry.get("frequency") or "").strip()
    target_tenor = str(entry.get("target_tenor") or "").strip()
    target_column = str(entry.get("target_column") or "").strip()
    model_id = str(entry.get("model_id") or "").strip()
    generator = str(entry.get("generator") or f"source_package.{runner_module}:{frequency}").strip()
    _reject_unsupported_weekly_average_tenor(scheme_id, frequency, target_tenor)
    _reject_weekly_point_references(scheme_id, generator, str(source_package))
    if not frequency or not target_tenor or not target_column or not model_id:
        raise RuntimeError(f"{scheme_id}: weekly average source evidence missing frequency/target/model metadata")

    return WeeklyAverageSourceEvidence(
        scheme_id=scheme_id,
        source_role=source_role,
        generator=generator,
        manifest_path=manifest_path,
        source_package_path=source_package,
        source_package_hash=source_package_hash,
        runner_module=runner_module,
        live_runner_module=live_runner_module,
        frequency=frequency,
        target_tenor=target_tenor,
        target_column=target_column,
        model_id=model_id,
    )


def _required_relative_dir(entry: dict[str, Any], key: str, batch_dir: Path, scheme_id: str) -> Path:
    raw = str(entry.get(key) or "").strip()
    if not raw:
        raise RuntimeError(f"{scheme_id}: weekly average source evidence missing {key}")
    path = (batch_dir / raw).resolve()
    if batch_dir.resolve() not in path.parents:
        raise RuntimeError(f"{scheme_id}: weekly average source evidence {key} must stay under {batch_dir}")
    if not path.exists() or not path.is_dir():
        raise RuntimeError(f"{scheme_id}: weekly average source evidence directory missing: {path}")
    return path


def _reject_unsupported_weekly_average_tenor(scheme_id: str, frequency: str, target_tenor: str) -> None:
    if frequency == "W7Y" or target_tenor == "7Y":
        raise RuntimeError(
            f"{scheme_id}: source weekly average package does not support 7Y; "
            "valid frequencies are W1Y/W5Y/W10Y"
        )
    if frequency and frequency not in {"W1Y", "W5Y", "W10Y"}:
        raise RuntimeError(f"{scheme_id}: unsupported weekly average frequency {frequency!r}")
    if target_tenor and target_tenor not in {"1Y", "5Y", "10Y"}:
        raise RuntimeError(f"{scheme_id}: unsupported weekly average target_tenor {target_tenor!r}")


def _reject_weekly_point_references(scheme_id: str, *values: str) -> None:
    point_scheme = WEEKLY_AVERAGE_POINT_SCHEMES.get(scheme_id)
    if not point_scheme:
        return
    markers = (
        point_scheme,
        f"backtests.{point_scheme}_reproduction",
        f"schemes/{point_scheme}",
        f"schemes.{point_scheme}",
    )
    haystack = "\n".join(values)
    for marker in markers:
        if marker in haystack:
            raise RuntimeError(
                f"{scheme_id}: weekly average source evidence must not reference weekly point "
                f"algorithm {point_scheme}"
            )


def source_package_tree_sha256(path: Path) -> str:
    """计算周平均 source package 的内容与相对路径摘要。"""
    digest = hashlib.sha256()
    for child in sorted(
        item
        for item in path.rglob("*")
        if item.is_file()
        and "__pycache__" not in item.parts
        and item.suffix != ".pyc"
    ):
        rel = child.relative_to(path).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()

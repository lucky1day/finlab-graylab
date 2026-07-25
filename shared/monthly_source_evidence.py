from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.source_runtime_database import (
    require_manifest_source_package_sha256,
)


MONTHLY_SOURCE_ROLE = "source_original_monthly_algorithm"
PLATFORM_CURRENT_MONTHLY_ROLE = "platform_current_monthly_adapter"
MONTHLY_SOURCE_BATCH = "monthly_0629"


@dataclass(frozen=True)
class MonthlySourceEvidence:
    """月度原始算法证据。"""

    scheme_id: str
    source_role: str
    generator: str
    manifest_path: Path
    source_package_path: Path
    source_package_hash: str
    runner_module: str
    frequency: str
    target_tenor: str
    final_select_id: str
    model_id: str
    candidate_id: str


def monthly_source_batch_dir(project_root: Path) -> Path:
    return project_root / "source_evidence" / "benchmark_batches" / MONTHLY_SOURCE_BATCH


def require_monthly_source_evidence(
    scheme_id: str,
    *,
    project_root: Path | None = None,
) -> MonthlySourceEvidence:
    """读取并校验月度原始算法证据。"""
    root = project_root or Path(__file__).resolve().parents[1]
    batch_dir = monthly_source_batch_dir(root)
    manifest_path = batch_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"monthly source evidence missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schemes = manifest.get("schemes")
    if not isinstance(schemes, dict) or scheme_id not in schemes:
        raise RuntimeError(f"monthly source evidence missing scheme entry: {scheme_id}")
    entry = schemes[scheme_id]
    if not isinstance(entry, dict):
        raise RuntimeError(f"monthly source evidence entry must be an object: {scheme_id}")

    source_role = str(entry.get("source_role") or manifest.get("source_role") or "").strip()
    if source_role != MONTHLY_SOURCE_ROLE:
        raise RuntimeError(
            f"{scheme_id}: monthly source evidence source_role must be {MONTHLY_SOURCE_ROLE!r}, got {source_role!r}"
        )
    source_package = _required_relative_dir(manifest, "source_package", batch_dir, scheme_id)
    source_package_hash = require_manifest_source_package_sha256(
        manifest,
        source_package,
        tree_sha256=source_package_tree_sha256,
        label="monthly",
    )
    runner_module = str(manifest.get("runner_module") or "src.monthly.run_monthly_pipeline").strip()
    frequency = str(entry.get("frequency") or "").strip()
    target_tenor = str(entry.get("target_tenor") or "").strip()
    final_select_id = str(entry.get("final_select_id") or "").strip()
    model_id = str(entry.get("model_id") or "").strip()
    candidate_id = str(entry.get("candidate_id") or "").strip()
    generator = str(entry.get("generator") or f"source_package.{runner_module}:{frequency}").strip()
    _reject_unsupported_monthly_scheme(scheme_id, frequency, target_tenor)
    if not all((frequency, target_tenor, final_select_id, model_id, candidate_id)):
        raise RuntimeError(f"{scheme_id}: monthly source evidence missing frequency/target/model metadata")

    return MonthlySourceEvidence(
        scheme_id=scheme_id,
        source_role=source_role,
        generator=generator,
        manifest_path=manifest_path,
        source_package_path=source_package,
        source_package_hash=source_package_hash,
        runner_module=runner_module,
        frequency=frequency,
        target_tenor=target_tenor,
        final_select_id=final_select_id,
        model_id=model_id,
        candidate_id=candidate_id,
    )


def _required_relative_dir(manifest: dict[str, Any], key: str, batch_dir: Path, scheme_id: str) -> Path:
    value = str(manifest.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"{scheme_id}: monthly source manifest missing {key}")
    path = (batch_dir / value).resolve()
    if batch_dir.resolve() not in path.parents:
        raise RuntimeError(
            f"{scheme_id}: monthly source manifest {key} "
            f"must stay under {batch_dir}"
        )
    if not path.is_dir():
        raise RuntimeError(f"{scheme_id}: monthly source manifest {key} not found: {path}")
    return path


def _reject_unsupported_monthly_scheme(scheme_id: str, frequency: str, target_tenor: str) -> None:
    allowed = {
        "monthly_1y_rf_top30_0629": ("M1Y", "1Y"),
        "monthly_5y_knn_top20_0629": ("M5Y", "5Y"),
        "monthly_10y_rf_top5_0629": ("M10Y", "10Y"),
    }
    if scheme_id not in allowed:
        return
    expected_frequency, expected_tenor = allowed[scheme_id]
    if (frequency, target_tenor) != (expected_frequency, expected_tenor):
        raise RuntimeError(
            f"{scheme_id}: expected monthly source {(expected_frequency, expected_tenor)}, "
            f"got {(frequency, target_tenor)}"
        )


def source_package_tree_sha256(path: Path) -> str:
    """计算月度 source package 的内容与相对路径摘要。"""
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

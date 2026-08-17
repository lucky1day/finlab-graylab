"""按部署目标过滤已完成校验的方案配置。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, TypeVar

from shared.one_shot_control_plane import (
    LAUNCHD_ONE_SHOT_CONTROL_PLANE,
    SYSTEMD_ONE_SHOT_CONTROL_PLANE,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT_MATRIX_PATH = PROJECT_ROOT / "deploy" / "scheme_deployment_matrix_v1.json"
DEPLOYMENT_TARGET_ENV = "BFL_DEPLOYMENT_TARGET"
MAC3_PRODUCTION_TARGET = "mac3-production"
ALIYUN_GRAY_TARGET = "aliyun-gray"
DEPLOYMENT_TARGETS = frozenset({MAC3_PRODUCTION_TARGET, ALIYUN_GRAY_TARGET})
CONTROL_PLANE_TARGETS = {
    LAUNCHD_ONE_SHOT_CONTROL_PLANE: MAC3_PRODUCTION_TARGET,
    SYSTEMD_ONE_SHOT_CONTROL_PLANE: ALIYUN_GRAY_TARGET,
}
T = TypeVar("T")


class DeploymentScopeError(ValueError):
    """部署目标或方案矩阵不成立。"""


def configured_deployment_target() -> str | None:
    """读取可选部署目标；未设置表示开发/Harness 全量发现。"""
    raw = os.getenv(DEPLOYMENT_TARGET_ENV)
    if raw is None:
        return None
    target = raw.strip()
    if target not in DEPLOYMENT_TARGETS:
        raise DeploymentScopeError("unsupported deployment target")
    return target


def require_deployment_target_for_control_plane(control_plane: str) -> str:
    """生产 one-shot 必须显式声明与控制面匹配的目标。"""
    target = configured_deployment_target()
    expected = CONTROL_PLANE_TARGETS.get(str(control_plane).strip())
    if target is None:
        raise DeploymentScopeError("deployment target is required")
    if expected is None or target != expected:
        raise DeploymentScopeError("deployment target does not match control plane")
    return target


def _load_matrix(path: Path, scheme_ids: list[str]) -> dict[str, frozenset[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentScopeError("deployment matrix is unavailable") from exc
    if not isinstance(payload, dict):
        raise DeploymentScopeError("deployment matrix root must be an object")
    if payload.get("schema_version") != "scheme-deployment-matrix-v1":
        raise DeploymentScopeError("unsupported deployment matrix schema")
    if payload.get("targets") != [
        MAC3_PRODUCTION_TARGET,
        ALIYUN_GRAY_TARGET,
    ]:
        raise DeploymentScopeError("deployment matrix targets are invalid")
    raw_schemes = payload.get("schemes")
    if not isinstance(raw_schemes, dict):
        raise DeploymentScopeError("deployment matrix schemes must be an object")
    if len(scheme_ids) != len(set(scheme_ids)):
        raise DeploymentScopeError("duplicate discovered scheme id")
    if set(raw_schemes) != set(scheme_ids):
        raise DeploymentScopeError("deployment matrix scheme coverage mismatch")
    matrix: dict[str, frozenset[str]] = {}
    for scheme_id, raw_targets in raw_schemes.items():
        if (
            not isinstance(raw_targets, list)
            or any(not isinstance(value, str) for value in raw_targets)
            or len(raw_targets) != len(set(raw_targets))
            or not set(raw_targets).issubset(DEPLOYMENT_TARGETS)
        ):
            raise DeploymentScopeError("deployment matrix contains invalid target list")
        matrix[str(scheme_id)] = frozenset(raw_targets)
    return matrix


def filter_schemes_for_configured_target(
    configs: Iterable[T],
    *,
    matrix_path: Path | None = None,
) -> list[T]:
    """无目标时保留全量；有目标时严格校验矩阵后过滤。"""
    items = list(configs)
    target = configured_deployment_target()
    if target is None:
        return items
    scheme_ids = [str(getattr(item, "scheme_id", "")) for item in items]
    matrix = _load_matrix(matrix_path or DEPLOYMENT_MATRIX_PATH, scheme_ids)
    return [
        item
        for item in items
        if target in matrix[str(getattr(item, "scheme_id", ""))]
    ]

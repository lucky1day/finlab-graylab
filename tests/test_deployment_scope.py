from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = PROJECT_ROOT / "deploy" / "scheme_deployment_matrix_v1.json"
MAC3_TARGET = "mac3-production"
ALIYUN_TARGET = "aliyun-gray"
MAC_ONLY_SCHEME_IDS = frozenset(
    {
        "daily_10y_lgbm_10y04_0629",
        "daily_1y_xgb_1y13_0629",
        "daily_5y_2_v28",
        "daily_5y_lgbm_5y10_0629",
        "daily_7y_1_v28",
        "liwei_0616_10y01_cons_say_k3_div_k10",
        "liwei_0616_10y01_full_oos_k3_div_k10",
        "liwei_0616_10y02_cons_say_k3_div_k5",
        "liwei_0616_5y01_full_oos_k3_div_k10",
        "liwei_0616_cons_sda_k3_div_k10",
        "monthly_10y_rf_top5_0629",
        "monthly_1y_rf_top30_0629",
        "monthly_5y_knn_top20_0629",
        "t1_daily",
        "t5_daily",
        "weekly_10y_d_overlay_0529",
        "weekly_5y_direct_0529",
        "weekly_7y_cross_d_overlay_0529",
        "weekly_avg_10y_lgbm_0529",
        "weekly_avg_1y_lgbm_0529",
        "weekly_avg_5y_lgbm_0529",
    }
)


def _discover(target: str | None):
    from scheduler.discovery import discover_schemes

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BFL_DEPLOYMENT_TARGET", None)
        if target is not None:
            os.environ["BFL_DEPLOYMENT_TARGET"] = target
        return discover_schemes()


def _matrix_schemes() -> dict[str, list[str]]:
    payload = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    return payload["schemes"]


def test_each_target_discovery_matches_its_matrix_entries() -> None:
    matrix = _matrix_schemes()
    for target in (MAC3_TARGET, ALIYUN_TARGET):
        expected = {
            scheme_id
            for scheme_id, targets in matrix.items()
            if target in targets
        }
        actual = {cfg.scheme_id for cfg in _discover(target)}
        assert actual == expected, target
        assert actual, f"{target} 过滤后不应为空"


def test_matrix_covers_every_discovered_scheme_exactly_once() -> None:
    payload = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    discovered_ids = {cfg.scheme_id for cfg in _discover(None)}
    assert payload["schema_version"] == "scheme-deployment-matrix-v1"
    assert payload["targets"] == [MAC3_TARGET, ALIYUN_TARGET]
    assert set(payload["schemes"]) == discovered_ids
    assert {
        scheme_id
        for scheme_id, targets in payload["schemes"].items()
        if targets == [MAC3_TARGET]
    } == MAC_ONLY_SCHEME_IDS


def test_unknown_deployment_target_fails_closed() -> None:
    from scheduler.deployment_scope import DeploymentScopeError

    with pytest.raises(DeploymentScopeError, match="unsupported deployment target"):
        _discover("unknown-host")


def test_incomplete_matrix_fails_closed(tmp_path: Path) -> None:
    from scheduler.deployment_scope import (
        DeploymentScopeError,
        filter_schemes_for_configured_target,
    )

    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "schema_version": "scheme-deployment-matrix-v1",
                "targets": [MAC3_TARGET, ALIYUN_TARGET],
                "schemes": {"only_one": [MAC3_TARGET]},
            }
        ),
        encoding="utf-8",
    )
    configs = [
        SimpleNamespace(scheme_id="only_one"),
        SimpleNamespace(scheme_id="missing"),
    ]
    with patch.dict(os.environ, {"BFL_DEPLOYMENT_TARGET": ALIYUN_TARGET}):
        with pytest.raises(DeploymentScopeError, match="coverage mismatch"):
            filter_schemes_for_configured_target(
                configs,
                matrix_path=matrix_path,
            )


def test_duplicate_or_unknown_matrix_targets_fail_closed(
    tmp_path: Path,
) -> None:
    from scheduler.deployment_scope import (
        DeploymentScopeError,
        filter_schemes_for_configured_target,
    )

    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "schema_version": "scheme-deployment-matrix-v1",
                "targets": [MAC3_TARGET, ALIYUN_TARGET],
                "schemes": {
                    "demo": [ALIYUN_TARGET, ALIYUN_TARGET, "other"]
                },
            }
        ),
        encoding="utf-8",
    )
    with patch.dict(os.environ, {"BFL_DEPLOYMENT_TARGET": ALIYUN_TARGET}):
        with pytest.raises(DeploymentScopeError, match="invalid target list"):
            filter_schemes_for_configured_target(
                [SimpleNamespace(scheme_id="demo")],
                matrix_path=matrix_path,
            )

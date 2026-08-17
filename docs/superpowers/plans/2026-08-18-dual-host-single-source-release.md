# Dual-Host Single-Source Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Mac3 与阿里云 ECS 从同一精确提交和同一源码 release 运行，并通过一个 discovery 单点适配器把 Mac3 的 65 个方案与 ECS 的 56 个方案安全分开。

**Architecture:** 新增 `deploy/scheme_deployment_matrix_v1.json` 声明每个 scheme 可进入的部署目标，`scheduler/deployment_scope.py` 负责严格校验目标与矩阵，`scheduler.discovery` 在存在 `BFL_DEPLOYMENT_TARGET` 时进行唯一一次过滤。one-shot runner 在读取 DataBridge、连接数据库或启动算法前校验 launchd/Mac3 与 systemd/ECS 的固定配对；Registry、executor、backend API、actuals 核心和 Harness 不实现第二份过滤逻辑。

**Tech Stack:** Python 3.12、pytest/unittest、JSON、launchd plist、systemd unit、Git immutable release。

---

## 0. 实施边界与文件结构

实施基线为 `af4248535b6be0b0bd1533c324fe1c4aaa9a4877`。所有代码和文档提交只进入 `codex/aliyun-db-clone-20260816`；`master@2b62a2e9ae7c661f4a7f1741b5ff6c351819a4ad` 保持冻结。

本计划只修改以下职责明确的文件：

- Create `scheduler/deployment_scope.py`：目标解析、控制面配对和矩阵严格校验。
- Modify `scheduler/discovery.py`：完整 config 加载后调用一次 deployment scope 过滤。
- Create `deploy/scheme_deployment_matrix_v1.json`：65 个 scheme 的目标资格数据。
- Modify `scheduler/launchd_prediction_runner.py`：在任何运行期副作用前校验控制面/目标。
- Create `tests/test_deployment_scope.py`：矩阵、65/56/9 和 fail-closed 契约。
- Modify `tests/test_launchd_prediction_runner.py`、`tests/test_systemd_control_plane.py`：one-shot 目标配对测试。
- Modify 9 个 `schemes/*/config.yaml`：只把 `status: paused` 恢复为 canonical `status: active`。
- Modify `tests/test_aliyun_c56_candidate.py`：把 C56 环境范围从 config status 改为 deployment target。
- Modify 6 个 `deploy/launchd/*.plist` 与 6 个 `deploy/systemd/*.service`：写入对应部署目标；不修改 timer。
- Modify `tests/test_launchd_config_drift_audit.py`、`tests/test_systemd_control_plane.py`：服务模板契约。
- Modify `deploy/README.md`：记录目标变量、矩阵与 installed 配置授权边界。
- Modify `AGENTS.md`、`CLAUDE.md`：同步当前活动分支、冻结 master 和单一 release 决策。
- Modify `docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md`：增加 v1.26 决策并修正当前态描述。

不得修改 `scheduler/repository.py`、`scheduler/executor.py`、`backend/`、`scheduler/*actuals*`、`harness/`、数据库 migration 或算法实现。executor 已经通过 `discover_schemes()` 做 scheduled-live canonical 校验，因此会自然继承同一个过滤结果。

生产 Python 净新增上限为 120 行，只统计：

```text
scheduler/deployment_scope.py
scheduler/discovery.py
scheduler/launchd_prediction_runner.py
```

若净新增超过 120 行，或必须触碰上述禁止扩散的运行模块，立即停止实施并回到设计评审。

### Task 1: 部署矩阵与 discovery 单点过滤

**Files:**

- Create: `tests/test_deployment_scope.py`
- Create: `deploy/scheme_deployment_matrix_v1.json`
- Create: `scheduler/deployment_scope.py`
- Modify: `scheduler/discovery.py:3-24,213-225`

- [ ] **Step 1: 先写 deployment scope 失败测试**

创建 `tests/test_deployment_scope.py`，测试必须直接断言精确集合，不能只断言数量：

```python
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
        "daily_5y_lgbm_5y10_0629",
        "monthly_10y_rf_top5_0629",
        "monthly_1y_rf_top30_0629",
        "monthly_5y_knn_top20_0629",
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
        return discover_schemes(strict=True)


def test_unscoped_discovery_keeps_all_schemes_for_harness() -> None:
    configs = _discover(None)
    assert len(configs) == 65


def test_mac3_target_keeps_all_65_schemes() -> None:
    configs = _discover(MAC3_TARGET)
    assert len(configs) == 65


def test_aliyun_target_keeps_56_and_excludes_exact_nine() -> None:
    unscoped_ids = {cfg.scheme_id for cfg in _discover(None)}
    aliyun_ids = {cfg.scheme_id for cfg in _discover(ALIYUN_TARGET)}
    assert len(aliyun_ids) == 56
    assert unscoped_ids - aliyun_ids == MAC_ONLY_SCHEME_IDS


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
    configs = [SimpleNamespace(scheme_id="only_one"), SimpleNamespace(scheme_id="missing")]
    with patch.dict(os.environ, {"BFL_DEPLOYMENT_TARGET": ALIYUN_TARGET}):
        with pytest.raises(DeploymentScopeError, match="coverage mismatch"):
            filter_schemes_for_configured_target(configs, matrix_path=matrix_path)


def test_unknown_matrix_schema_fails_closed(tmp_path: Path) -> None:
    from scheduler.deployment_scope import (
        DeploymentScopeError,
        filter_schemes_for_configured_target,
    )

    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "schema_version": "future-schema",
                "targets": [MAC3_TARGET, ALIYUN_TARGET],
                "schemes": {"demo": [ALIYUN_TARGET]},
            }
        ),
        encoding="utf-8",
    )
    with patch.dict(os.environ, {"BFL_DEPLOYMENT_TARGET": ALIYUN_TARGET}):
        with pytest.raises(DeploymentScopeError, match="unsupported deployment matrix schema"):
            filter_schemes_for_configured_target(
                [SimpleNamespace(scheme_id="demo")],
                matrix_path=matrix_path,
            )


def test_duplicate_or_unknown_matrix_targets_fail_closed(tmp_path: Path) -> None:
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
                "schemes": {"demo": [ALIYUN_TARGET, ALIYUN_TARGET, "other"]},
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


def test_aliyun_excluded_scheme_has_no_scheduled_canonical_config() -> None:
    from scheduler import executor

    config = next(
        cfg
        for cfg in _discover(None)
        if cfg.scheme_id == "daily_1y_xgb_1y13_0629"
    )
    with patch.dict(
        os.environ,
        {"BFL_DEPLOYMENT_TARGET": ALIYUN_TARGET},
        clear=False,
    ):
        error = executor.scheduled_live_execution_configuration_error(
            config,
            scheduled_control_plane="systemd_one_shot",
            scheduled_execution_context=(
                executor._systemd_scheduled_execution_context()
            ),
        )

    assert error is not None
    assert "scheduled_live canonical configuration is unavailable" in error
```

- [ ] **Step 2: 运行测试并确认红灯原因正确**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest \
  -q tests/test_deployment_scope.py
```

Expected: FAIL，因为 `scheduler.deployment_scope` 和矩阵文件尚不存在；不能是数据库连接、scheme config 或第三方依赖错误。

- [ ] **Step 3: 添加精确 65 项 deployment matrix**

用 `apply_patch` 创建 `deploy/scheme_deployment_matrix_v1.json`。固定头部为：

```json
{
  "schema_version": "scheme-deployment-matrix-v1",
  "targets": ["mac3-production", "aliyun-gray"],
  "schemes": {
```

`schemes` 必须使用以下精确映射；前 56 个共享项使用两个目标，9 个 Mac-only 项只使用 `mac3-production`：

```json
    "cgb_a4_fundseason_10y": ["mac3-production", "aliyun-gray"],
    "cgb_a4_fundseason_1y": ["mac3-production", "aliyun-gray"],
    "cgb_a4_fundseason_3y": ["mac3-production", "aliyun-gray"],
    "cgb_a4_fundseason_5y": ["mac3-production", "aliyun-gray"],
    "cgb_a4_fundseason_7y": ["mac3-production", "aliyun-gray"],
    "cgb_causal_wk_1y": ["mac3-production", "aliyun-gray"],
    "cgb_causal_wk_1y_v128": ["mac3-production", "aliyun-gray"],
    "cgb_causal_wk_3y": ["mac3-production", "aliyun-gray"],
    "daily_10y_lgbm_10y04_0629": ["mac3-production"],
    "daily_1y_xgb_1y13_0629": ["mac3-production"],
    "daily_5y_2_v28": ["mac3-production", "aliyun-gray"],
    "daily_5y_lgbm_5y10_0629": ["mac3-production"],
    "daily_7y_1_v28": ["mac3-production", "aliyun-gray"],
    "five_y_t5_lgbm_3y_anti_lag252_b8_v1": ["mac3-production", "aliyun-gray"],
    "five_y_t5_lgbm_3y_z_anti180_b12_v1": ["mac3-production", "aliyun-gray"],
    "five_y_t5_xgb_spr_3y1y_b8_v1": ["mac3-production", "aliyun-gray"],
    "liwei_0616_10y01_cons_say_k3_div_k10": ["mac3-production", "aliyun-gray"],
    "liwei_0616_10y01_full_oos_k3_div_k10": ["mac3-production", "aliyun-gray"],
    "liwei_0616_10y02_cons_say_k3_div_k5": ["mac3-production", "aliyun-gray"],
    "liwei_0616_5y01_full_oos_k3_div_k10": ["mac3-production", "aliyun-gray"],
    "liwei_0616_5y_auc_static_all_k3_div_k10": ["mac3-production", "aliyun-gray"],
    "liwei_0616_5y_auc_yearly_all_k3_div_k10": ["mac3-production", "aliyun-gray"],
    "liwei_0616_5y_ic_yearly_all_k3_div_k10": ["mac3-production", "aliyun-gray"],
    "liwei_0616_7y01_cons_say_k3_div_k10": ["mac3-production", "aliyun-gray"],
    "liwei_0616_7y03_cons_all_k3_div_k8": ["mac3-production", "aliyun-gray"],
    "liwei_0616_cons_sda_k3_div_k10": ["mac3-production", "aliyun-gray"],
    "monthly_10y_rf_top5_0629": ["mac3-production"],
    "monthly_1y_rf_top30_0629": ["mac3-production"],
    "monthly_5y_knn_top20_0629": ["mac3-production"],
    "one_y_t1_quote_state_hv_v1": ["mac3-production", "aliyun-gray"],
    "one_y_t5_liq_excess_a_v1": ["mac3-production", "aliyun-gray"],
    "one_y_t5_liq_excess_a_w252_l7_v1": ["mac3-production", "aliyun-gray"],
    "one_y_t5_liq_excess_a_w350_l7_v1": ["mac3-production", "aliyun-gray"],
    "one_y_t5_liq_excess_b_w252_l7_v1": ["mac3-production", "aliyun-gray"],
    "one_y_t5_xgb_10y_streak_anti7_b8_v1": ["mac3-production", "aliyun-gray"],
    "one_y_t5_xgb_7y_cond_rev20_b12_v1": ["mac3-production", "aliyun-gray"],
    "one_y_t5_xgb_spr_zrev_10y5y_b12_v1": ["mac3-production", "aliyun-gray"],
    "seven_y_current55_lgbm_001_v2": ["mac3-production", "aliyun-gray"],
    "seven_y_current55_lgbm_002_v2": ["mac3-production", "aliyun-gray"],
    "seven_y_t5_lgbm_bf_z_anti40_b8_v1": ["mac3-production", "aliyun-gray"],
    "seven_y_t5_xgb_7y_rv_rev20_b0_v1": ["mac3-production", "aliyun-gray"],
    "seven_y_t5_xgb_bf_z_anti40_b0_v1": ["mac3-production", "aliyun-gray"],
    "t1_daily": ["mac3-production", "aliyun-gray"],
    "t5_daily": ["mac3-production", "aliyun-gray"],
    "ten_y_t5_maj3_k3_ic_static_v1": ["mac3-production", "aliyun-gray"],
    "ten_y_t5_maj4_k3_ic_static_v1": ["mac3-production", "aliyun-gray"],
    "ten_y_t5_maj4_k3_ic_yearly_v1": ["mac3-production", "aliyun-gray"],
    "ten_y_t5_say_k5_sharpe_static_v1": ["mac3-production", "aliyun-gray"],
    "three_y_adyn_lb1_k3_v1": ["mac3-production", "aliyun-gray"],
    "three_y_adyn_lb2_k1_v1": ["mac3-production", "aliyun-gray"],
    "three_y_t5_lgbm_7yanti_b12_v2": ["mac3-production", "aliyun-gray"],
    "three_y_t5_xgb_fxlead_b8_v2": ["mac3-production", "aliyun-gray"],
    "three_y_t5_xgb_tp_5y1y_b12_v2": ["mac3-production", "aliyun-gray"],
    "wavg_10y_gapflip_v5": ["mac3-production", "aliyun-gray"],
    "wavg_1y_gapflip_v5": ["mac3-production", "aliyun-gray"],
    "wavg_3y_gapflip_v5": ["mac3-production", "aliyun-gray"],
    "wavg_5y_gapflip_v5": ["mac3-production", "aliyun-gray"],
    "wavg_7y_gapflip_v5": ["mac3-production", "aliyun-gray"],
    "weekly_10y_d_overlay_0529": ["mac3-production", "aliyun-gray"],
    "weekly_10y_lgbm_point_v1": ["mac3-production", "aliyun-gray"],
    "weekly_5y_direct_0529": ["mac3-production", "aliyun-gray"],
    "weekly_7y_cross_d_overlay_0529": ["mac3-production", "aliyun-gray"],
    "weekly_avg_10y_lgbm_0529": ["mac3-production"],
    "weekly_avg_1y_lgbm_0529": ["mac3-production"],
    "weekly_avg_5y_lgbm_0529": ["mac3-production"]
```

关闭 `schemes` 与根对象。不得生成平台特有的第二份 config 文件。

- [ ] **Step 4: 实现最小 deployment scope 模块**

创建 `scheduler/deployment_scope.py`，保持该文件聚焦且不 import discovery：

```python
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
    if payload.get("targets") != [MAC3_PRODUCTION_TARGET, ALIYUN_GRAY_TARGET]:
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
    return [item for item in items if target in matrix[str(getattr(item, "scheme_id", ""))]]
```

- [ ] **Step 5: 在 discovery 唯一接入过滤器**

在 `scheduler/discovery.py` import：

```python
from scheduler.deployment_scope import filter_schemes_for_configured_target
```

只把 `discover_schemes()` 的最终返回改为：

```python
    return filter_schemes_for_configured_target(configs)
```

不得在 config 加载循环中提前跳过 Mac-only 方案；即使 ECS 不执行，它们的 config 仍必须先通过现有 strict 校验。

- [ ] **Step 6: 运行 deployment scope 测试并确认绿灯**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest \
  -q tests/test_deployment_scope.py tests/test_blackbox_v2_discovery.py
```

Expected: PASS。若临时 schemes fixture 因全局矩阵覆盖失败，说明实现错误地在未设置目标时加载了生产矩阵，应修正为“未设置目标直接返回全量”。

- [ ] **Step 7: 提交单点过滤器**

```bash
git status --short
git add deploy/scheme_deployment_matrix_v1.json scheduler/deployment_scope.py \
  scheduler/discovery.py tests/test_deployment_scope.py
git commit -m "feat(deploy): scope schemes by deployment target"
```

### Task 2: one-shot 控制面与目标在副作用前强绑定

**Files:**

- Modify: `scheduler/launchd_prediction_runner.py:20-28,275-306`
- Modify: `tests/test_launchd_prediction_runner.py:1-12,134-204`
- Modify: `tests/test_systemd_control_plane.py:67-128`

- [ ] **Step 1: 写缺目标和目标错配的失败测试**

在 `tests/test_launchd_prediction_runner.py` 增加 `import os`，并加入：

```python
    def test_one_shot_requires_matching_target_before_runtime_access(self) -> None:
        from scheduler import launchd_prediction_runner as runner

        cases = ({}, {"BFL_DEPLOYMENT_TARGET": "aliyun-gray"})
        for environment in cases:
            with self.subTest(environment=environment), patch.dict(
                os.environ, environment, clear=True
            ), patch.object(
                runner.DataBridgeRefreshConfig, "from_env"
            ) as data_bridge_config, patch.object(
                runner, "create_engine_from_env"
            ) as create_engine:
                with self.assertRaisesRegex(
                    runner.LaunchdPredictionConfigurationError,
                    "deployment target does not match one-shot control plane",
                ):
                    runner.run(
                        "weekly",
                        predict_date="2026-08-01",
                        algo_env="forecast_env",
                    )

                data_bridge_config.assert_not_called()
                create_engine.assert_not_called()
```

同时给现有成功的 launchd runner 测试 context 增加：

```python
patch.dict(os.environ, {"BFL_DEPLOYMENT_TARGET": "mac3-production"}, clear=False)
```

给 `tests/test_systemd_control_plane.py::test_systemd_runner_passes_truthful_control_plane` 的 context 增加：

```python
patch.dict(os.environ, {"BFL_DEPLOYMENT_TARGET": "aliyun-gray"}, clear=False)
```

并增加一个 systemd/Mac3 交叉错配测试，断言 `DataBridgeRefreshConfig.from_env` 未调用：

```python
    def test_systemd_runner_rejects_mac3_target_before_runtime_access(self) -> None:
        from scheduler import launchd_prediction_runner as common_runner
        from scheduler import systemd_prediction_runner as runner

        with patch.dict(
            os.environ,
            {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
            clear=False,
        ), patch.object(
            common_runner.DataBridgeRefreshConfig, "from_env"
        ) as data_bridge_config:
            with self.assertRaisesRegex(
                common_runner.LaunchdPredictionConfigurationError,
                "deployment target does not match one-shot control plane",
            ):
                runner.run(
                    "weekly",
                    predict_date="2026-08-15",
                    algo_env="forecast_env",
                )

        data_bridge_config.assert_not_called()
```

- [ ] **Step 2: 运行 runner 测试并确认红灯**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_launchd_prediction_runner.py \
  tests/test_systemd_control_plane.py
```

Expected: 新增的缺失/错配测试 FAIL，因为 runner 尚未调用目标校验；原有成功测试可以因新增测试环境而继续通过。

- [ ] **Step 3: 在 `_run_one_shot` 最前端加入目标校验**

在 `scheduler/launchd_prediction_runner.py` import：

```python
from scheduler.deployment_scope import (
    DeploymentScopeError,
    require_deployment_target_for_control_plane,
)
```

在日期和 monthly day-15 校验之后、`DataBridgeRefreshConfig.from_env()` 之前加入：

```python
    try:
        require_deployment_target_for_control_plane(scheduled_control_plane)
    except DeploymentScopeError as exc:
        raise LaunchdPredictionConfigurationError(
            "deployment target does not match one-shot control plane"
        ) from exc
```

不要在 launchd 和 systemd 两个 wrapper 分别复制映射；两者继续复用 `_run_one_shot`。

- [ ] **Step 4: 运行 runner 与 executor canonical 测试**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_launchd_prediction_runner.py \
  tests/test_systemd_control_plane.py \
  tests/test_native_executor.py \
  tests/test_blackbox_v2_runner.py
```

Expected: PASS。executor 文件本身不应出现 diff；它重新调用 discovery 时会继承当前进程的 target。

- [ ] **Step 5: 提交 one-shot fail-closed 校验**

```bash
git status --short
git add scheduler/launchd_prediction_runner.py \
  tests/test_launchd_prediction_runner.py tests/test_systemd_control_plane.py
git commit -m "feat(scheduler): bind one-shot control plane to host target"
```

### Task 3: 恢复 canonical config 并把 C56 范围迁到矩阵

**Files:**

- Modify: `schemes/daily_10y_lgbm_10y04_0629/config.yaml:13`
- Modify: `schemes/daily_1y_xgb_1y13_0629/config.yaml:13`
- Modify: `schemes/daily_5y_lgbm_5y10_0629/config.yaml:13`
- Modify: `schemes/weekly_avg_10y_lgbm_0529/config.yaml:13`
- Modify: `schemes/weekly_avg_1y_lgbm_0529/config.yaml:13`
- Modify: `schemes/weekly_avg_5y_lgbm_0529/config.yaml:13`
- Modify: `schemes/monthly_10y_rf_top5_0629/config.yaml:14`
- Modify: `schemes/monthly_1y_rf_top30_0629/config.yaml:14`
- Modify: `schemes/monthly_5y_knn_top20_0629/config.yaml:14`
- Modify: `tests/test_aliyun_c56_candidate.py:29-69`

- [ ] **Step 1: 先把 C56 契约测试改成目标语义**

给 `tests/test_aliyun_c56_candidate.py` 增加 `import os`、`from unittest.mock import patch`，保留现有 `DEFERRED_NATIVE_SCHEME_IDS` 精确集合。把前两个测试替换为：

```python
def _target_configs(target: str | None) -> list[SchemeConfig]:
    from scheduler.discovery import discover_schemes

    project_root = Path(__file__).resolve().parents[1]
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BFL_DEPLOYMENT_TARGET", None)
        if target is not None:
            os.environ["BFL_DEPLOYMENT_TARGET"] = target
        return discover_schemes(project_root / "schemes", strict=True)


def test_canonical_configs_keep_all_65_schemes_active() -> None:
    from shared.source_runtime_database import SOURCE_RUNTIME_SCHEME_IDS

    configs = {config.scheme_id: config for config in _target_configs(None)}
    assert DEFERRED_NATIVE_SCHEME_IDS == SOURCE_RUNTIME_SCHEME_IDS
    assert len(configs) == 65
    assert all(config.status == "active" for config in configs.values())
    assert all(
        configs[scheme_id].runtime_type == "native_adapter"
        for scheme_id in DEFERRED_NATIVE_SCHEME_IDS
    )


def test_aliyun_execution_scope_is_56_active_base_and_60_composite() -> None:
    configs = _target_configs("aliyun-gray")
    assert len(configs) == 56
    assert all(config.status == "active" for config in configs)
    assert sum(len(config.tenors) for config in configs) == 60
    assert Counter(config.runtime_type for config in configs) == {
        "native_adapter": 17,
        "blackbox_v2": 39,
    }
    assert Counter(config.frequency for config in configs) == {
        "daily": 39,
        "weekly": 12,
        "monthly": 5,
    }
```

将本文件其余 `_candidate_configs()` 调用统一改为 `_target_configs(None)`，保证 policy 26、owner 69、weekly overlay exact version 和实现文件存在性检查均针对 canonical 全量源码。

- [ ] **Step 2: 运行 C56 测试并确认红灯**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_aliyun_c56_candidate.py
```

Expected: `test_canonical_configs_keep_all_65_schemes_active` FAIL，并精确报告当前 9 个 config 仍为 paused；Aliyun 目标测试应已由矩阵得到 56。

- [ ] **Step 3: 只恢复 9 个 config 的 status**

对上述 9 个文件分别执行唯一的语义变化：

```yaml
status: active
```

不得改 schedule、timeout、算法文件、input spec、backtest、description 或格式；不得新增 `version_status`。这一步的目的正是恢复原始 config bytes 对应的 canonical exact version。

- [ ] **Step 4: 验证 config、政策清单与 owner 契约**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_aliyun_c56_candidate.py \
  tests/test_onboarding_policy.py \
  tests/test_scheme_owner.py \
  tests/test_repository_registry.py
```

Expected: PASS，且测试输出不连接生产数据库。特别确认 unscoped=65/69 composite、Aliyun=56/60 composite、policy=26、owner=69。

- [ ] **Step 5: 提交 canonical config 恢复**

```bash
git status --short
git add schemes/daily_10y_lgbm_10y04_0629/config.yaml \
  schemes/daily_1y_xgb_1y13_0629/config.yaml \
  schemes/daily_5y_lgbm_5y10_0629/config.yaml \
  schemes/weekly_avg_10y_lgbm_0529/config.yaml \
  schemes/weekly_avg_1y_lgbm_0529/config.yaml \
  schemes/weekly_avg_5y_lgbm_0529/config.yaml \
  schemes/monthly_10y_rf_top5_0629/config.yaml \
  schemes/monthly_1y_rf_top30_0629/config.yaml \
  schemes/monthly_5y_knn_top20_0629/config.yaml \
  tests/test_aliyun_c56_candidate.py
git commit -m "fix(config): separate host scope from scheme lifecycle"
```

### Task 4: 两个平台的服务模板写入部署目标

**Files:**

- Modify: `deploy/launchd/com.bond-factor-lab.backend.plist`
- Modify: `deploy/launchd/com.bond-factor-lab.data-bridge-refresh.plist`
- Modify: `deploy/launchd/com.bond-factor-lab.daily-predictions.plist`
- Modify: `deploy/launchd/com.bond-factor-lab.weekly-predictions.plist`
- Modify: `deploy/launchd/com.bond-factor-lab.monthly-predictions.plist`
- Modify: `deploy/launchd/com.bond-factor-lab.actuals.plist`
- Modify: `deploy/systemd/bond-factor-lab-backend.service`
- Modify: `deploy/systemd/bond-factor-lab-data-bridge.service`
- Modify: `deploy/systemd/bond-factor-lab-prediction-daily.service`
- Modify: `deploy/systemd/bond-factor-lab-prediction-weekly.service`
- Modify: `deploy/systemd/bond-factor-lab-prediction-monthly.service`
- Modify: `deploy/systemd/bond-factor-lab-actuals.service`
- Modify: `tests/test_launchd_config_drift_audit.py`
- Modify: `tests/test_systemd_control_plane.py:220-281`
- Modify: `deploy/README.md`

- [ ] **Step 1: 先写服务模板目标测试**

在 `tests/test_launchd_config_drift_audit.py` 增加：

```python
APPLICATION_LAUNCHD_TEMPLATES = (
    "com.bond-factor-lab.backend.plist",
    "com.bond-factor-lab.data-bridge-refresh.plist",
    "com.bond-factor-lab.daily-predictions.plist",
    "com.bond-factor-lab.weekly-predictions.plist",
    "com.bond-factor-lab.monthly-predictions.plist",
    "com.bond-factor-lab.actuals.plist",
)


def test_application_launchd_templates_bind_mac3_target() -> None:
    for name in APPLICATION_LAUNCHD_TEMPLATES:
        with (LAUNCHD_ROOT / name).open("rb") as handle:
            payload = plistlib.load(handle)
        assert payload["EnvironmentVariables"]["BFL_DEPLOYMENT_TARGET"] == (
            "mac3-production"
        ), name
```

在 `tests/test_systemd_control_plane.py` 读取 `contents` 的现有模板测试中，对每个 `.service` 增加：

```python
                self.assertIn(
                    "Environment=BFL_DEPLOYMENT_TARGET=aliyun-gray",
                    content,
                    name,
                )
```

该断言只放在 `if name.endswith(".service")` 内，不能要求 timer 含环境变量。

- [ ] **Step 2: 运行模板测试并确认红灯**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_launchd_config_drift_audit.py \
  tests/test_systemd_control_plane.py
```

Expected: 新目标断言 FAIL，其他既有调度、Persistent=false、localhost 和 DataBridge producer 测试继续通过。

- [ ] **Step 3: 更新 launchd 仓库模板**

用 `apply_patch` 在 6 个 plist 的 `EnvironmentVariables` dictionary 中分别加入：

```xml
<key>BFL_DEPLOYMENT_TARGET</key>
<string>mac3-production</string>
```

不要修改 `com.bond-factor-lab.ssh-tunnel.plist`，不要执行 `plutil -replace` 写文件，不要复制到 `~/Library/LaunchAgents`，不要调用 `launchctl`。

- [ ] **Step 4: 更新 systemd 仓库模板**

用 `apply_patch` 在 6 个 `.service` 的 `[Service]` 环境段加入：

```ini
Environment=BFL_DEPLOYMENT_TARGET=aliyun-gray
```

不修改任何 `.timer`，不执行 `systemctl daemon-reload/start/enable`，不复制到 `/etc/systemd/system`。

- [ ] **Step 5: 更新部署说明**

在 `deploy/README.md` 的“双平台一次性控制面”后新增“部署目标与方案矩阵”小节，写明：

```markdown
## 部署目标与方案矩阵

Mac3 的应用 launchd 模板固定声明
`BFL_DEPLOYMENT_TARGET=mac3-production`，ECS 的应用 systemd service 固定声明
`BFL_DEPLOYMENT_TARGET=aliyun-gray`。生产 one-shot 的目标与控制面必须分别为
`launchd_one_shot/mac3-production` 和 `systemd_one_shot/aliyun-gray`；缺失或交叉配对会在
DataBridge 配置、数据库连接和算法子进程之前失败。

`deploy/scheme_deployment_matrix_v1.json` 是唯一主机资格清单。未设置目标的开发和 Harness
发现保持全量；生产服务设置目标后，discovery 严格校验矩阵与全部 config 一一覆盖再过滤。
矩阵不自动删除或暂停 Registry 行；目标移除与加入仍须分别完成受控 Registry 生命周期和读回。
模板变更不表示 installed launchd/systemd 已更新，现场安装、重载、启停和 timer enable 均需独立授权。
```

- [ ] **Step 6: 校验 plist、unit 和测试**

Run:

```bash
for plist in \
  deploy/launchd/com.bond-factor-lab.backend.plist \
  deploy/launchd/com.bond-factor-lab.data-bridge-refresh.plist \
  deploy/launchd/com.bond-factor-lab.daily-predictions.plist \
  deploy/launchd/com.bond-factor-lab.weekly-predictions.plist \
  deploy/launchd/com.bond-factor-lab.monthly-predictions.plist \
  deploy/launchd/com.bond-factor-lab.actuals.plist
do
  plutil -lint "$plist"
done
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_launchd_config_drift_audit.py \
  tests/test_systemd_control_plane.py
```

Expected: 每个 plist 输出 `OK`，pytest PASS。不得读取或修改 installed plist/units。

- [ ] **Step 7: 提交服务模板期望状态**

```bash
git status --short
git add deploy/launchd/com.bond-factor-lab.backend.plist \
  deploy/launchd/com.bond-factor-lab.data-bridge-refresh.plist \
  deploy/launchd/com.bond-factor-lab.daily-predictions.plist \
  deploy/launchd/com.bond-factor-lab.weekly-predictions.plist \
  deploy/launchd/com.bond-factor-lab.monthly-predictions.plist \
  deploy/launchd/com.bond-factor-lab.actuals.plist \
  deploy/systemd/bond-factor-lab-backend.service \
  deploy/systemd/bond-factor-lab-data-bridge.service \
  deploy/systemd/bond-factor-lab-prediction-daily.service \
  deploy/systemd/bond-factor-lab-prediction-weekly.service \
  deploy/systemd/bond-factor-lab-prediction-monthly.service \
  deploy/systemd/bond-factor-lab-actuals.service \
  deploy/README.md tests/test_launchd_config_drift_audit.py \
  tests/test_systemd_control_plane.py
git commit -m "deploy: bind service templates to host targets"
```

### Task 5: 同步项目规范与迁移当前态

**Files:**

- Modify: `AGENTS.md:9-24`
- Modify: `CLAUDE.md:9-24`
- Modify: `docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md`

- [ ] **Step 1: 更新根规范并保持逐字节一致**

在 `AGENTS.md` 和 `CLAUDE.md` 同步替换当前分支条目：

```markdown
- 当前活动集成分支：`codex/aliyun-db-clone-20260816`。本阶段日常修改只进入该分支；需要并行时可以使用短期 `codex/<task>` 分支，但不得创建 Mac3/ECS 两条长期环境分支。
- `master` 已冻结在 `2b62a2e9ae7c661f4a7f1741b5ff6c351819a4ad`，作为本阶段开始前的备份点；未经用户新的明确授权，不得移动或推送 `master`。
- Mac3 与 ECS 必须接收从同一精确集成提交构建的同一份源码 archive；两端只独立管理 Registry、数据库、调度启用、`current` 与回滚，不允许在 ECS 上保留 Git checkout、执行 `git pull` 或本地修改 release。
- 环境方案范围只由 `BFL_DEPLOYMENT_TARGET` 与 `deploy/scheme_deployment_matrix_v1.json` 表达；不得再通过为不同主机修改 canonical `config.yaml` 的 `status` 制造两个 `scheme_version`。
```

保留“合并/推送 master 必须明确授权”“检查 status”“installed 调度变更单独授权”等既有约束，不重复创建同义条目。修改后执行：

```bash
cmp AGENTS.md CLAUDE.md
```

Expected: exit 0。

- [ ] **Step 2: 将迁移评估更新到 v1.26**

在 `docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md` 增加 `0.0.5 v1.26 单一源码双主机发布决策（2026-08-18）`，内容必须明确：

```markdown
### 0.0.5 v1.26 单一源码双主机发布决策（2026-08-18）

Mac3 与 ECS 不维护两条环境分支。`master@2b62a2e9ae7c661f4a7f1741b5ff6c351819a4ad`
冻结为本阶段备份点，
`codex/aliyun-db-clone-20260816` 是唯一活动集成分支；每个候选提交只生成一个带 SHA-256 的
源码 archive，两端使用相同 bytes，但独立切换 current、Registry、数据库和调度状态。

canonical 源码恢复全部 65 个 config 为 active。ECS 的 56/9 边界改由
`BFL_DEPLOYMENT_TARGET=aliyun-gray` 与 `deploy/scheme_deployment_matrix_v1.json` 在 discovery
单点强制；Mac3 使用 `mac3-production` 并发现 65 个。ECS 已有 9 个 composite Registry 行
继续保持 paused，矩阵不会自动改写 Registry。目标/控制面缺失或错配在 one-shot 运行期副作用前失败。

该代码变更只形成可部署 release，不授权安装服务模板、重载服务、启用 ECS timer、修改 Mac3
installed launchd、切换域名或操作两端数据库。
```

同步修改索引、当前摘要、MIG-017、MIG-019、第 7 节当前结论和更新记录：

- 把当前方案范围描述从“C56 release 的 9 个 config paused”改为“canonical config 65 active，ECS matrix effective discovery 56，ECS Registry 9 paused”。
- 保留 v1.24 及更早的 paused-config 文字作为历史实施事实，但必须显式标注已被 v1.26 的矩阵设计取代，不能继续作为当前实施指令。
- 把文档版本从 1.25 更新为 1.26。
- 记录本轮不改变 ECS timer disabled/inactive、Mac3 生产和域名 No-Go。

- [ ] **Step 3: 扫描当前态矛盾并修正文档**

Run:

```bash
rg -n "config.*paused|paused.*config|C56|P65|master|活动集成分支|scheme_deployment_matrix" \
  AGENTS.md CLAUDE.md deploy/README.md \
  docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md
```

逐条判断输出：历史章节可以保留 paused-config 事实；标为 CURRENT、下一步、当前目标、MIG 当前处置或开工手册的语句必须使用 v1.26 矩阵语义。不得机械全局替换历史证据。

- [ ] **Step 4: 运行文档契约**

Run:

```bash
cmp AGENTS.md CLAUDE.md
git diff --check
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_onboarding_docs.py
```

Expected: `cmp` exit 0，`git diff --check` 无输出，`10 passed, 5 subtests passed` 或更多通过项且 0 failure。

- [ ] **Step 5: 提交当前态文档**

```bash
git status --short
git add AGENTS.md CLAUDE.md deploy/README.md \
  docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md
git commit -m "docs(ops): adopt single-source dual-host releases"
```

如果 `deploy/README.md` 已在 Task 4 提交，这里只 add 实际仍有 diff 的文件；不得使用 `git add -A`。

### Task 6: 全量验证、止损检查与发布前交接

**Files:**

- Verify only; no production deployment or database writes.

- [ ] **Step 1: 运行目标化回归测试**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_deployment_scope.py \
  tests/test_aliyun_c56_candidate.py \
  tests/test_launchd_prediction_runner.py \
  tests/test_systemd_control_plane.py \
  tests/test_launchd_config_drift_audit.py \
  tests/test_blackbox_v2_discovery.py \
  tests/test_native_executor.py \
  tests/test_blackbox_v2_runner.py \
  tests/test_repository_registry.py \
  tests/test_onboarding_policy.py \
  tests/test_scheme_owner.py \
  tests/test_onboarding_docs.py
```

Expected: exit 0、0 failure。任何失败都先定位，不得通过放宽矩阵覆盖或移除精确 ID 断言绕过。

- [ ] **Step 2: 运行完整测试集**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q
```

Expected: exit 0、0 failure。记录通过数、subtests 数和 warnings 数作为最终证据。

- [ ] **Step 3: 验证三种 discovery 结果**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python - <<'PY'
import os
from scheduler.discovery import discover_schemes

for target in (None, "mac3-production", "aliyun-gray"):
    os.environ.pop("BFL_DEPLOYMENT_TARGET", None)
    if target is not None:
        os.environ["BFL_DEPLOYMENT_TARGET"] = target
    configs = discover_schemes(strict=True)
    print(target or "unscoped", len(configs), sum(len(cfg.tenors) for cfg in configs))
PY
```

Expected:

```text
unscoped 65 69
mac3-production 65 69
aliyun-gray 56 60
```

- [ ] **Step 4: 执行 120 行止损与扩散检查**

Run:

```bash
git diff --numstat af4248535b6be0b0bd1533c324fe1c4aaa9a4877 -- \
  scheduler/deployment_scope.py scheduler/discovery.py \
  scheduler/launchd_prediction_runner.py
git diff --exit-code af4248535b6be0b0bd1533c324fe1c4aaa9a4877 -- \
  scheduler/repository.py scheduler/executor.py backend \
  scheduler/actuals_runner.py scheduler/daily_actuals_updater.py \
  scheduler/weekly_actuals_updater.py scheduler/monthly_actuals_updater.py harness
```

把第一条输出的新增行总和减去删除行总和，必须小于等于 120。第二条必须无输出且 exit 0。若任一条件不满足，停止，不进入 release 构建。

- [ ] **Step 5: 复核 Git 边界**

Run:

```bash
git status --short --branch
git log --oneline --decorate -8
git rev-parse master
git merge-base --is-ancestor master codex/aliyun-db-clone-20260816
git rev-list --left-right --count master...codex/aliyun-db-clone-20260816
```

Expected:

- 开发工作树干净。
- `master` 仍为 `2b62a2e9ae7c661f4a7f1741b5ff6c351819a4ad`。
- master 是开发分支祖先，开发分支只向前新增本计划提交。
- 没有 push、merge master、ECS 部署、Mac3 installed plist 操作或数据库写入。

- [ ] **Step 6: 形成发布前交接，不执行生产动作**

向用户报告：

1. 实现提交序列和最终 exact commit。
2. 完整测试结果与 65/65/56 discovery 证据。
3. 生产 Python 净新增行数。
4. `master` 冻结状态和远端未推送状态。
5. 下一独立授权点是“从 exact commit 构建一次 archive，并只部署 ECS、读回 target/Registry/timers”；该授权不包含 Mac3 部署或 timer enable。

不得在本计划执行阶段自行运行 `git archive`、SSH/SCP、切换 ECS `current`、安装 unit、`systemctl daemon-reload`、修改 Registry、启用 timer、替换 Mac3 plist 或调用 launchctl。

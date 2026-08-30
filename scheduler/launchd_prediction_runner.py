"""由 launchd one-shot 启动的单批预测入口。

本模块不保存 cron、ledger、occurrence 或 startup catch-up 状态。每次进程启动时
只做一次严格发现、按 active cadence 筛选、DataBridge Gate 校验和逐方案执行；跨 cadence 的
互斥由 runtime 目录中的单一阻塞锁保证。
"""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from scheduler.executor import (
    DEFAULT_ALGO_ENV,
    _launchd_scheduled_execution_context,
)
from scheduler.one_shot_prediction_runner import (
    OneShotPredictionConfigurationError,
    OneShotPredictionSummary,
    configuration_summary,
    run_one_shot,
    today,
)
from shared.one_shot_control_plane import LAUNCHD_ONE_SHOT_CONTROL_PLANE
from shared.task_specs import PREDICTION_CADENCES


_LAUNCHD_EVENT = "launchd_prediction_run"

# 保留既有导入名称，调用方无需感知内部平台中立化。
LaunchdPredictionConfigurationError = OneShotPredictionConfigurationError
LaunchdPredictionSummary = OneShotPredictionSummary


def run(
    cadence: str,
    *,
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    scheme_ids: Sequence[str] | None = None,
) -> OneShotPredictionSummary:
    """执行一次由 launchd capability 约束的 scheduled_live 批次。"""
    return run_one_shot(
        cadence,
        predict_date=predict_date,
        algo_env=algo_env,
        scheduled_control_plane=LAUNCHD_ONE_SHOT_CONTROL_PLANE,
        scheduled_execution_context=_launchd_scheduled_execution_context(),
        event=_LAUNCHD_EVENT,
        scheme_ids=scheme_ids,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """解析 launchd one-shot 参数并输出脱敏结构化摘要。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cadence",
        required=True,
        choices=sorted(PREDICTION_CADENCES),
    )
    parser.add_argument("--predict-date", default=today())
    parser.add_argument("--algo-env", default=DEFAULT_ALGO_ENV)
    parser.add_argument("--scheme-id", action="append", dest="scheme_ids")
    args = parser.parse_args(argv)
    try:
        summary = run(
            args.cadence,
            predict_date=args.predict_date,
            algo_env=args.algo_env,
            scheme_ids=args.scheme_ids,
        )
    except OneShotPredictionConfigurationError:
        summary = configuration_summary(
            args.cadence,
            args.predict_date,
            event=_LAUNCHD_EVENT,
        )
    except Exception:  # noqa: BLE001 - 不序列化底层异常
        summary = configuration_summary(
            args.cadence,
            args.predict_date,
            event=_LAUNCHD_EVENT,
        )
    print(
        json.dumps(
            summary.to_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""由 systemd one-shot 启动的单批预测入口。"""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from scheduler.executor import (
    DEFAULT_ALGO_ENV,
    _systemd_scheduled_execution_context,
)
from scheduler.one_shot_prediction_runner import (
    OneShotPredictionConfigurationError,
    OneShotPredictionSummary,
    configuration_summary,
    run_one_shot,
    today,
)
from shared.one_shot_control_plane import SYSTEMD_ONE_SHOT_CONTROL_PLANE
from shared.task_specs import PREDICTION_CADENCES


_SYSTEMD_EVENT = "systemd_prediction_run"


def run(
    cadence: str,
    *,
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    scheme_ids: Sequence[str] | None = None,
) -> OneShotPredictionSummary:
    """执行一次由 systemd capability 约束的 scheduled_live 批次。"""
    return run_one_shot(
        cadence,
        predict_date=predict_date,
        algo_env=algo_env,
        scheduled_control_plane=SYSTEMD_ONE_SHOT_CONTROL_PLANE,
        scheduled_execution_context=(
            _systemd_scheduled_execution_context()
        ),
        event=_SYSTEMD_EVENT,
        scheme_ids=scheme_ids,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """解析 systemd one-shot 参数并输出脱敏结构化摘要。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cadence",
        required=True,
        choices=sorted(PREDICTION_CADENCES),
    )
    parser.add_argument(
        "--predict-date",
        default=today(),
    )
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
            event=_SYSTEMD_EVENT,
        )
    except Exception:  # noqa: BLE001 - 不向 systemd 日志序列化底层异常
        summary = configuration_summary(
            args.cadence,
            args.predict_date,
            event=_SYSTEMD_EVENT,
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

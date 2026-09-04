from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from sqlalchemy.engine import Engine

from backtests import liwei_0616_5y_all_k10_reproduction_common as common
from backtests.repository import clean_json
from shared.calendar_service import get_calendar
from shared.data_service import create_sqlalchemy_engine
from shared.input_artifacts import (
    build_daily_input_artifact,
    build_monthly_input_artifact,
    build_weekly_input_artifact,
)
from schemes.liwei_0616_5y_auc_yearly_all_k3_div_k10.core.v31_common import (
    PROD_CONFIG,
    SCREEN_METRIC,
    SCREEN_REBAL,
    SOURCE_MODEL_ID,
    model_config,
    required_baselines,
    run_prediction,
)
from schemes.liwei_0616_5y_auc_yearly_all_k3_div_k10.inference import (
    CACHE_FAMILY,
    MODEL_VERSION,
    full_oos_window,
    run_for_window_silent,
)


SCHEME_ID = "liwei_0616_5y_auc_yearly_all_k3_div_k10"
BENCHMARK_ID = "liwei_0616_5y_auc_yearly_all_k3_div_k10"
DEFAULT_N_WORKERS = common.DEFAULT_N_WORKERS
DEFAULT_PARALLEL_SHARDS = common.DEFAULT_PARALLEL_SHARDS
DEFAULT_BATCH_MODE = common.DEFAULT_BATCH_MODE


def _variant_spec() -> common.VariantSpec:
    return common.VariantSpec(
        scheme_id=SCHEME_ID,
        benchmark_id=BENCHMARK_ID,
        source_model_id=SOURCE_MODEL_ID,
        model_version=MODEL_VERSION,
        cache_family=CACHE_FAMILY,
        screen_metric=SCREEN_METRIC,
        screen_rebal=SCREEN_REBAL,
        prod_config=PROD_CONFIG,
        model_config=model_config,
        required_baselines=required_baselines,
        run_prediction=run_prediction,
        window_factory=full_oos_window,
        run_window=run_for_window_silent,
        runner_path=Path(__file__),
    )


def _runtime_deps() -> common.RuntimeDeps:
    return common.RuntimeDeps(
        create_engine=create_sqlalchemy_engine,
        get_calendar=get_calendar,
        build_daily=build_daily_input_artifact,
        build_weekly=build_weekly_input_artifact,
        build_monthly=build_monthly_input_artifact,
    )


def run_historical_prediction(**kwargs: Any) -> pd.DataFrame:
    return common.run_historical_prediction(_variant_spec(), **kwargs)


def build_backtest_rows(detail: pd.DataFrame, **kwargs: Any) -> list[dict[str, Any]]:
    return common.build_backtest_rows(_variant_spec(), detail, **kwargs)


def run_liwei_0616_5y_auc_yearly_all_k3_div_k10_reproduction(
    *,
    persist: bool = True,
    n_workers: int = DEFAULT_N_WORKERS,
    engine: Engine | None = None,
    sample_dates: Iterable[str] | None = None,
    parallel_shards: int = 1,
    cache_dir: str | Path | None = None,
    disable_cache: bool = False,
    batch_mode: str = DEFAULT_BATCH_MODE,
    use_phase_a_cache: bool = False,
) -> dict[str, Any]:
    """执行 5Y AUC yearly ALL_K10 DB-aligned 历史回测。"""
    return common.run_reproduction(
        _variant_spec(),
        _runtime_deps(),
        persist=persist,
        n_workers=n_workers,
        engine=engine,
        sample_dates=sample_dates,
        parallel_shards=parallel_shards,
        cache_dir=cache_dir,
        disable_cache=disable_cache,
        batch_mode=batch_mode,
        use_phase_a_cache=use_phase_a_cache,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--n-workers", type=int, default=DEFAULT_N_WORKERS)
    parser.add_argument("--sample-dates")
    parser.add_argument("--parallel-shards", type=int, default=DEFAULT_PARALLEL_SHARDS)
    parser.add_argument("--batch-mode", choices=["daily", "monthly"], default=DEFAULT_BATCH_MODE)
    parser.add_argument("--phase-a-cache", action="store_true")
    parser.add_argument("--cache-dir")
    parser.add_argument("--disable-cache", action="store_true")
    args = parser.parse_args()
    result = run_liwei_0616_5y_auc_yearly_all_k3_div_k10_reproduction(
        persist=not args.no_persist,
        n_workers=args.n_workers,
        sample_dates=common.parse_sample_dates(args.sample_dates),
        parallel_shards=args.parallel_shards,
        batch_mode=args.batch_mode,
        use_phase_a_cache=args.phase_a_cache,
        cache_dir=args.cache_dir,
        disable_cache=args.disable_cache,
    )
    print(json.dumps(clean_json(result), ensure_ascii=False))


if __name__ == "__main__":
    main()

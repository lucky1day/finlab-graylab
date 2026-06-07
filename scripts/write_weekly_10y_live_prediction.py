from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from typing import Callable, Iterable

from sqlalchemy.engine import Engine

from scheduler.executor import DEFAULT_ALGO_ENV, run_scheme_subprocess
from scheduler.repository import create_engine_from_env, upsert_predictions, write_run_log
from shared.models import PredictionRecord

from scripts.check_weekly_10y_readiness import assess_weekly_10y_readiness


SCHEME_ID = "weekly_10y_d_overlay"
TARGET_TENOR = "10Y"
HORIZON_DAYS = 6


@dataclass(frozen=True)
class Weekly10YLiveWriteResult:
    """周度 10Y 受控 live 写库结果。"""

    scheme_id: str
    predict_date: str
    target_date: str
    status: str
    records_written: int
    duration_sec: float

    def to_dict(self) -> dict:
        return asdict(self)


def validate_scheme_id(scheme_id: str) -> None:
    """确保该命令不会被复用于其他方案。"""
    if scheme_id != SCHEME_ID:
        raise ValueError(f"controlled weekly live writer only supports {SCHEME_ID}")


def _validate_records(records: Iterable[PredictionRecord], predict_date: str, target_date: str) -> list[PredictionRecord]:
    materialized = list(records)
    if len(materialized) != 1:
        raise ValueError(f"{SCHEME_ID} must return exactly one live record, got {len(materialized)}")
    record = materialized[0]
    if record.scheme_id != SCHEME_ID:
        raise ValueError(f"unexpected scheme_id={record.scheme_id}")
    if record.target_tenor != TARGET_TENOR:
        raise ValueError(f"unexpected target_tenor={record.target_tenor}")
    if record.horizon != HORIZON_DAYS:
        raise ValueError(f"unexpected horizon={record.horizon}")
    if record.predict_date != predict_date:
        raise ValueError(f"unexpected predict_date={record.predict_date}")
    if record.target_date != target_date:
        raise ValueError(f"unexpected target_date={record.target_date}")
    return materialized


def write_weekly_10y_live_prediction(
    predict_date: str,
    scheme_id: str = SCHEME_ID,
    engine: Engine | None = None,
    algo_env: str = DEFAULT_ALGO_ENV,
    dry_run_func: Callable[..., list[PredictionRecord]] = run_scheme_subprocess,
    upsert_func: Callable[[Engine, Iterable[PredictionRecord]], int] = upsert_predictions,
    run_log_func: Callable[[Engine, str, str, str, float | None, str | None], None] = write_run_log,
) -> Weekly10YLiveWriteResult:
    """先通过 readiness 和 dry-run，再只写入周度 10Y 自己的 live 预测与 run_log。"""
    validate_scheme_id(scheme_id)
    own_engine = engine is None
    engine = engine or create_engine_from_env()
    started = time.monotonic()
    try:
        readiness = assess_weekly_10y_readiness(engine, predict_date)
        if not readiness.ready:
            missing = json.dumps(readiness.missing_required_values, ensure_ascii=False)
            raise RuntimeError(
                "weekly source data is not ready; "
                f"latest_supported_predict_date={readiness.latest_supported_predict_date}; "
                f"missing_required_values={missing}"
            )
        records = dry_run_func(scheme_id, predict_date, algo_env=algo_env)
        validated_records = _validate_records(records, readiness.predict_date, readiness.target_date)
        written = upsert_func(engine, validated_records)
        duration = time.monotonic() - started
        status = "success" if written == len(validated_records) else "partial"
        error_msg = None if status == "success" else f"written={written}, returned={len(validated_records)}"
        run_log_func(engine, scheme_id, predict_date, status, duration, error_msg)
        return Weekly10YLiveWriteResult(
            scheme_id=scheme_id,
            predict_date=readiness.predict_date,
            target_date=readiness.target_date,
            status=status,
            records_written=written,
            duration_sec=duration,
        )
    finally:
        if own_engine:
            engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled live write for weekly_10y_d_overlay.")
    parser.add_argument("--predict-date", required=True, help="Saturday predict date in YYYY-MM-DD format")
    parser.add_argument("--scheme-id", default=SCHEME_ID, help="Must be weekly_10y_d_overlay")
    parser.add_argument("--algo-env", default=DEFAULT_ALGO_ENV)
    args = parser.parse_args()

    result = write_weekly_10y_live_prediction(
        predict_date=args.predict_date,
        scheme_id=args.scheme_id,
        algo_env=args.algo_env,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

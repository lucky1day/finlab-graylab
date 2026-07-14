from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy.engine import Engine

from backtests.repository import clean_json
from backtests.weekly_avg_lgbm_0529_reproduction import run_weekly_avg_lgbm_0529_reproduction
from shared.input_artifacts import build_weekly_input_artifact as _input_artifact_contract


SCHEME_ID = "weekly_avg_1y_lgbm_0529"
_ = _input_artifact_contract


def run_weekly_avg_1y_lgbm_0529_reproduction(
    engine: Engine | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    return run_weekly_avg_lgbm_0529_reproduction(SCHEME_ID, engine=engine, persist=persist)


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Run weekly_avg_1y_lgbm_0529 historical reproduction.")
    parser.add_argument("--no-persist", action="store_true", help="只输出 JSON，不写 t_backtest_*")
    args = parser.parse_args(argv)
    payload = run_weekly_avg_1y_lgbm_0529_reproduction(persist=not args.no_persist)
    print(json.dumps(clean_json(payload), ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    main()

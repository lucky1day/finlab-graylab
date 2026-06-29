from __future__ import annotations

from backtests.monthly_0629_reproduction import main_for_scheme, run_monthly_0629_reproduction
from shared.input_artifacts import build_monthly_input_artifact as _input_artifact_contract


SCHEME_ID = "monthly_1y_rf_top30_0629"
_ = _input_artifact_contract


def run_monthly_1y_rf_top30_0629_reproduction(*, predict_dates=None, engine=None, persist: bool = True):
    return run_monthly_0629_reproduction(SCHEME_ID, predict_dates=predict_dates, engine=engine, persist=persist)


def main(argv: list[str] | None = None):
    return main_for_scheme(SCHEME_ID, argv)


if __name__ == "__main__":
    main()

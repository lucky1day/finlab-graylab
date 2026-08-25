from __future__ import annotations

from backtests.daily_0629_reproduction import main_for_scheme, run_daily_0629_reproduction


SCHEME_ID = "daily_5y_lgbm_5y10_0629"
def run_daily_5y_lgbm_5y10_0629_reproduction(
    *,
    source_run_date=None,
    start_date=None,
    gray_start_date=None,
    engine=None,
    persist: bool = True,
):
    kwargs = {key: value for key, value in {
        "source_run_date": source_run_date,
        "start_date": start_date,
        "gray_start_date": gray_start_date,
    }.items() if value is not None}
    return run_daily_0629_reproduction(SCHEME_ID, engine=engine, persist=persist, **kwargs)


def main(argv: list[str] | None = None):
    return main_for_scheme(SCHEME_ID, argv)


if __name__ == "__main__":
    main()

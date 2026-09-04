from __future__ import annotations

from backtests.daily_0629_reproduction import main_for_scheme


SCHEME_ID = "daily_5y_lgbm_5y10_0629"


def main(argv: list[str] | None = None):
    return main_for_scheme(SCHEME_ID, argv)


if __name__ == "__main__":
    main()

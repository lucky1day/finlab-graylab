from __future__ import annotations

from .run_daily import main_daily_process


def main_update_process(current_date: str | None = None, dry_run: bool = True):
    """Maintenance entry point.

    The new LightGBM workflow trains rolling models during daily prediction, so
    update currently runs the same pipeline in dry-run mode for health checks.
    """
    return main_daily_process(current_date, dry_run=dry_run)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run daily LightGBM maintenance check.")
    parser.add_argument("date", nargs="?", default=None)
    parser.add_argument("--write-db", action="store_true", help="Also write forecast rows to database")
    args = parser.parse_args()
    main_update_process(args.date, dry_run=not args.write_db)

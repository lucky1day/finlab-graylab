from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class _Result:
    lastrowid = 101


class _Connection:
    def __init__(self, store: dict[str, object]) -> None:
        self.store = store

    def execute(self, sql, params=None):
        self.store["sql"] = str(sql)
        self.store["params"] = params
        return _Result()


class _Begin:
    def __init__(self, store: dict[str, object]) -> None:
        self.store = store

    def __enter__(self) -> _Connection:
        return _Connection(self.store)

    def __exit__(self, *_exc_info) -> None:
        return None


class _Engine:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    def begin(self) -> _Begin:
        return _Begin(self.store)


class RepositoryLaunchdOneShotTests(unittest.TestCase):
    def test_launchd_one_shot_daily_scheduled_live_needs_no_ledger_item(self) -> None:
        from scheduler.repository import create_scheme_run

        engine = _Engine()
        with patch.dict(
            os.environ,
            {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
            clear=False,
        ):
            run_id = create_scheme_run(
                engine,
                scheme_id="formal_daily",
                predict_date="2026-08-03",
                prediction_phase="scheduled_live",
                schedule_frequency="daily",
                scheduled_control_plane="launchd_one_shot",
            )

        self.assertEqual(run_id, 101)
        self.assertIsNone(engine.store["params"]["schedule_item_id"])

    def test_unknown_scheduled_control_plane_fails_closed(self) -> None:
        from scheduler.repository import create_scheme_run

        with self.assertRaisesRegex(ValueError, "scheduled_control_plane"):
            create_scheme_run(
                _Engine(),
                scheme_id="formal_daily",
                predict_date="2026-08-03",
                prediction_phase="scheduled_live",
                schedule_frequency="daily",
                scheduled_control_plane="forged_plane",
            )

    def test_direct_weekly_and_monthly_scheduled_live_require_item(self) -> None:
        """无 ledger item 的 scheduled_live 只能是 launchd one-shot。"""
        from scheduler.repository import create_scheme_run

        for frequency in ("weekly", "monthly"):
            with self.subTest(frequency=frequency):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "requires launchd_one_shot",
                ):
                    create_scheme_run(
                        _Engine(),
                        scheme_id=f"direct_{frequency}",
                        predict_date="2026-08-03",
                        prediction_phase="scheduled_live",
                        schedule_frequency=frequency,
                    )


if __name__ == "__main__":
    unittest.main()

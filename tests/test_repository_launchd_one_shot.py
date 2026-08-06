from __future__ import annotations

import unittest


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
    def test_launchd_one_shot_scheduled_live_creates_a_normal_run(self) -> None:
        from scheduler.repository import create_scheme_run

        engine = _Engine()
        run_id = create_scheme_run(
            engine,
            scheme_id="formal_daily",
            predict_date="2026-08-03",
            prediction_phase="scheduled_live",
            scheduled_control_plane="launchd_one_shot",
        )

        self.assertEqual(run_id, 101)
        self.assertNotIn("schedule_item_id", engine.store["params"])
        self.assertNotIn("schedule_item_id", engine.store["sql"])

    def test_unknown_scheduled_control_plane_fails_closed(self) -> None:
        from scheduler.repository import create_scheme_run

        with self.assertRaisesRegex(ValueError, "scheduled_control_plane"):
            create_scheme_run(
                _Engine(),
                scheme_id="formal_daily",
                predict_date="2026-08-03",
                prediction_phase="scheduled_live",
                scheduled_control_plane="forged_plane",
            )

    def test_scheduled_live_requires_launchd_one_shot(self) -> None:
        """自然 scheduled_live 仅允许 launchd one-shot 身份。"""
        from scheduler.repository import create_scheme_run

        with self.assertRaisesRegex(RuntimeError, "requires launchd_one_shot"):
            create_scheme_run(
                _Engine(),
                scheme_id="formal_weekly",
                predict_date="2026-08-03",
                prediction_phase="scheduled_live",
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text


RETIRED_SCHEMES = (
    "weekly_avg_5y_direct_0529",
    "weekly_avg_7y_cross_d_overlay_0529",
    "weekly_avg_10y_d_overlay_0529",
)


class DeleteRetiredWeeklyAverageSchemesTests(unittest.TestCase):
    def test_dry_run_reports_exact_retired_closure_without_deleting(self) -> None:
        from scripts import delete_retired_weekly_average_schemes as cleanup

        engine = _engine_with_retired_schemes()

        summary = cleanup.delete_retired_weekly_average_schemes(
            engine,
            apply=False,
        )

        self.assertFalse(summary["applied"])
        self.assertFalse(summary["already_absent"])
        self.assertEqual(
            summary["counts"],
            {
                "t_backtest_monthly_metrics": 3,
                "t_backtest_predictions": 3,
                "t_backtest_runs": 3,
                "t_harness_gate_results": 3,
                "t_harness_runs": 3,
                "t_scheme_predictions": 3,
                "t_scheme_registry": 3,
                "t_scheme_run_log": 3,
                "t_scheme_runs": 3,
                "t_scheme_versions": 3,
            },
        )
        self.assertEqual(_count(engine, "t_scheme_registry"), 4)
        self.assertEqual(_count(engine, "t_scheme_predictions"), 4)

    def test_apply_deletes_only_retired_closure_and_is_idempotent(self) -> None:
        from scripts import delete_retired_weekly_average_schemes as cleanup

        engine = _engine_with_retired_schemes()

        first = cleanup.delete_retired_weekly_average_schemes(
            engine,
            apply=True,
        )
        second = cleanup.delete_retired_weekly_average_schemes(
            engine,
            apply=True,
        )

        self.assertTrue(first["applied"])
        self.assertFalse(first["already_absent"])
        self.assertTrue(second["applied"])
        self.assertTrue(second["already_absent"])
        for table in (
            "t_backtest_monthly_metrics",
            "t_backtest_predictions",
            "t_backtest_runs",
            "t_harness_gate_results",
            "t_harness_runs",
            "t_scheme_predictions",
            "t_scheme_registry",
            "t_scheme_run_log",
            "t_scheme_runs",
            "t_scheme_versions",
        ):
            with self.subTest(table=table):
                self.assertEqual(_count(engine, table), 1)
        with engine.connect() as conn:
            remaining = conn.execute(
                text("SELECT base_scheme_id FROM t_scheme_registry")
            ).scalar_one()
        self.assertEqual(remaining, "weekly_avg_1y_lgbm_0529")

    def test_rejects_non_paused_retired_registry_identity(self) -> None:
        from scripts import delete_retired_weekly_average_schemes as cleanup

        engine = _engine_with_retired_schemes()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE t_scheme_registry
                    SET status = 'active'
                    WHERE base_scheme_id = 'weekly_avg_5y_direct_0529'
                    """
                )
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "retired Registry identities must be exact paused weekly_average rows",
        ):
            cleanup.delete_retired_weekly_average_schemes(engine, apply=False)

    def test_rejects_schedule_ledger_reference(self) -> None:
        from scripts import delete_retired_weekly_average_schemes as cleanup

        engine = _engine_with_retired_schemes()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_schedule_items (item_id, base_scheme_id)
                    VALUES ('item-1', 'weekly_avg_10y_d_overlay_0529')
                    """
                )
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "retired schemes still have protected references",
        ):
            cleanup.delete_retired_weekly_average_schemes(engine, apply=False)


def _engine_with_retired_schemes():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_registry (
                    scheme_id TEXT PRIMARY KEY,
                    base_scheme_id TEXT,
                    runtime_type TEXT,
                    status TEXT,
                    task_type TEXT,
                    target_tenor TEXT,
                    horizon INTEGER
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_versions (
                    id INTEGER PRIMARY KEY,
                    scheme_id TEXT,
                    status TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_harness_runs (
                    harness_run_id TEXT PRIMARY KEY,
                    scheme_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_harness_gate_results (
                    id INTEGER PRIMARY KEY,
                    harness_run_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_runs (
                    run_id INTEGER PRIMARY KEY,
                    scheme_id TEXT,
                    harness_run_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_run_log (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER,
                    scheme_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_predictions (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER,
                    scheme_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_runs (
                    id INTEGER PRIMARY KEY,
                    scheme_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_predictions (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER,
                    scheme_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_monthly_metrics (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER,
                    scheme_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_schedule_items (
                    item_id TEXT PRIMARY KEY,
                    base_scheme_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_schedule_item_targets (
                    target_id TEXT PRIMARY KEY,
                    base_scheme_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_serving_pointer (
                    scheme_id TEXT PRIMARY KEY
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_input_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    scheme_id TEXT
                )
                """
            )
        )

        registry_rows = [
            (
                f"{scheme_id}__h6__{tenor}",
                scheme_id,
                "native_adapter",
                "paused",
                "weekly_average",
                tenor,
                6,
            )
            for scheme_id, tenor in zip(
                RETIRED_SCHEMES,
                ("5Y", "7Y", "10Y"),
                strict=True,
            )
        ]
        registry_rows.append(
            (
                "weekly_avg_1y_lgbm_0529__h6__1Y",
                "weekly_avg_1y_lgbm_0529",
                "native_adapter",
                "active",
                "weekly_average",
                "1Y",
                6,
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, base_scheme_id, runtime_type, status,
                     task_type, target_tenor, horizon)
                VALUES
                    (:scheme_id, :base_scheme_id, :runtime_type, :status,
                     :task_type, :target_tenor, :horizon)
                """
            ),
            [
                {
                    "scheme_id": row[0],
                    "base_scheme_id": row[1],
                    "runtime_type": row[2],
                    "status": row[3],
                    "task_type": row[4],
                    "target_tenor": row[5],
                    "horizon": row[6],
                }
                for row in registry_rows
            ],
        )
        for index, scheme_id in enumerate(
            (*RETIRED_SCHEMES, "weekly_avg_1y_lgbm_0529"),
            start=1,
        ):
            harness_run_id = f"hr-{index}"
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_versions (id, scheme_id, status)
                    VALUES (:id, :scheme_id, :status)
                    """
                ),
                {
                    "id": index,
                    "scheme_id": scheme_id,
                    "status": "paused" if scheme_id in RETIRED_SCHEMES else "active",
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_harness_runs (harness_run_id, scheme_id)
                    VALUES (:harness_run_id, :scheme_id)
                    """
                ),
                {"harness_run_id": harness_run_id, "scheme_id": scheme_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_harness_gate_results (id, harness_run_id)
                    VALUES (:id, :harness_run_id)
                    """
                ),
                {"id": index, "harness_run_id": harness_run_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_runs (run_id, scheme_id, harness_run_id)
                    VALUES (:run_id, :scheme_id, :harness_run_id)
                    """
                ),
                {
                    "run_id": index,
                    "scheme_id": scheme_id,
                    "harness_run_id": harness_run_id,
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_run_log (id, run_id, scheme_id)
                    VALUES (:id, :run_id, :scheme_id)
                    """
                ),
                {"id": index, "run_id": index, "scheme_id": scheme_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions (id, run_id, scheme_id)
                    VALUES (:id, :run_id, :scheme_id)
                    """
                ),
                {"id": index, "run_id": index, "scheme_id": scheme_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs (id, scheme_id)
                    VALUES (:id, :scheme_id)
                    """
                ),
                {"id": index, "scheme_id": scheme_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (id, run_id, scheme_id)
                    VALUES (:id, :run_id, :scheme_id)
                    """
                ),
                {"id": index, "run_id": index, "scheme_id": scheme_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_monthly_metrics
                        (id, run_id, scheme_id)
                    VALUES (:id, :run_id, :scheme_id)
                    """
                ),
                {"id": index, "run_id": index, "scheme_id": scheme_id},
            )
    return engine


def _count(engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())

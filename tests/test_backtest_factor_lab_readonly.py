from __future__ import annotations

import inspect
import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text


def _create_minimal_factor_lab_backtest_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_target_registry (
                    target_code TEXT,
                    display_name TEXT,
                    asset_class TEXT,
                    target_type TEXT,
                    sort_order INTEGER,
                    status TEXT,
                    extra TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_registry (
                    scheme_id TEXT,
                    base_scheme_id TEXT,
                    runtime_type TEXT DEFAULT 'native_adapter',
                    name TEXT,
                    description TEXT,
                    horizon INTEGER,
                    task_type TEXT DEFAULT 'T+5',
                    frequency TEXT,
                    target_tenor TEXT,
                    schedule_cron TEXT,
                    schedule_timezone TEXT,
                    status TEXT,
                    deployed_at TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_runs (
                    id INTEGER,
                    benchmark_id TEXT,
                    scheme_id TEXT,
                    data_source TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    status TEXT,
                    summary TEXT,
                    report_path TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE VIEW v_latest_backtest_run AS
                SELECT *
                FROM (
                    SELECT r.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY benchmark_id, scheme_id, data_source
                               ORDER BY updated_at DESC, id DESC
                           ) AS rn
                    FROM t_backtest_runs r
                    WHERE status = 'success'
                ) ranked
                WHERE rn = 1
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_predictions (
                    run_id INTEGER,
                    target_tenor TEXT,
                    horizon INTEGER,
                    predict_date TEXT,
                    feature_date TEXT,
                    target_date TEXT,
                    label INTEGER,
                    predicted_direction INTEGER,
                    confidence REAL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_target_registry
                    (target_code, display_name, asset_class, target_type, sort_order,
                     status, extra, created_at, updated_at)
                VALUES
                    ('1Y', '1Y国债活跃', 'bond', 'active_treasury',
                     1, 'active', '{}', NULL, NULL),
                    ('5Y', '5Y国债活跃', 'bond', 'active_treasury',
                     2, 'active', '{}', NULL, NULL),
                    ('10Y', '10Y国债活跃', 'bond', 'active_treasury',
                     4, 'active', '{}', NULL, NULL)
                """
            )
        )


class BacktestFactorLabReadonlyTests(unittest.TestCase):
    def test_factor_lab_history_filters_pre_2025_rows_but_preserves_audit_data(
        self,
    ) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_minimal_factor_lab_backtest_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, runtime_type, name,
                         description, horizon, task_type, frequency,
                         target_tenor, schedule_cron, schedule_timezone,
                         status, deployed_at, created_at, updated_at)
                    VALUES
                        ('monthly_trial__h1__10Y', 'monthly_trial',
                         'blackbox_v2', 'Monthly Trial', 'Blackbox V2', 1,
                         'monthly', 'monthly', '10Y', '0 7 15 * *',
                         'Asia/Shanghai', 'active', '2026-07-20', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date,
                         end_date, status, summary, report_path, created_at,
                         updated_at)
                    VALUES
                        (940, 'bbv2-monthly-trial', 'monthly_trial',
                         'blackbox_v2_current_snapshot_as_of', '2024-12-15',
                         '2025-01-15', 'success', '{}', NULL, NULL,
                         '2026-07-20T10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date,
                         feature_date, target_date, label,
                         predicted_direction, confidence)
                    VALUES
                        (940, '10Y', 1, '2024-12-15', '2024-12-15',
                         '2025-01-15', 1, -1, NULL),
                        (940, '10Y', 1, '2025-01-15', '2025-01-15',
                         '2025-02-15', 1, 1, NULL)
                    """
                )
            )

        result = backtest_factor_lab_results(engine)

        self.assertEqual(len(result["schemes"]), 1)
        self.assertEqual(
            [row["predict_date"] for row in result["schemes"][0]["daily_rows"]],
            ["2025-01-15"],
        )
        with engine.connect() as conn:
            stored = conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM t_backtest_predictions
                    WHERE run_id = 940
                      AND predict_date < '2025-01-01'
                    """
                )
            ).scalar_one()
        self.assertEqual(stored, 1)

    def test_default_factor_lab_returns_only_latest_successful_run_with_provenance(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_minimal_factor_lab_backtest_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, runtime_type, name, description, horizon,
                         task_type, frequency, target_tenor, schedule_cron, schedule_timezone,
                         status, deployed_at, created_at, updated_at)
                    VALUES
                        ('blackbox_trial__h1__10Y', 'blackbox_trial', 'blackbox_v2',
                         'Blackbox Trial', 'Blackbox V2', 1, 'weekly_point', 'weekly', '10Y',
                         '0 7 * * 1', 'Asia/Shanghai', 'active', '2026-07-20', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (930, 'bbv2-blackbox_trial-hr_old', 'blackbox_trial',
                         'blackbox_v2_current_snapshot_as_of', '2026-01-01', '2026-06-30',
                         'success', :old_summary, NULL, NULL, '2026-07-20T10:00:00'),
                        (931, 'bbv2-blackbox_trial-hr_current', 'blackbox_trial',
                         'blackbox_v2_current_snapshot_as_of', '2026-01-01', '2026-06-30',
                         'success', :current_summary, NULL, NULL, '2026-07-20T10:01:00')
                    """
                ),
                {
                    "old_summary": json.dumps(
                        {
                            "scheme_version": "old-version",
                            "data_snapshot_id": "snapshot-old",
                            "harness_run_id": "hr_old",
                        }
                    ),
                    "current_summary": json.dumps(
                        {
                            "scheme_version": "current-version",
                            "data_snapshot_id": "snapshot-current",
                            "harness_run_id": "hr_current",
                        }
                    ),
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (930, '10Y', 1, '2026-06-20', '2026-06-20', '2026-06-27', 1, -1, NULL),
                        (931, '10Y', 1, '2026-06-21', '2026-06-21', '2026-06-28', 1, 1, NULL)
                    """
                )
            )

        result = backtest_factor_lab_results(engine)

        self.assertEqual(len(result["schemes"]), 1)
        current = result["schemes"][0]
        self.assertEqual(current["run_id"], 931)
        self.assertEqual(current["benchmark_id"], "bbv2-blackbox_trial-hr_current")
        self.assertEqual(current["scheme_version"], "current-version")
        self.assertEqual(current["data_snapshot_id"], "snapshot-current")
        self.assertEqual(current["harness_run_id"], "hr_current")

        historical = backtest_factor_lab_results(
            engine,
            benchmark_id="bbv2-blackbox_trial-hr_old",
        )
        self.assertEqual(len(historical["schemes"]), 1)
        self.assertEqual(historical["schemes"][0]["run_id"], 930)

    def test_default_factor_lab_selects_blackbox_data_source_for_active_blackbox(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_minimal_factor_lab_backtest_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, runtime_type, name, description, horizon, task_type,
                         frequency, target_tenor, schedule_cron, schedule_timezone, status,
                         deployed_at, created_at, updated_at)
                    VALUES
                        ('weekly_trial__h1__10Y', 'weekly_trial', 'blackbox_v2', 'Weekly Trial',
                         'Blackbox V2', 1, 'weekly_point', 'weekly', '10Y',
                         '0 7 * * 1', 'Asia/Shanghai', 'active', '2026-07-20', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (901, 'bbv2-weekly', 'weekly_trial',
                         'blackbox_v2_current_snapshot_as_of', '2024-01-05', '2026-07-10',
                         'success', '{}', NULL, NULL, '2026-07-20T10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (901, '10Y', 1, '2026-07-03', '2026-07-03',
                         '2026-07-10', 1, 1, NULL)
                    """
                )
            )

        result = backtest_factor_lab_results(engine)

        self.assertEqual(result["data_source"], "blackbox_v2_current_snapshot_as_of")
        self.assertEqual(len(result["schemes"]), 1)
        self.assertEqual(result["schemes"][0]["scheme_id"], "weekly_trial__h1__10Y")
        self.assertEqual(
            result["schemes"][0]["data_source"],
            "blackbox_v2_current_snapshot_as_of",
        )

    def test_default_factor_lab_native_ignores_stray_blackbox_run(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_minimal_factor_lab_backtest_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, runtime_type, name, description, horizon,
                         task_type, frequency, target_tenor, schedule_cron, schedule_timezone,
                         status, deployed_at, created_at, updated_at)
                    VALUES
                        ('native_trial__h1__10Y', 'native_trial', 'native_adapter',
                         'Native Trial', 'Native V1', 1, 'T+1', 'daily', '10Y',
                         '0 7 * * 1-5', 'Asia/Shanghai', 'active', '2026-07-20', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (910, 'mixed-source', 'native_trial', 'framework_db_aligned',
                         '2026-07-01', '2026-07-02', 'success', '{}', NULL, NULL,
                         '2026-07-20T10:00:00'),
                        (911, 'mixed-source', 'native_trial',
                         'blackbox_v2_current_snapshot_as_of', '2026-07-01', '2026-07-02',
                         'success', '{}', NULL, NULL, '2026-07-20T10:01:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (910, '10Y', 1, '2026-07-01', '2026-07-01', '2026-07-02', 1, 1, NULL),
                        (911, '10Y', 1, '2026-07-01', '2026-07-01', '2026-07-02', 1, -1, NULL)
                    """
                )
            )

        result = backtest_factor_lab_results(engine)

        self.assertEqual(result["data_source"], "framework_db_aligned")
        self.assertEqual(len(result["schemes"]), 1)
        self.assertEqual(result["schemes"][0]["run_id"], 910)

    def test_default_factor_lab_blackbox_ignores_stray_native_run(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_minimal_factor_lab_backtest_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, runtime_type, name, description, horizon,
                         task_type, frequency, target_tenor, schedule_cron, schedule_timezone,
                         status, deployed_at, created_at, updated_at)
                    VALUES
                        ('blackbox_trial__h1__10Y', 'blackbox_trial', 'blackbox_v2',
                         'Blackbox Trial', 'Blackbox V2', 1, 'T+1', 'daily', '10Y',
                         '0 7 * * 1-5', 'Asia/Shanghai', 'active', '2026-07-20', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (920, 'mixed-source', 'blackbox_trial', 'framework_db_aligned',
                         '2026-07-01', '2026-07-02', 'success', '{}', NULL, NULL,
                         '2026-07-20T10:01:00'),
                        (921, 'mixed-source', 'blackbox_trial',
                         'blackbox_v2_current_snapshot_as_of', '2026-07-01', '2026-07-02',
                         'success', '{}', NULL, NULL, '2026-07-20T10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (920, '10Y', 1, '2026-07-01', '2026-07-01', '2026-07-02', 1, -1, NULL),
                        (921, '10Y', 1, '2026-07-01', '2026-07-01', '2026-07-02', 1, 1, NULL)
                    """
                )
            )

        result = backtest_factor_lab_results(engine)

        self.assertEqual(result["data_source"], "blackbox_v2_current_snapshot_as_of")
        self.assertEqual(len(result["schemes"]), 1)
        self.assertEqual(result["schemes"][0]["run_id"], 921)

    def test_explicit_native_data_source_does_not_include_blackbox_runs(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_minimal_factor_lab_backtest_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, task_type,
                         frequency, target_tenor, schedule_cron, schedule_timezone, status,
                         deployed_at, created_at, updated_at)
                    VALUES
                        ('weekly_trial__h1__10Y', 'weekly_trial', 'Weekly Trial',
                         'Blackbox V2', 1, 'weekly_point', 'weekly', '10Y',
                         '0 7 * * 1', 'Asia/Shanghai', 'active', '2026-07-20', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (901, 'bbv2-weekly', 'weekly_trial',
                         'blackbox_v2_current_snapshot_as_of', '2024-01-05', '2026-07-10',
                         'success', '{}', NULL, NULL, '2026-07-20T10:00:00')
                    """
                )
            )

        result = backtest_factor_lab_results(engine, data_source="framework_db_aligned")

        self.assertEqual(result["data_source"], "framework_db_aligned")
        self.assertEqual(result["schemes"], [])

    def test_factor_lab_query_reads_canonical_latest_view(self) -> None:
        from backend.services import backtest_factor_lab_results

        source = inspect.getsource(backtest_factor_lab_results)
        self.assertIn("FROM v_latest_backtest_run", source)
        self.assertNotIn("FROM t_backtest_runs", source)

    def test_factor_lab_does_not_require_backtest_monthly_metrics_table(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_target_registry (
                        target_code TEXT,
                        display_name TEXT,
                        asset_class TEXT,
                        target_type TEXT,
                        sort_order INTEGER,
                        status TEXT,
                        extra TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_registry (
                        scheme_id TEXT,
                        base_scheme_id TEXT,
                        runtime_type TEXT DEFAULT 'native_adapter',
                        name TEXT,
                        description TEXT,
                        horizon INTEGER,
                        task_type TEXT DEFAULT 'T+5',
                        frequency TEXT,
                        target_tenor TEXT,
                        schedule_cron TEXT,
                        schedule_timezone TEXT,
                        status TEXT,
                        deployed_at TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_runs (
                        id INTEGER,
                        benchmark_id TEXT,
                        scheme_id TEXT,
                        data_source TEXT,
                        start_date TEXT,
                        end_date TEXT,
                        status TEXT,
                        summary TEXT,
                        report_path TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE VIEW v_latest_backtest_run AS
                    SELECT *
                    FROM (
                        SELECT r.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY benchmark_id, scheme_id, data_source
                                   ORDER BY updated_at DESC, id DESC
                               ) AS rn
                        FROM t_backtest_runs r
                        WHERE status = 'success'
                    ) ranked
                    WHERE rn = 1
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_predictions (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        predict_date TEXT,
                        feature_date TEXT,
                        target_date TEXT,
                        label INTEGER,
                        predicted_direction INTEGER,
                        confidence REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_target_registry
                        (target_code, display_name, asset_class, target_type, sort_order,
                         status, extra, created_at, updated_at)
                    VALUES
                        ('5Y', '5Y国债活跃', 'bond', 'active_treasury',
                         2, 'active', '{}', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, frequency,
                         target_tenor, schedule_cron, schedule_timezone, status,
                         deployed_at, created_at, updated_at)
                    VALUES
                        ('demo_daily__h5__5Y', 'demo_daily', 'demo_daily',
                         '只读回测方案', 5, 'daily', '5Y', '3 7 * * 1-5',
                         'Asia/Shanghai', 'active', '2026-06-05',
                         '2026-06-05T00:00:00', '2026-06-05T00:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (288, 'demo_benchmark', 'demo_daily',
                         'framework_db_aligned', '2026-06-01', '2026-06-30',
                         'success', '{}', NULL, NULL, '2026-06-10T10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (288, '5Y', 5, '2026-06-01', '2026-06-01',
                         '2026-06-08', 1, 1, 0.6)
                    """
                )
            )

        result = backtest_factor_lab_results(engine, benchmark_id="demo_benchmark")

        self.assertEqual(result["schemes"][0]["scheme_id"], "demo_daily__h5__5Y")
        self.assertEqual(result["schemes"][0]["monthly_metrics"][0]["correct"], 1)

    def test_default_factor_lab_includes_monthly_framework_db_aligned_backtests(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_minimal_factor_lab_backtest_schema(engine)
        monthly_schemes = [
            ("monthly_1y_rf_top30_0629", "monthly_1y_rf_top30_0629__h30__1Y", "1Y", 301),
            ("monthly_5y_knn_top20_0629", "monthly_5y_knn_top20_0629__h30__5Y", "5Y", 302),
            ("monthly_10y_rf_top5_0629", "monthly_10y_rf_top5_0629__h30__10Y", "10Y", 303),
        ]
        with engine.begin() as conn:
            for base_scheme_id, registry_id, tenor, run_id in monthly_schemes:
                conn.execute(
                    text(
                        """
                        INSERT INTO t_scheme_registry
                            (scheme_id, base_scheme_id, name, description, horizon, task_type,
                             frequency, target_tenor, schedule_cron, schedule_timezone, status,
                             deployed_at, created_at, updated_at)
                        VALUES
                            (:registry_id, :base_scheme_id, :base_scheme_id,
                             '月度默认矩阵可见性测试', 30, 'monthly', 'monthly', :tenor,
                             '0 18 15 * *', 'Asia/Shanghai', 'active',
                             '2026-06-30', NULL, NULL)
                        """
                    ),
                    {"registry_id": registry_id, "base_scheme_id": base_scheme_id, "tenor": tenor},
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO t_backtest_runs
                            (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                             status, summary, report_path, created_at, updated_at)
                        VALUES
                            (:source_run_id, 'monthly_0629', :base_scheme_id,
                             'source_original_monthly_binary_runner', '2026-04-15',
                             '2026-05-15', 'success', '{}', NULL, NULL,
                             '2026-06-29T10:00:00'),
                            (:framework_run_id, 'monthly_0629', :base_scheme_id,
                             'framework_db_aligned', '2026-04-15', '2026-05-15',
                             'success', '{}', NULL, NULL, '2026-06-30T10:00:00')
                        """
                    ),
                    {
                        "source_run_id": run_id - 100,
                        "framework_run_id": run_id,
                        "base_scheme_id": base_scheme_id,
                    },
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO t_backtest_predictions
                            (run_id, target_tenor, horizon, predict_date, feature_date,
                             target_date, label, predicted_direction, confidence)
                        VALUES
                            (:run_id, :tenor, 30, '2026-04-15', '2026-04-15',
                             '2026-05-15', -1, -1, 0.8)
                        """
                    ),
                    {"run_id": run_id, "tenor": tenor},
                )

        result = backtest_factor_lab_results(engine)

        by_scheme = {scheme["scheme_id"]: scheme for scheme in result["schemes"]}
        self.assertEqual(result["data_source"], "framework_db_aligned")
        for _base_scheme_id, registry_id, _tenor, run_id in monthly_schemes:
            with self.subTest(registry_id=registry_id):
                scheme = by_scheme[registry_id]
                self.assertEqual(scheme["run_id"], run_id)
                self.assertEqual(scheme["data_source"], "framework_db_aligned")
                self.assertEqual(scheme["task_type"], "monthly")
                self.assertEqual(scheme["monthly_metrics"][0]["month"], "2026-05")
                self.assertEqual(scheme["monthly_metrics"][0]["overall"], 100.0)
                self.assertEqual(scheme["summary"]["overall"], 100.0)

    def test_factor_lab_fallback_targets_do_not_filter_out_active_1y_registry_row(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_registry (
                        scheme_id TEXT,
                        base_scheme_id TEXT,
                        runtime_type TEXT DEFAULT 'native_adapter',
                        name TEXT,
                        description TEXT,
                        horizon INTEGER,
                        task_type TEXT DEFAULT 'T+1',
                        frequency TEXT,
                        target_tenor TEXT,
                        schedule_cron TEXT,
                        schedule_timezone TEXT,
                        status TEXT,
                        deployed_at TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_runs (
                        id INTEGER,
                        benchmark_id TEXT,
                        scheme_id TEXT,
                        data_source TEXT,
                        start_date TEXT,
                        end_date TEXT,
                        status TEXT,
                        summary TEXT,
                        report_path TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE VIEW v_latest_backtest_run AS
                    SELECT *
                    FROM (
                        SELECT r.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY benchmark_id, scheme_id, data_source
                                   ORDER BY updated_at DESC, id DESC
                               ) AS rn
                        FROM t_backtest_runs r
                        WHERE status = 'success'
                    ) ranked
                    WHERE rn = 1
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_predictions (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        predict_date TEXT,
                        feature_date TEXT,
                        target_date TEXT,
                        label INTEGER,
                        predicted_direction INTEGER,
                        confidence REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, task_type,
                         frequency, target_tenor, schedule_cron, schedule_timezone, status,
                         deployed_at, created_at, updated_at)
                    VALUES
                        ('demo_1y__h1__1Y', 'demo_1y', 'demo_1y',
                         '1Y 可见性测试', 1, 'T+1', 'daily', '1Y',
                         '3 7 * * 1-5', 'Asia/Shanghai', 'active',
                         '2026-06-05', '2026-06-05T00:00:00',
                         '2026-06-05T00:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (528, 'demo_benchmark', 'demo_1y',
                         'framework_db_aligned', '2026-06-01', '2026-06-30',
                         'success', '{}', NULL, NULL, '2026-06-10T10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (528, '1Y', 1, '2026-06-01', '2026-06-01',
                         '2026-06-02', 1, 1, 0.7)
                    """
                )
            )

        result = backtest_factor_lab_results(engine, benchmark_id="demo_benchmark")

        self.assertEqual(result["target_labels"]["1Y"], "1Y国债活跃")
        self.assertEqual([item["target_tenor"] for item in result["schemes"]], ["1Y"])
        self.assertEqual(result["schemes"][0]["scheme_id"], "demo_1y__h1__1Y")

    def test_factor_lab_fails_closed_when_latest_run_has_no_prediction_details(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_target_registry (
                        target_code TEXT,
                        display_name TEXT,
                        asset_class TEXT,
                        target_type TEXT,
                        sort_order INTEGER,
                        status TEXT,
                        extra TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_registry (
                        scheme_id TEXT,
                        base_scheme_id TEXT,
                        runtime_type TEXT DEFAULT 'native_adapter',
                        name TEXT,
                        description TEXT,
                        horizon INTEGER,
                        task_type TEXT DEFAULT 'T+5',
                        frequency TEXT,
                        target_tenor TEXT,
                        schedule_cron TEXT,
                        schedule_timezone TEXT,
                        status TEXT,
                        deployed_at TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_runs (
                        id INTEGER,
                        benchmark_id TEXT,
                        scheme_id TEXT,
                        data_source TEXT,
                        start_date TEXT,
                        end_date TEXT,
                        status TEXT,
                        summary TEXT,
                        report_path TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE VIEW v_latest_backtest_run AS
                    SELECT *
                    FROM (
                        SELECT r.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY benchmark_id, scheme_id, data_source
                                   ORDER BY updated_at DESC, id DESC
                               ) AS rn
                        FROM t_backtest_runs r
                        WHERE status = 'success'
                    ) ranked
                    WHERE rn = 1
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_monthly_metrics (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        month TEXT,
                        sample_count INTEGER,
                        correct_count INTEGER,
                        accuracy REAL,
                        up_precision REAL,
                        up_recall REAL,
                        down_precision REAL,
                        down_recall REAL,
                        actual_dist TEXT,
                        predicted_dist TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_predictions (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        predict_date TEXT,
                        feature_date TEXT,
                        target_date TEXT,
                        label INTEGER,
                        predicted_direction INTEGER,
                        confidence REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_target_registry
                        (target_code, display_name, asset_class, target_type, sort_order,
                         status, extra, created_at, updated_at)
                    VALUES
                        ('5Y', '5Y国债活跃', 'bond', 'active_treasury',
                         2, 'active', '{}', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, frequency,
                         target_tenor, schedule_cron, schedule_timezone, status,
                         deployed_at, created_at, updated_at)
                    VALUES
                        ('demo_daily__h5__5Y', 'demo_daily', 'demo_daily',
                         '只读回测方案', 5, 'daily', '5Y', '3 7 * * 1-5',
                         'Asia/Shanghai', 'active', '2026-06-05',
                         '2026-06-05T00:00:00', '2026-06-05T00:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (388, 'demo_benchmark', 'demo_daily',
                         'framework_db_aligned', '2026-06-01', '2026-06-30',
                         'success', '{}', NULL, NULL, '2026-06-10T10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_monthly_metrics
                        (run_id, target_tenor, horizon, month, sample_count, correct_count,
                         accuracy, up_precision, up_recall, down_precision, down_recall,
                         actual_dist, predicted_dist)
                    VALUES
                        (388, '5Y', 5, '2026-06', 8, 8,
                         1.0, 1.0, 1.0, 1.0, 1.0,
                         :dist, :dist)
                    """
                ),
                {"dist": '{"up":4,"down":4,"flat":0}'},
            )

        with self.assertRaisesRegex(ValueError, "has no backtest prediction details"):
            backtest_factor_lab_results(engine, benchmark_id="demo_benchmark")

    def test_factor_lab_fails_closed_when_active_registry_row_missing_deployed_at(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_minimal_factor_lab_backtest_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, frequency,
                         target_tenor, schedule_cron, schedule_timezone, status,
                         deployed_at, created_at, updated_at)
                    VALUES
                        ('demo_daily__h5__5Y', 'demo_daily', 'demo_daily',
                         '只读回测方案', 5, 'daily', '5Y', '3 7 * * 1-5',
                         'Asia/Shanghai', 'active', NULL,
                         '2026-06-05T00:00:00', '2026-06-05T00:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (489, 'demo_benchmark', 'demo_daily',
                         'framework_db_aligned', '2026-06-01', '2026-06-30',
                         'success', '{}', NULL, NULL, '2026-06-10T10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (489, '5Y', 5, '2026-06-01', '2026-06-01',
                         '2026-06-08', 1, 1, 0.6)
                    """
                )
            )

        with self.assertRaisesRegex(ValueError, "missing deployed_at.*demo_daily__h5__5Y"):
            backtest_factor_lab_results(engine, benchmark_id="demo_benchmark")

    def test_factor_lab_fails_closed_when_prediction_detail_missing_target_date(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_minimal_factor_lab_backtest_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, frequency,
                         target_tenor, schedule_cron, schedule_timezone, status,
                         deployed_at, created_at, updated_at)
                    VALUES
                        ('demo_daily__h5__5Y', 'demo_daily', 'demo_daily',
                         '只读回测方案', 5, 'daily', '5Y', '3 7 * * 1-5',
                         'Asia/Shanghai', 'active', '2026-06-05',
                         '2026-06-05T00:00:00', '2026-06-05T00:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (490, 'demo_benchmark', 'demo_daily',
                         'framework_db_aligned', '2026-06-01', '2026-06-30',
                         'success', '{}', NULL, NULL, '2026-06-10T10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (490, '5Y', 5, '2026-06-01', '2026-06-01',
                         NULL, 1, 1, 0.6)
                    """
                )
            )

        with self.assertRaisesRegex(ValueError, "missing required backtest prediction fields.*target_date"):
            backtest_factor_lab_results(engine, benchmark_id="demo_benchmark")

    def test_factor_lab_results_do_not_sync_scheme_registry(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_target_registry (
                        target_code TEXT,
                        display_name TEXT,
                        asset_class TEXT,
                        target_type TEXT,
                        sort_order INTEGER,
                        status TEXT,
                        extra TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_registry (
                        scheme_id TEXT,
                        base_scheme_id TEXT,
                        runtime_type TEXT DEFAULT 'native_adapter',
                        name TEXT,
                        description TEXT,
                        horizon INTEGER,
                        task_type TEXT DEFAULT 'T+5',
                        frequency TEXT,
                        target_tenor TEXT,
                        schedule_cron TEXT,
                        schedule_timezone TEXT,
                        status TEXT,
                        deployed_at TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_runs (
                        id INTEGER,
                        benchmark_id TEXT,
                        scheme_id TEXT,
                        data_source TEXT,
                        start_date TEXT,
                        end_date TEXT,
                        status TEXT,
                        summary TEXT,
                        report_path TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE VIEW v_latest_backtest_run AS
                    SELECT *
                    FROM (
                        SELECT r.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY benchmark_id, scheme_id, data_source
                                   ORDER BY updated_at DESC, id DESC
                               ) AS rn
                        FROM t_backtest_runs r
                        WHERE status = 'success'
                    ) ranked
                    WHERE rn = 1
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_monthly_metrics (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        month TEXT,
                        sample_count INTEGER,
                        correct_count INTEGER,
                        accuracy REAL,
                        up_precision REAL,
                        up_recall REAL,
                        down_precision REAL,
                        down_recall REAL,
                        actual_dist TEXT,
                        predicted_dist TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_predictions (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        predict_date TEXT,
                        feature_date TEXT,
                        target_date TEXT,
                        label INTEGER,
                        predicted_direction INTEGER,
                        confidence REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_target_registry
                        (target_code, display_name, asset_class, target_type, sort_order,
                         status, extra, created_at, updated_at)
                    VALUES
                        ('10Y', '10Y国债活跃', 'bond', 'active_treasury', 4,
                         'active', '{}', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, frequency,
                         target_tenor, schedule_cron, schedule_timezone, status, deployed_at,
                         created_at, updated_at)
                    VALUES
                        ('demo_weekly_scheme__h6__10Y', 'demo_weekly_scheme', '周度示例',
                         '只读回测方案', 6, 'weekly', '10Y', '30 11 * * 6',
                         'Asia/Shanghai', 'active', '2026-06-05',
                         '2026-06-05T00:00:00', '2026-06-05T00:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (15, 'model_muti_0529', 'demo_weekly_scheme',
                         'framework_db_aligned', '2025-07-12', '2026-05-09',
                         'success', '{}', NULL, NULL, '2026-06-05T21:32:13')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_monthly_metrics
                        (run_id, target_tenor, horizon, month, sample_count, correct_count,
                         accuracy, up_precision, up_recall, down_precision, down_recall,
                         actual_dist, predicted_dist)
                    VALUES
                        (15, '10Y', 6, '2026-05', 2, 2,
                         1.0, NULL, NULL, 1.0, 1.0,
                         :actual_dist, :predicted_dist)
                    """
                ),
                {
                    "actual_dist": '{"up":0,"down":2,"flat":0}',
                    "predicted_dist": '{"up":0,"down":2,"flat":0}',
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (15, '10Y', 6, '2026-05-09', '2026-05-08',
                         '2026-05-15', -1, -1, 0.32)
                    """
                )
            )

        with patch(
            "backend.services.sync_registry_from_configs",
            side_effect=AssertionError("factor-lab endpoint must not sync registry"),
        ):
            result = backtest_factor_lab_results(engine, benchmark_id="model_muti_0529")

        self.assertEqual(result["schemes"][0]["scheme_id"], "demo_weekly_scheme__h6__10Y")
        self.assertEqual(result["schemes"][0]["base_scheme_id"], "demo_weekly_scheme")
        self.assertEqual(result["schemes"][0]["target_tenor"], "10Y")
        self.assertEqual(result["schemes"][0]["frequency"], "weekly")
        self.assertEqual(result["schemes"][0]["summary"]["accuracy"], 100.0)
        self.assertEqual(result["benchmark_label"], "0529历史基准")
        self.assertEqual(result["data_source_label"], "当前DB对齐回测")
        self.assertEqual(result["schemes"][0]["scheme_name"], "周度示例")
        self.assertEqual(result["schemes"][0]["description"], "只读回测方案")
        self.assertEqual(result["schemes"][0]["data_source_label"], "当前DB对齐回测")
        self.assertEqual(result["schemes"][0]["name"], "周度示例 · 10Y国债活跃")

    def test_factor_lab_recomputes_frontend_metrics_so_flat_predictions_do_not_enter_denominator(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_target_registry (
                        target_code TEXT,
                        display_name TEXT,
                        asset_class TEXT,
                        target_type TEXT,
                        sort_order INTEGER,
                        status TEXT,
                        extra TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_registry (
                        scheme_id TEXT,
                        base_scheme_id TEXT,
                        runtime_type TEXT DEFAULT 'native_adapter',
                        name TEXT,
                        description TEXT,
                        horizon INTEGER,
                        task_type TEXT DEFAULT 'T+5',
                        frequency TEXT,
                        target_tenor TEXT,
                        schedule_cron TEXT,
                        schedule_timezone TEXT,
                        status TEXT,
                        deployed_at TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_runs (
                        id INTEGER,
                        benchmark_id TEXT,
                        scheme_id TEXT,
                        data_source TEXT,
                        start_date TEXT,
                        end_date TEXT,
                        status TEXT,
                        summary TEXT,
                        report_path TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE VIEW v_latest_backtest_run AS
                    SELECT *
                    FROM (
                        SELECT r.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY benchmark_id, scheme_id, data_source
                                   ORDER BY updated_at DESC, id DESC
                               ) AS rn
                        FROM t_backtest_runs r
                        WHERE status = 'success'
                    ) ranked
                    WHERE rn = 1
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_monthly_metrics (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        month TEXT,
                        sample_count INTEGER,
                        correct_count INTEGER,
                        accuracy REAL,
                        up_precision REAL,
                        up_recall REAL,
                        down_precision REAL,
                        down_recall REAL,
                        actual_dist TEXT,
                        predicted_dist TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_predictions (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        predict_date TEXT,
                        feature_date TEXT,
                        target_date TEXT,
                        label INTEGER,
                        predicted_direction INTEGER,
                        confidence REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_target_registry
                        (target_code, display_name, asset_class, target_type, sort_order,
                         status, extra, created_at, updated_at)
                    VALUES
                        ('5Y', '5Y国债活跃', 'bond', 'active_treasury', 2,
                         'active', '{}', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, frequency,
                         target_tenor, schedule_cron, schedule_timezone, status, deployed_at,
                         created_at, updated_at)
                    VALUES
                        ('demo_daily__h5__5Y', 'demo_daily', '日频示例',
                         '只读回测方案', 5, 'daily', '5Y', '3 7 * * 1-5',
                         'Asia/Shanghai', 'active', '2026-06-05',
                         '2026-06-05T00:00:00', '2026-06-05T00:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (88, 'demo_benchmark', 'demo_daily',
                         'framework_db_aligned', '2026-06-01', '2026-06-30',
                         'success', '{}', NULL, NULL, '2026-06-10T10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_monthly_metrics
                        (run_id, target_tenor, horizon, month, sample_count, correct_count,
                         accuracy, up_precision, up_recall, down_precision, down_recall,
                         actual_dist, predicted_dist)
                    VALUES
                        (88, '5Y', 5, '2026-06', 8, 8,
                         1.0, 1.0, 1.0, 1.0, 1.0,
                         :actual_dist, :predicted_dist)
                    """
                ),
                {
                    "actual_dist": '{"up":4,"down":3,"flat":1}',
                    "predicted_dist": '{"up":3,"down":4,"flat":1}',
                },
            )
            for day, label, predicted in [
                (1, 1, 1),
                (2, -1, -1),
                (3, 1, 1),
                (4, -1, 1),
                (5, 1, -1),
                (6, -1, 1),
                (7, 1, -1),
                (8, 0, 0),
            ]:
                conn.execute(
                    text(
                        """
                        INSERT INTO t_backtest_predictions
                            (run_id, target_tenor, horizon, predict_date, feature_date,
                             target_date, label, predicted_direction, confidence)
                        VALUES
                            (88, '5Y', 5, :predict_date, :predict_date,
                             :target_date, :label, :predicted, 0.6)
                        """
                    ),
                    {
                        "predict_date": f"2026-06-{day:02d}",
                        "target_date": f"2026-06-{day + 5:02d}",
                        "label": label,
                        "predicted": predicted,
                    },
                )

        with patch(
            "backend.services.sync_registry_from_configs",
            side_effect=AssertionError("factor-lab endpoint must not sync registry"),
        ):
            result = backtest_factor_lab_results(engine, benchmark_id="demo_benchmark")

        scheme = result["schemes"][0]
        month = scheme["monthly_metrics"][0]
        self.assertEqual(month["samples"], 8)
        self.assertEqual(month["metric_samples"], 7)
        self.assertEqual(month["correct"], 3)
        self.assertEqual(month["accuracy"], 42.9)
        self.assertEqual(scheme["summary"]["metric_samples"], 7)
        self.assertEqual(scheme["summary"]["accuracy"], 42.9)

    def test_factor_lab_ignores_monthly_rows_and_fails_closed_without_details(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_target_registry (
                        target_code TEXT,
                        display_name TEXT,
                        asset_class TEXT,
                        target_type TEXT,
                        sort_order INTEGER,
                        status TEXT,
                        extra TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_registry (
                        scheme_id TEXT,
                        base_scheme_id TEXT,
                        runtime_type TEXT DEFAULT 'native_adapter',
                        name TEXT,
                        description TEXT,
                        horizon INTEGER,
                        task_type TEXT DEFAULT 'T+5',
                        frequency TEXT,
                        target_tenor TEXT,
                        schedule_cron TEXT,
                        schedule_timezone TEXT,
                        status TEXT,
                        deployed_at TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_runs (
                        id INTEGER,
                        benchmark_id TEXT,
                        scheme_id TEXT,
                        data_source TEXT,
                        start_date TEXT,
                        end_date TEXT,
                        status TEXT,
                        summary TEXT,
                        report_path TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE VIEW v_latest_backtest_run AS
                    SELECT *
                    FROM (
                        SELECT r.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY benchmark_id, scheme_id, data_source
                                   ORDER BY updated_at DESC, id DESC
                               ) AS rn
                        FROM t_backtest_runs r
                        WHERE status = 'success'
                    ) ranked
                    WHERE rn = 1
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_monthly_metrics (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        month TEXT,
                        sample_count INTEGER,
                        correct_count INTEGER,
                        accuracy REAL,
                        up_precision REAL,
                        up_recall REAL,
                        down_precision REAL,
                        down_recall REAL,
                        actual_dist TEXT,
                        predicted_dist TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_predictions (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        predict_date TEXT,
                        feature_date TEXT,
                        target_date TEXT,
                        label INTEGER,
                        predicted_direction INTEGER,
                        confidence REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_target_registry
                        (target_code, display_name, asset_class, target_type, sort_order,
                         status, extra, created_at, updated_at)
                    VALUES
                        ('5Y', '5Y国债活跃', 'bond', 'active_treasury',
                         5, 'active', '{}', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, frequency,
                         target_tenor, schedule_cron, schedule_timezone, status,
                         deployed_at, created_at, updated_at)
                    VALUES
                        ('demo_daily__h5__5Y', 'demo_daily', 'demo_daily',
                         '只读回测方案', 5, 'daily', '5Y', '3 7 * * 1-5',
                         'Asia/Shanghai', 'active', '2026-06-05',
                         '2026-06-05T00:00:00', '2026-06-05T00:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (188, 'demo_benchmark', 'demo_daily',
                         'framework_db_aligned', '2026-06-01', '2026-06-30',
                         'success', '{}', NULL, NULL, '2026-06-10T10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_monthly_metrics
                        (run_id, target_tenor, horizon, month, sample_count, correct_count,
                         accuracy, up_precision, up_recall, down_precision, down_recall,
                         actual_dist, predicted_dist)
                    VALUES
                        (188, '5Y', 5, '2026-06', 8, 3,
                         0.375, NULL, NULL, NULL, NULL,
                         '{}', '{}')
                    """
                )
            )

        with self.assertRaisesRegex(ValueError, "has no backtest prediction details"):
            backtest_factor_lab_results(engine, benchmark_id="demo_benchmark")

    def test_factor_lab_default_deduplicates_benchmarks_but_explicit_history_remains_available(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_target_registry (
                        target_code TEXT,
                        display_name TEXT,
                        asset_class TEXT,
                        target_type TEXT,
                        sort_order INTEGER,
                        status TEXT,
                        extra TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_registry (
                        scheme_id TEXT,
                        base_scheme_id TEXT,
                        runtime_type TEXT DEFAULT 'native_adapter',
                        name TEXT,
                        description TEXT,
                        horizon INTEGER,
                        task_type TEXT DEFAULT 'T+5',
                        frequency TEXT,
                        target_tenor TEXT,
                        schedule_cron TEXT,
                        schedule_timezone TEXT,
                        status TEXT,
                        deployed_at TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_runs (
                        id INTEGER,
                        benchmark_id TEXT,
                        scheme_id TEXT,
                        data_source TEXT,
                        start_date TEXT,
                        end_date TEXT,
                        status TEXT,
                        summary TEXT,
                        report_path TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE VIEW v_latest_backtest_run AS
                    SELECT *
                    FROM (
                        SELECT r.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY benchmark_id, scheme_id, data_source
                                   ORDER BY updated_at DESC, id DESC
                               ) AS rn
                        FROM t_backtest_runs r
                        WHERE status = 'success'
                    ) ranked
                    WHERE rn = 1
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_monthly_metrics (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        month TEXT,
                        sample_count INTEGER,
                        correct_count INTEGER,
                        accuracy REAL,
                        up_precision REAL,
                        up_recall REAL,
                        down_precision REAL,
                        down_recall REAL,
                        actual_dist TEXT,
                        predicted_dist TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_predictions (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        predict_date TEXT,
                        feature_date TEXT,
                        target_date TEXT,
                        label INTEGER,
                        predicted_direction INTEGER,
                        confidence REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_target_registry
                        (target_code, display_name, asset_class, target_type, sort_order,
                         status, extra, created_at, updated_at)
                    VALUES
                        ('5Y', '5Y国债活跃', 'bond', 'active_treasury', 2, 'active', '{}', NULL, NULL),
                        ('10Y', '10Y国债活跃', 'bond', 'active_treasury', 4, 'active', '{}', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, frequency,
                         target_tenor, schedule_cron, schedule_timezone, status, deployed_at,
                         created_at, updated_at)
                    VALUES
                        ('legacy_daily__h5__10Y', 'legacy_daily', '旧日频', '默认基准方案', 5,
                         'daily', '10Y', '3 7 * * 1-5', 'Asia/Shanghai', 'archived',
                         '2026-06-01', NULL, NULL),
                        ('daily_5y_2_v28__h5__5Y', 'daily_5y_2_v28', 'V28日频5Y方案2',
                         '新基准方案', 5, 'daily', '5Y', '3 7 * * 1-5',
                         'Asia/Shanghai', 'active', '2026-06-12', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (15, 'model_muti_0529', 'legacy_daily',
                         'framework_db_aligned', '2025-01-01', '2025-05-30',
                         'success', '{}', NULL, NULL, '2026-06-05T21:32:13'),
                        (16, 'unregistered_t1', 't1_daily',
                         'framework_db_aligned', '2025-01-01', '2025-05-30',
                         'success', '{}', NULL, NULL, '2026-06-05T22:32:13'),
                        (92, 'v28_daily_5y_2', 'daily_5y_2_v28',
                         'framework_db_aligned', '2024-07-01', '2026-04-23',
                         'success', '{}', NULL, NULL, '2026-06-12T02:31:03'),
                        (93, 'v28_daily_5y_2_alt', 'daily_5y_2_v28',
                         'framework_db_aligned', '2025-01-01', '2026-05-29',
                         'success', '{}', NULL, NULL, '2026-06-12T03:31:03')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_monthly_metrics
                        (run_id, target_tenor, horizon, month, sample_count, correct_count,
                         accuracy, up_precision, up_recall, down_precision, down_recall,
                         actual_dist, predicted_dist)
                    VALUES
                        (15, '10Y', 5, '2025-05', 2, 1,
                         0.5, NULL, NULL, 1.0, 1.0, :dist, :dist),
                        (16, '10Y', 1, '2025-05', 2, 1,
                         0.5, NULL, NULL, 1.0, 1.0, :dist, :dist),
                        (92, '5Y', 5, '2026-04', 21, 11,
                         0.524, 0.0, 0.0, 0.611, 0.786, :dist, :dist),
                        (93, '5Y', 5, '2026-05', 18, 12,
                         0.524, 0.0, 0.0, 0.611, 0.786, :dist, :dist)
                    """
                ),
                {"dist": '{"up":0,"down":2,"flat":0}'},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (15, '10Y', 5, '2025-05-23', '2025-05-23',
                         '2025-05-30', -1, -1, 0.32),
                        (16, '10Y', 1, '2025-05-23', '2025-05-23',
                         '2025-05-26', -1, -1, 0.32),
                        (92, '5Y', 5, '2026-04-23', '2026-04-23',
                         '2026-04-30', 0, -1, 1.0),
                        (93, '5Y', 5, '2026-05-22', '2026-05-22',
                         '2026-05-29', -1, -1, 1.0)
                    """
                )
            )

        result = backtest_factor_lab_results(engine)

        scheme_ids = {scheme["scheme_id"] for scheme in result["schemes"]}
        self.assertEqual(result["benchmark_id"], "all")
        self.assertNotIn("legacy_daily__h5__10Y", scheme_ids)
        self.assertNotIn("t1_daily__h1__10Y", scheme_ids)
        self.assertIn("daily_5y_2_v28__h5__5Y", scheme_ids)
        v28_runs = [scheme["run_id"] for scheme in result["schemes"] if scheme["scheme_id"] == "daily_5y_2_v28__h5__5Y"]
        self.assertEqual(v28_runs, [93])

        historical = backtest_factor_lab_results(engine, benchmark_id="v28_daily_5y_2")
        self.assertEqual([scheme["run_id"] for scheme in historical["schemes"]], [92])

    def test_factor_lab_uses_canonical_latest_success_run_per_benchmark_scheme_source(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_target_registry (
                        target_code TEXT,
                        display_name TEXT,
                        asset_class TEXT,
                        target_type TEXT,
                        sort_order INTEGER,
                        status TEXT,
                        extra TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_registry (
                        scheme_id TEXT,
                        base_scheme_id TEXT,
                        runtime_type TEXT DEFAULT 'native_adapter',
                        name TEXT,
                        description TEXT,
                        horizon INTEGER,
                        task_type TEXT DEFAULT 'T+5',
                        frequency TEXT,
                        target_tenor TEXT,
                        schedule_cron TEXT,
                        schedule_timezone TEXT,
                        status TEXT,
                        deployed_at TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_runs (
                        id INTEGER,
                        benchmark_id TEXT,
                        scheme_id TEXT,
                        data_source TEXT,
                        start_date TEXT,
                        end_date TEXT,
                        status TEXT,
                        summary TEXT,
                        report_path TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE VIEW v_latest_backtest_run AS
                    SELECT *
                    FROM (
                        SELECT r.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY benchmark_id, scheme_id, data_source
                                   ORDER BY updated_at DESC, id DESC
                               ) AS rn
                        FROM t_backtest_runs r
                        WHERE status = 'success'
                    ) ranked
                    WHERE rn = 1
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_monthly_metrics (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        month TEXT,
                        sample_count INTEGER,
                        correct_count INTEGER,
                        accuracy REAL,
                        up_precision REAL,
                        up_recall REAL,
                        down_precision REAL,
                        down_recall REAL,
                        actual_dist TEXT,
                        predicted_dist TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_predictions (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        predict_date TEXT,
                        feature_date TEXT,
                        target_date TEXT,
                        label INTEGER,
                        predicted_direction INTEGER,
                        confidence REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_target_registry
                        (target_code, display_name, asset_class, target_type, sort_order,
                         status, extra, created_at, updated_at)
                    VALUES
                        ('5Y', '5Y国债活跃', 'bond', 'active_treasury', 2, 'active', '{}', NULL, NULL),
                        ('10Y', '10Y国债活跃', 'bond', 'active_treasury', 4, 'active', '{}', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, frequency,
                         target_tenor, schedule_cron, schedule_timezone, status, deployed_at,
                         created_at, updated_at)
                    VALUES
                        ('daily_5y_2_v28__h5__5Y', 'daily_5y_2_v28',
                         'V28日频5Y方案2', '新基准方案', 5, 'daily', '5Y',
                         '3 7 * * 1-5', 'Asia/Shanghai', 'active',
                         '2026-06-12', NULL, NULL),
                        ('weekly_10y_d_overlay_0529__h6__10Y', 'weekly_10y_d_overlay_0529',
                         '周度10Y方案', '周度方案', 6, 'weekly', '10Y',
                         '30 11 * * 6', 'Asia/Shanghai', 'active',
                         '2026-06-11', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (92, 'v28_daily_5y_2', 'daily_5y_2_v28',
                         'framework_db_aligned', '2024-07-01', '2026-04-23',
                         'success', '{}', NULL, NULL, '2026-06-12T02:31:03'),
                        (93, 'v28_daily_5y_2', 'daily_5y_2_v28',
                         'framework_db_aligned', '2025-01-02', '2026-05-22',
                         'success', '{}', NULL, NULL, '2026-06-12T13:56:12'),
                        (94, 'v28_daily_5y_2', 'daily_5y_2_v28',
                         'framework_db_aligned', '2025-01-02', '2026-05-23',
                         'failed', '{}', NULL, NULL, '2026-06-12T14:56:12'),
                        (90, 'model_muti_0529', 'weekly_10y_d_overlay_0529',
                         'framework_db_aligned', '2025-07-05', '2026-06-06',
                         'success', '{}', NULL, NULL, '2026-06-11T09:00:00'),
                        (91, 'model_muti_0529', 'weekly_10y_d_overlay_0529',
                         'framework_db_aligned', '2025-07-05', '2026-05-23',
                         'success', '{}', NULL, NULL, '2026-06-11T10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_monthly_metrics
                        (run_id, target_tenor, horizon, month, sample_count, correct_count,
                         accuracy, up_precision, up_recall, down_precision, down_recall,
                         actual_dist, predicted_dist)
                    VALUES
                        (92, '5Y', 5, '2026-04', 21, 11, 0.524, NULL, NULL, NULL, NULL, :dist, :dist),
                        (93, '5Y', 5, '2026-05', 13, 10, 0.769, NULL, NULL, NULL, NULL, :dist, :dist),
                        (94, '5Y', 5, '2026-06', 1, 1, 1.0, NULL, NULL, NULL, NULL, :dist, :dist),
                        (90, '10Y', 6, '2026-06', 2, 1, 0.5, NULL, NULL, NULL, NULL, :dist, :dist),
                        (91, '10Y', 6, '2026-05', 4, 3, 0.75, NULL, NULL, NULL, NULL, :dist, :dist)
                    """
                ),
                {"dist": '{"up":0,"down":1,"flat":0}'},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (92, '5Y', 5, '2026-04-23', '2026-04-23', '2026-04-30', -1, -1, 1.0),
                        (93, '5Y', 5, '2026-05-22', '2026-05-22', '2026-05-29', -1, -1, 1.0),
                        (94, '5Y', 5, '2026-05-23', '2026-05-23', '2026-06-01', -1, -1, 1.0),
                        (90, '10Y', 6, '2026-06-06', '2026-06-05', '2026-06-12', -1, -1, 0.4),
                        (91, '10Y', 6, '2026-05-23', '2026-05-22', '2026-05-29', -1, -1, 0.4)
                    """
                )
            )

        v28_result = backtest_factor_lab_results(
            engine,
            benchmark_id="v28_daily_5y_2",
            data_source="framework_db_aligned",
        )
        v28 = v28_result["schemes"][0]
        self.assertEqual(v28["run_id"], 93)
        self.assertEqual(v28["start_date"], "2025-01-02")
        self.assertEqual([row["month"] for row in v28["monthly_metrics"]], ["2026-05"])

        weekly_result = backtest_factor_lab_results(
            engine,
            benchmark_id="model_muti_0529",
            data_source="framework_db_aligned",
        )
        weekly = weekly_result["schemes"][0]
        self.assertEqual(weekly["run_id"], 91)
        self.assertEqual([row["month"] for row in weekly["monthly_metrics"]], ["2026-05"])


if __name__ == "__main__":
    unittest.main()

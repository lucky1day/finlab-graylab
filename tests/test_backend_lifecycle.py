from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from backend.services import schemes_lifecycle


def _setup_lifecycle_db() -> Engine:
    engine = create_engine("sqlite:///:memory:")
    schema = """
    CREATE TABLE t_scheme_registry (
        scheme_id TEXT PRIMARY KEY,
        name TEXT,
        description TEXT,
        horizon INTEGER,
        tenors TEXT,
        frequency TEXT,
        schedule_cron TEXT,
        schedule_timezone TEXT,
        status TEXT
    );
    CREATE TABLE t_scheme_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scheme_id TEXT,
        scheme_version TEXT,
        code_hash TEXT,
        config_hash TEXT,
        manifest_hash TEXT,
        git_commit TEXT,
        status TEXT,
        created_by TEXT,
        created_at TEXT,
        approved_by TEXT,
        approved_at TEXT
    );
    CREATE TABLE t_scheme_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        scheme_id TEXT,
        scheme_version TEXT,
        run_type TEXT,
        predict_date TEXT,
        status TEXT,
        harness_run_id TEXT,
        input_artifact_id TEXT,
        started_at TEXT,
        finished_at TEXT,
        records_expected INTEGER,
        records_returned INTEGER,
        records_written INTEGER,
        error_message TEXT
    );
    CREATE TABLE t_scheme_serving_pointer (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scheme_id TEXT,
        target_tenor TEXT,
        predict_date TEXT,
        serving_run_id INTEGER,
        serving_status TEXT,
        updated_by TEXT,
        updated_at TEXT
    );
    """
    with engine.begin() as conn:
        for statement in schema.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                  (scheme_id, name, description, horizon, tenors, frequency,
                   schedule_cron, schedule_timezone, status)
                VALUES
                  ('alpha_daily', 'Alpha', '', 5, '["5Y"]', 'daily', '', 'Asia/Shanghai', 'active'),
                  ('beta_daily', 'Beta', '', 5, '["10Y"]', 'daily', '', 'Asia/Shanghai', 'paused')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_versions
                  (scheme_id, scheme_version, code_hash, status, created_at, approved_at)
                VALUES
                  ('alpha_daily', 'v1', 'hash1', 'validated', '2026-06-01 09:00:00', NULL),
                  ('alpha_daily', 'v2', 'hash2', 'active', '2026-06-02 09:00:00', '2026-06-02 10:00:00'),
                  ('beta_daily', 'v1', 'hash3', 'paused', '2026-06-01 09:00:00', NULL)
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_runs
                  (run_id, scheme_id, scheme_version, run_type, predict_date, status,
                   started_at, finished_at, records_written, error_message)
                VALUES
                  (1, 'alpha_daily', 'v2', 'active', '2026-06-01', 'success',
                   '2026-06-01 09:20:00', '2026-06-01 09:21:00', 2, NULL),
                  (2, 'alpha_daily', 'v2', 'active', '2026-06-02', 'failed',
                   '2026-06-02 09:20:00', '2026-06-02 09:21:00', 0, 'boom'),
                  (3, 'alpha_daily', 'v2', 'active', '2026-06-03', 'success',
                   '2026-06-03 09:20:00', '2026-06-03 09:21:00', 2, NULL),
                  (4, 'beta_daily', 'v1', 'active', '2026-06-03', 'failed',
                   '2026-06-03 09:20:00', '2026-06-03 09:21:00', 0, 'paused source')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_serving_pointer
                  (scheme_id, target_tenor, predict_date, serving_run_id, serving_status, updated_at)
                VALUES
                  ('alpha_daily', '5Y', '2026-06-03', 3, 'approved', '2026-06-03 09:22:00'),
                  ('alpha_daily', '5Y', '2026-06-02', 2, 'deprecated', '2026-06-02 09:22:00')
                """
            )
        )
    return engine


class SchemesLifecycleServiceTests(unittest.TestCase):
    def test_schemes_lifecycle_returns_version_run_and_prediction_health(self) -> None:
        result = schemes_lifecycle(_setup_lifecycle_db())

        schemes = {item["scheme_id"]: item for item in result["schemes"]}
        self.assertEqual(set(schemes), {"alpha_daily", "beta_daily"})
        alpha = schemes["alpha_daily"]
        self.assertEqual(alpha["version_status"], "active")
        self.assertEqual(alpha["scheme_version"], "v2")
        self.assertEqual(alpha["latest_run"]["status"], "success")
        self.assertEqual(alpha["latest_run"]["records_written"], 2)
        self.assertEqual(alpha["recent_success_rate"], 66.7)
        self.assertEqual(alpha["latest_prediction_date"], "2026-06-03")
        self.assertEqual(alpha["alerts"], [])

        beta = schemes["beta_daily"]
        self.assertEqual(beta["version_status"], "paused")
        self.assertEqual(beta["latest_run"]["status"], "failed")
        self.assertEqual(beta["recent_success_rate"], 0.0)
        self.assertIn("latest_run_failed", beta["alerts"])
        self.assertIn("missing_predictions", beta["alerts"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from backend.services import metrics_compare


def _setup_compare_db() -> Engine:
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
    CREATE TABLE t_scheme_serving_pointer (
        scheme_id TEXT,
        target_tenor TEXT,
        predict_date TEXT,
        serving_run_id TEXT,
        serving_status TEXT
    );
    CREATE TABLE t_scheme_predictions (
        run_id TEXT,
        scheme_id TEXT,
        target_tenor TEXT,
        horizon INTEGER,
        predict_date TEXT,
        target_date TEXT,
        predicted_direction INTEGER,
        confidence REAL,
        model_version TEXT,
        extra TEXT,
        created_at TEXT,
        updated_at TEXT
    );
    CREATE TABLE t_scheme_actuals (
        tenor TEXT,
        trade_date TEXT,
        close_yield REAL,
        direction_1d INTEGER,
        direction_5d INTEGER,
        created_at TEXT,
        updated_at TEXT
    );
    CREATE TABLE t_scheme_weekly_actuals (
        tenor TEXT,
        predict_date TEXT,
        target_date TEXT,
        direction_weekly INTEGER
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
                  ('alpha_daily', 'Alpha', '', 5, '["5Y", "10Y"]', 'daily', '', 'Asia/Shanghai', 'active'),
                  ('beta_daily', 'Beta', '', 5, '["5Y"]', 'daily', '', 'Asia/Shanghai', 'active'),
                  ('weekly_demo', 'Weekly', '', 6, '["5Y"]', 'weekly', '', 'Asia/Shanghai', 'active')
                """
            )
        )
        prediction_rows = [
            ("run-a1", "alpha_daily", "5Y", 5, "2026-06-01", "2026-06-06", 1),
            ("run-a2", "alpha_daily", "5Y", 5, "2026-06-02", "2026-06-07", -1),
            ("run-a3", "alpha_daily", "5Y", 5, "2026-06-03", "2026-06-08", 1),
            ("run-a4", "alpha_daily", "10Y", 5, "2026-06-01", "2026-06-06", -1),
            ("run-b1", "beta_daily", "5Y", 5, "2026-06-01", "2026-06-06", -1),
            ("run-w1", "weekly_demo", "5Y", 6, "2026-06-01", "2026-06-08", 1),
        ]
        for run_id, scheme_id, tenor, horizon, predict_date, target_date, direction in prediction_rows:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                      (run_id, scheme_id, target_tenor, horizon, predict_date, target_date,
                       predicted_direction, confidence, model_version, extra, created_at, updated_at)
                    VALUES
                      (:run_id, :scheme_id, :tenor, :horizon, :predict_date, :target_date,
                       :direction, 0.7, 'v1', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "run_id": run_id,
                    "scheme_id": scheme_id,
                    "tenor": tenor,
                    "horizon": horizon,
                    "predict_date": predict_date,
                    "target_date": target_date,
                    "direction": direction,
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_serving_pointer
                      (scheme_id, target_tenor, predict_date, serving_run_id, serving_status)
                    VALUES (:scheme_id, :tenor, :predict_date, :run_id, 'approved')
                    """
                ),
                {
                    "scheme_id": scheme_id,
                    "tenor": tenor,
                    "predict_date": predict_date,
                    "run_id": run_id,
                },
            )
        actual_rows = [
            ("5Y", "2026-06-06", 1),
            ("5Y", "2026-06-07", -1),
            ("5Y", "2026-06-08", -1),
            ("10Y", "2026-06-06", -1),
        ]
        for tenor, trade_date, direction_5d in actual_rows:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals
                      (tenor, trade_date, close_yield, direction_1d, direction_5d, created_at, updated_at)
                    VALUES (:tenor, :trade_date, 2.0, NULL, :direction_5d, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                ),
                {"tenor": tenor, "trade_date": trade_date, "direction_5d": direction_5d},
            )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_weekly_actuals
                  (tenor, predict_date, target_date, direction_weekly)
                VALUES ('5Y', '2026-06-01', '2026-06-08', 1)
                """
            )
        )
    return engine


class MetricsCompareServiceTests(unittest.TestCase):
    def test_metrics_compare_groups_by_scheme_and_tenor(self) -> None:
          result = metrics_compare(
              _setup_compare_db(),
              frequency="daily",
              start_month="2026-06",
              end_month="2026-06",
              metric="overall",
          )

          self.assertEqual(result["frequency"], "daily")
          self.assertEqual(result["metric"], "overall")
          self.assertEqual(result["tenors"], ["5Y", "10Y"])
          schemes = {item["scheme_id"]: item for item in result["schemes"]}
          self.assertEqual(set(schemes), {"alpha_daily", "beta_daily"})
          self.assertEqual(schemes["alpha_daily"]["cells"]["5Y"]["samples"], 3)
          self.assertEqual(schemes["alpha_daily"]["cells"]["5Y"]["correct"], 2)
          self.assertEqual(schemes["alpha_daily"]["cells"]["5Y"]["value"], 66.7)
          self.assertEqual(schemes["alpha_daily"]["cells"]["10Y"]["value"], 100.0)
          self.assertEqual(schemes["beta_daily"]["cells"]["5Y"]["value"], 0.0)

    def test_metrics_compare_can_filter_weekly_frequency(self) -> None:
          result = metrics_compare(_setup_compare_db(), frequency="weekly", metric="overall")

          self.assertEqual(result["frequency"], "weekly")
          self.assertEqual(result["tenors"], ["5Y"])
          self.assertEqual(len(result["schemes"]), 1)
          self.assertEqual(result["schemes"][0]["scheme_id"], "weekly_demo")
          self.assertEqual(result["schemes"][0]["cells"]["5Y"]["value"], 100.0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest


class _Result:
    lastrowid = 201


class _Connection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def execute(self, sql, params=None) -> _Result:
        self._store.setdefault("calls", []).append((str(sql), params))
        return _Result()


class _Begin:
    def __init__(self, store: dict) -> None:
        self._store = store

    def __enter__(self) -> _Connection:
        return _Connection(self._store)

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _Engine:
    def __init__(self) -> None:
        self.store: dict = {}

    def begin(self) -> _Begin:
        return _Begin(self.store)


class ImmutableBacktestRepositoryTests(unittest.TestCase):
    def test_create_backtest_run_appends_without_legacy_upsert(self) -> None:
        from backtests.repository import create_backtest_run

        engine = _Engine()
        run_id = create_backtest_run(
            engine,
            benchmark_id="demo_benchmark",
            scheme_id="demo_daily",
            data_source="framework_db_aligned",
            start_date="2026-01-01",
            end_date="2026-01-31",
            status="running",
            code_hash="c" * 64,
            config_hash="f" * 64,
            input_artifact_hash="i" * 64,
            run_mode="persist",
        )

        self.assertEqual(run_id, 201)
        insert_sql, params = engine.store["calls"][0]
        all_sql = "\n".join(sql for sql, _ in engine.store["calls"])
        self.assertIn("INSERT INTO t_backtest_runs", insert_sql)
        self.assertNotIn("ON DUPLICATE KEY UPDATE", insert_sql)
        self.assertNotIn("t_scheme_", all_sql)
        self.assertEqual(params["benchmark_id"], "demo_benchmark")
        self.assertEqual(params["code_hash"], "c" * 64)
        self.assertEqual(params["config_hash"], "f" * 64)
        self.assertEqual(params["input_artifact_hash"], "i" * 64)
        self.assertEqual(params["run_mode"], "persist")

    def test_latest_backtest_run_query_reads_latest_view(self) -> None:
        from backtests.repository import latest_backtest_run_id

        class Result:
            def scalar_one_or_none(self):
                return 201

        class Connection(_Connection):
            def execute(self, sql, params=None):
                self._store.setdefault("calls", []).append((str(sql), params))
                return Result()

        class Begin(_Begin):
            def __enter__(self) -> Connection:
                return Connection(self._store)

        class Engine(_Engine):
            def begin(self) -> Begin:
                return Begin(self.store)

        engine = Engine()
        run_id = latest_backtest_run_id(
            engine,
            benchmark_id="demo_benchmark",
            scheme_id="demo_daily",
            data_source="framework_db_aligned",
        )

        sql, params = engine.store["calls"][0]
        self.assertEqual(run_id, 201)
        self.assertIn("v_latest_backtest_run", sql)
        self.assertEqual(params["benchmark_id"], "demo_benchmark")
        self.assertEqual(params["scheme_id"], "demo_daily")
        self.assertNotIn("start_date", sql)
        self.assertNotIn("end_date", sql)

    def test_latest_backtest_run_query_allows_explicit_window_filter(self) -> None:
        from backtests.repository import latest_backtest_run_id

        class Result:
            def scalar_one_or_none(self):
                return None

        class Connection(_Connection):
            def execute(self, sql, params=None):
                self._store.setdefault("calls", []).append((str(sql), params))
                return Result()

        class Begin(_Begin):
            def __enter__(self) -> Connection:
                return Connection(self._store)

        class Engine(_Engine):
            def begin(self) -> Begin:
                return Begin(self.store)

        engine = Engine()
        run_id = latest_backtest_run_id(
            engine,
            benchmark_id="demo_benchmark",
            scheme_id="demo_daily",
            data_source="framework_db_aligned",
            start_date="2025-01-02",
            end_date="2026-05-22",
        )

        sql, params = engine.store["calls"][0]
        self.assertIsNone(run_id)
        self.assertIn("v_latest_backtest_run", sql)
        self.assertIn("start_date = :start_date", sql)
        self.assertIn("end_date = :end_date", sql)
        self.assertEqual(params["start_date"], "2025-01-02")
        self.assertEqual(params["end_date"], "2026-05-22")


if __name__ == "__main__":
    unittest.main()

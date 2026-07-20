from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from sqlalchemy import create_engine, text

from shared.blackbox_v2.contracts import BlackboxMetadata
from shared.blackbox_v2.snapshot import BlackboxSnapshot, CutoffKeys


def _metadata(task_type: str) -> BlackboxMetadata:
    combinations = {
        "T+1": (1, "target_date_yield_vs_feature_date_yield", "daily"),
        "T+5": (5, "target_date_yield_vs_feature_date_yield", "daily"),
        "weekly_point": (1, "target_week_end_yield_vs_feature_week_end_yield", "weekly"),
        "weekly_average": (1, "target_week_average_yield_vs_feature_week_average_yield", "weekly"),
        "monthly": (1, "target_month_observation_yield_vs_feature_month_observation_yield", "monthly"),
    }
    horizon, target_rule, frequency = combinations[task_type]
    return BlackboxMetadata(
        schema_version="1.0",
        scheme_id=f"history_{task_type.lower().replace('+', '_').replace('-', '_')}",
        name=task_type,
        algorithm_version="1.0.0",
        target_tenor="10Y",
        task_type=task_type,
        horizon=horizon,
        target_rule=target_rule,
        frequency=frequency,
    )


def _source_engine(start: str = "2024-01-01", end: str = "2026-07-20"):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    calendar_rows = []
    wind_rows = []
    yield_rows = []
    cursor = start_day
    observation = 0
    while cursor <= end_day:
        is_trading = cursor.weekday() < 5
        iso = cursor.isocalendar()
        week_id = f"{iso.year}{iso.week:02d}"
        calendar_rows.append({"rdate": cursor.isoformat(), "trade_flag": "1" if is_trading else "0"})
        wind_rows.append({"rdate": cursor.isoformat(), "week_id": week_id})
        if is_trading:
            observation += 1
            yield_rows.append(
                {
                    "rdate": cursor.isoformat(),
                    "indicators_code": "TB0YWI0C",
                    "indicators_value": 1.5 + observation / 10000,
                }
            )
        cursor += timedelta(days=1)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t_trade_calendar (rdate TEXT PRIMARY KEY, trade_flag TEXT NOT NULL)"))
        conn.execute(text("CREATE TABLE api_wind_date (rdate TEXT PRIMARY KEY, week_id TEXT NOT NULL)"))
        conn.execute(
            text(
                "CREATE TABLE api_wind_daily "
                "(rdate TEXT NOT NULL, indicators_code TEXT NOT NULL, indicators_value REAL NOT NULL)"
            )
        )
        conn.execute(
            text("INSERT INTO t_trade_calendar (rdate, trade_flag) VALUES (:rdate, :trade_flag)"),
            calendar_rows,
        )
        conn.execute(text("INSERT INTO api_wind_date (rdate, week_id) VALUES (:rdate, :week_id)"), wind_rows)
        conn.execute(
            text(
                "INSERT INTO api_wind_daily (rdate, indicators_code, indicators_value) "
                "VALUES (:rdate, :indicators_code, :indicators_value)"
            ),
            yield_rows,
        )
    return engine


def _snapshot(root: Path, engine) -> BlackboxSnapshot:
    data_dir = root / "snapshot" / "data"
    data_dir.mkdir(parents=True)
    with engine.connect() as conn:
        trading_dates = [str(row[0]) for row in conn.execute(text(
            "SELECT rdate FROM t_trade_calendar WHERE trade_flag='1' ORDER BY rdate"
        )).all()]
        week_ids = [str(row[0]) for row in conn.execute(text(
            "SELECT DISTINCT week_id FROM api_wind_date ORDER BY week_id"
        )).all()]
    month_ids = sorted({value[:7].replace("-", "") for value in trading_dates})
    pd.DataFrame({"date": trading_dates, "x": range(len(trading_dates))}).to_csv(
        data_dir / "daily_output.csv", index=False
    )
    pd.DataFrame({"week_id": week_ids, "x": range(len(week_ids))}).to_csv(
        data_dir / "weekly_output.csv", index=False
    )
    pd.DataFrame({"month_id": month_ids, "x": range(len(month_ids))}).to_csv(
        data_dir / "monthly_output.csv", index=False
    )
    manifest = root / "snapshot" / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    return BlackboxSnapshot(
        snapshot_id="snapshot-history",
        root_dir=root / "snapshot",
        data_dir=data_dir,
        manifest_path=manifest,
        schema_version="data-bridge-v1",
    )


def _cutoffs(snapshot: BlackboxSnapshot, *, feature_date: str, engine) -> CutoffKeys:
    daily = pd.read_csv(snapshot.data_dir / "daily_output.csv", dtype=str)
    weekly = pd.read_csv(snapshot.data_dir / "weekly_output.csv", dtype=str)
    monthly = pd.read_csv(snapshot.data_dir / "monthly_output.csv", dtype=str)
    daily_key = max(value for value in daily["date"] if value <= feature_date)
    with engine.connect() as conn:
        weekly_key = str(
            conn.execute(
                text("SELECT week_id FROM api_wind_date WHERE rdate=:rdate"),
                {"rdate": feature_date},
            ).scalar_one()
        )
    monthly_key = feature_date[:7].replace("-", "")
    if weekly_key not in set(weekly["week_id"]) or monthly_key not in set(monthly["month_id"]):
        raise ValueError("cutoff missing from supplied snapshot")
    return CutoffKeys(daily_key, weekly_key, monthly_key)


def _cutoffs_bulk(snapshot: BlackboxSnapshot, *, feature_dates, engine, **_kwargs):
    return {
        feature_date: _cutoffs(snapshot, feature_date=feature_date, engine=engine)
        for feature_date in feature_dates
    }


class BlackboxV2HistoryTests(unittest.TestCase):
    def _cases(self, task_type: str, *, limit: int = 3, boundary: str = "2026-07-20"):
        from shared.blackbox_v2.history import build_historical_cases

        engine = _source_engine()
        tmpdir = tempfile.TemporaryDirectory()
        snapshot = _snapshot(Path(tmpdir.name), engine)
        patcher = patch(
            "shared.blackbox_v2.history.resolve_blackbox_input_cutoffs_bulk",
            side_effect=_cutoffs_bulk,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(tmpdir.cleanup)
        self.addCleanup(engine.dispose)
        return build_historical_cases(
            _metadata(task_type),
            snapshot,
            engine,
            limit=limit,
            target_date_before=boundary,
            predict_date_from="2024-01-01",
        )

    def test_daily_t1_uses_trading_horizon_and_platform_daily_fact(self) -> None:
        cases = self._cases("T+1")
        for case in cases:
            self.assertEqual(case.request.predict_date, case.request.feature_date)
            self.assertEqual(case.actual_extra["actual_fact_key"][0], "10Y")
            self.assertEqual(case.actual_extra["direction_field"], "direction_1d")
            self.assertIn(case.label, {-1, 0, 1})

    def test_daily_t5_requires_fifth_calendar_trading_day(self) -> None:
        cases = self._cases("T+5")
        self.assertEqual(len(cases), 3)
        for case in cases:
            self.assertEqual(case.actual_extra["direction_field"], "direction_5d")
            self.assertEqual(case.actual_extra["calendar_horizon"], 5)

    def test_weekly_point_uses_exact_calendar_week_ends(self) -> None:
        cases = self._cases("weekly_point")
        for case in cases:
            self.assertEqual(case.request.predict_date, case.request.feature_date)
            self.assertEqual(case.request.feature_date, case.actual_extra["feature_week_end"])
            self.assertEqual(case.request.target_date, case.actual_extra["target_week_end"])
            self.assertEqual(
                case.actual_extra["platform_actual_rule"],
                "next_week_last_trading_day_vs_current_week_last_trading_day",
            )

    def test_weekly_average_maps_contract_rule_to_platform_alias(self) -> None:
        cases = self._cases("weekly_average")
        self.assertTrue(all(
            case.actual_extra["platform_actual_rule"]
            == "next_week_average_yield_vs_current_week_average_yield"
            for case in cases
        ))

    def test_weekly_history_ignores_makeup_weekend_without_bond_observation(self) -> None:
        """通用工作日历的调休周末不能伪造债券周末观测。"""
        from shared.blackbox_v2.history import build_historical_cases

        engine = _source_engine(start="2025-12-01", end="2026-03-01")
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE t_trade_calendar SET trade_flag='1' WHERE rdate='2026-02-08'")
            )
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = _snapshot(Path(tmpdir), engine)
            with patch(
                "shared.blackbox_v2.history.resolve_blackbox_input_cutoffs_bulk",
                side_effect=_cutoffs_bulk,
            ):
                cases = build_historical_cases(
                    _metadata("weekly_point"),
                    snapshot,
                    engine,
                    limit=2,
                    target_date_before="2026-03-01",
                    predict_date_from="2026-01-01",
                )

        self.assertEqual(len(cases), 2)
        self.assertTrue(all(
            date.fromisoformat(case.request.feature_date).weekday() < 5
            for case in cases
        ))
        self.assertTrue(all(
            date.fromisoformat(case.request.target_date).weekday() < 5
            for case in cases
        ))
        engine.dispose()

    def test_monthly_keeps_natural_fifteenth_predict_date(self) -> None:
        cases = self._cases("monthly", limit=6)
        weekend_case = next(case for case in cases if case.request.predict_date != case.request.feature_date)
        self.assertTrue(weekend_case.request.predict_date.endswith("-15"))
        self.assertLess(weekend_case.request.feature_date, weekend_case.request.predict_date)
        self.assertEqual(
            weekend_case.actual_extra["platform_actual_rule"],
            "next_month_observation_yield_vs_feature_month_observation_yield",
        )

    def test_direct_metadata_must_use_fixed_task_contract(self) -> None:
        from shared.blackbox_v2.history import build_historical_cases

        engine = _source_engine(start="2026-01-01", end="2026-03-01")
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = _snapshot(Path(tmpdir), engine)
            invalid_metadata = (
                replace(_metadata("weekly_point"), horizon=5),
                replace(_metadata("weekly_point"), target_rule="free_text_rule"),
                replace(_metadata("weekly_point"), task_type="unsupported_task"),
            )
            for metadata in invalid_metadata:
                with self.subTest(metadata=metadata):
                    with self.assertRaisesRegex(
                        ValueError,
                        "Blackbox historical metadata contract",
                    ):
                        build_historical_cases(
                            metadata,
                            snapshot,
                            engine,
                            limit=1,
                            target_date_before="2026-03-01",
                            predict_date_from="2026-01-01",
                        )
        engine.dispose()

    def test_history_resolves_all_cutoffs_from_one_supplied_snapshot(self) -> None:
        from shared.blackbox_v2.history import build_historical_cases

        engine = _source_engine(start="2025-01-01", end="2026-03-01")
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = _snapshot(Path(tmpdir), engine)
            weekly = pd.read_csv(snapshot.data_dir / "weekly_output.csv", dtype=str)
            monthly = pd.read_csv(snapshot.data_dir / "monthly_output.csv", dtype=str)
            with patch(
                "shared.blackbox_v2.history.resolve_blackbox_input_cutoffs_bulk",
                side_effect=_cutoffs_bulk,
            ):
                for task_type in (
                    "T+1",
                    "T+5",
                    "weekly_point",
                    "weekly_average",
                    "monthly",
                ):
                    with self.subTest(task_type=task_type):
                        cases = build_historical_cases(
                            _metadata(task_type),
                            snapshot,
                            engine,
                            limit=2,
                            target_date_before="2026-03-01",
                            predict_date_from="2025-01-01",
                        )
                        daily_keys = set(pd.read_csv(
                            snapshot.data_dir / "daily_output.csv", dtype=str
                        )["date"])
                        weekly_keys = set(weekly["week_id"])
                        monthly_keys = set(monthly["month_id"])
                        for case in cases:
                            self.assertIn(case.request.daily_cutoff_key, daily_keys)
                            self.assertIn(case.request.weekly_cutoff_key, weekly_keys)
                            self.assertIn(case.request.monthly_cutoff_key, monthly_keys)

                missing_weekly = weekly.iloc[:-8].copy()
                missing_weekly.to_csv(
                    snapshot.data_dir / "weekly_output.csv",
                    index=False,
                )
                with self.assertRaisesRegex(ValueError, "cutoff missing"):
                    build_historical_cases(
                        _metadata("T+1"),
                        snapshot,
                        engine,
                        limit=1,
                        target_date_before="2026-03-01",
                        predict_date_from="2025-01-01",
                    )
        engine.dispose()

    def test_cases_have_exact_limit_unique_identity_and_current_snapshot_disclaimer(self) -> None:
        cases = self._cases("T+1", limit=100)
        self.assertEqual(len(cases), 100)
        self.assertEqual(len({case.request.request_id for case in cases}), 100)
        self.assertEqual(len({case.request.predict_date for case in cases}), 100)
        self.assertEqual(len({case.request.target_date for case in cases}), 100)
        self.assertTrue(all(
            case.actual_extra["replay_semantics"] == "current_snapshot_as_of_not_historical_vintage"
            for case in cases
        ))

    def test_history_resolves_all_selected_feature_dates_in_one_bulk_call(self) -> None:
        from shared.blackbox_v2.history import build_historical_cases

        engine = _source_engine()
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = _snapshot(Path(tmpdir), engine)

            def resolve_once(_snapshot_arg, *, feature_dates, engine, **_kwargs):
                return {
                    feature_date: CutoffKeys(
                        daily_cutoff_key=feature_date,
                        weekly_cutoff_key="202401",
                        monthly_cutoff_key="202401",
                    )
                    for feature_date in feature_dates
                }

            with patch(
                "shared.blackbox_v2.history.resolve_blackbox_input_cutoffs_bulk",
                side_effect=resolve_once,
            ) as resolve_bulk:
                cases = build_historical_cases(
                    _metadata("T+1"),
                    snapshot,
                    engine,
                    limit=100,
                    target_date_before="2026-07-20",
                    predict_date_from="2024-01-01",
                )

        engine.dispose()
        self.assertEqual(len(cases), 100)
        resolve_bulk.assert_called_once()
        self.assertEqual(len(resolve_bulk.call_args.kwargs["feature_dates"]), 100)

    def test_target_boundary_is_strict_and_insufficient_cases_fail_closed(self) -> None:
        from shared.blackbox_v2.history import build_historical_cases

        engine = _source_engine(start="2026-01-01", end="2026-02-01")
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = _snapshot(Path(tmpdir), engine)
            with patch(
                "shared.blackbox_v2.history.resolve_blackbox_input_cutoffs_bulk",
                side_effect=_cutoffs_bulk,
            ):
                with self.assertRaisesRegex(ValueError, "exactly 100"):
                    build_historical_cases(
                        _metadata("T+1"), snapshot, engine,
                        limit=100,
                        target_date_before="2026-02-01",
                        predict_date_from="2026-01-01",
                    )
                cases = build_historical_cases(
                    _metadata("T+1"), snapshot, engine,
                    limit=2,
                    target_date_before="2026-02-01",
                    predict_date_from="2026-01-01",
                )
        engine.dispose()
        self.assertTrue(all(case.request.target_date < "2026-02-01" for case in cases))

    def test_duplicate_source_fact_and_missing_cutoff_fail_closed(self) -> None:
        from shared.blackbox_v2.history import build_historical_cases

        engine = _source_engine(start="2026-01-01", end="2026-03-01")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO api_wind_daily (rdate, indicators_code, indicators_value) "
                    "VALUES ('2026-01-05', 'TB0YWI0C', 9.9)"
                )
            )
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = _snapshot(Path(tmpdir), engine)
            with patch(
                "shared.blackbox_v2.history.resolve_blackbox_input_cutoffs_bulk",
                side_effect=_cutoffs_bulk,
            ):
                with self.assertRaisesRegex(ValueError, "duplicate source actual"):
                    build_historical_cases(
                        _metadata("T+1"), snapshot, engine,
                        limit=2,
                        target_date_before="2026-03-01",
                        predict_date_from="2026-01-01",
                    )
        engine.dispose()

        clean_engine = _source_engine(start="2026-01-01", end="2026-03-01")
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = _snapshot(Path(tmpdir), clean_engine)
            with patch(
                "shared.blackbox_v2.history.resolve_blackbox_input_cutoffs_bulk",
                side_effect=ValueError("missing exact cutoff"),
            ):
                with self.assertRaisesRegex(ValueError, "missing exact cutoff"):
                    build_historical_cases(
                        _metadata("T+1"), snapshot, clean_engine,
                        limit=2,
                        target_date_before="2026-03-01",
                        predict_date_from="2026-01-01",
                    )
        clean_engine.dispose()

    def test_source_gap_and_week_end_fallback_fail_closed(self) -> None:
        from shared.blackbox_v2.history import build_historical_cases

        engine = _source_engine(start="2025-01-01", end="2026-03-01")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM api_wind_daily WHERE rdate IN ('2026-02-18', '2026-02-20')"))
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = _snapshot(Path(tmpdir), engine)
            with patch(
                "shared.blackbox_v2.history.resolve_blackbox_input_cutoffs_bulk",
                side_effect=_cutoffs_bulk,
            ):
                with self.assertRaisesRegex(ValueError, "source gap violates calendar horizon"):
                    build_historical_cases(
                        _metadata("T+5"), snapshot, engine,
                        limit=5, target_date_before="2026-03-01", predict_date_from="2026-02-01",
                    )
                with self.assertRaisesRegex(ValueError, "exact calendar week ends"):
                    build_historical_cases(
                        _metadata("weekly_point"), snapshot, engine,
                        limit=2, target_date_before="2026-03-01", predict_date_from="2026-01-01",
                    )
        engine.dispose()

    def test_missing_latest_actual_source_fact_fails_closed(self) -> None:
        from shared.blackbox_v2.history import build_historical_cases

        for task_type in ("T+1", "monthly"):
            with self.subTest(task_type=task_type):
                engine = _source_engine(start="2024-01-01", end="2026-03-01")
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM api_wind_daily WHERE rdate='2026-02-27'"))
                with tempfile.TemporaryDirectory() as tmpdir:
                    snapshot = _snapshot(Path(tmpdir), engine)
                    with patch(
                        "shared.blackbox_v2.history.resolve_blackbox_input_cutoffs_bulk",
                        side_effect=_cutoffs_bulk,
                    ):
                        with self.assertRaisesRegex(ValueError, "missing platform actual source"):
                            build_historical_cases(
                                _metadata(task_type), snapshot, engine,
                                limit=2,
                                target_date_before="2026-03-01",
                                predict_date_from="2025-01-01",
                            )
                engine.dispose()

    def test_case_set_validator_rejects_duplicate_request_predict_and_target_dates(self) -> None:
        from shared.blackbox_v2.history import validate_historical_cases

        cases = self._cases("T+1", limit=2)
        duplicate_id = [cases[0], replace(cases[1], request=replace(cases[1].request, request_id=cases[0].request.request_id))]
        duplicate_predict = [cases[0], replace(cases[1], request=replace(cases[1].request, predict_date=cases[0].request.predict_date))]
        duplicate_target = [cases[0], replace(cases[1], request=replace(cases[1].request, target_date=cases[0].request.target_date))]
        for invalid in (duplicate_id, duplicate_predict, duplicate_target):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                validate_historical_cases(invalid, expected_count=2)


if __name__ == "__main__":
    unittest.main()

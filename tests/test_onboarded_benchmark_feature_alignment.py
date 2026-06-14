from __future__ import annotations

import csv
import math
import unittest
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from scheduler.repository import create_engine_from_env
from shared.calendar_service import CalendarService


class OnboardedBenchmarkFeatureAlignmentTests(unittest.TestCase):
    def test_all_active_source_benchmarks_align_to_database_feature_date(self) -> None:
        try:
            report = build_alignment_report()
        except SQLAlchemyError as exc:
            self.skipTest(f"database is unavailable for benchmark alignment verification: {exc}")

        self.assertTrue(report["schemes"])
        self.assertEqual(report["summary"]["schemes_checked"], 6)
        self.assertEqual(report["summary"]["duplicate_conflicts"], 0, report["summary"])
        self.assertEqual(report["summary"]["unexpected_database_multiples"], 0, report["summary"])

        conclusions = {scheme["scheme_id"]: scheme["conclusion"] for scheme in report["schemes"]}
        self.assertEqual(conclusions["daily_5y_2_v28"], "FAIL")
        self.assertEqual(conclusions["weekly_5y_direct_0529"], "PASS")
        self.assertEqual(conclusions["weekly_7y_cross_d_overlay_0529"], "PASS")
        self.assertEqual(conclusions["weekly_10y_d_overlay_0529"], "PASS")
        self.assertEqual(conclusions["t5_daily"], "PASS_WITH_LEGACY_SAMPLE_LIMITATIONS")
        self.assertEqual(conclusions["t1_daily"], "PASS_WITH_LEGACY_SAMPLE_LIMITATIONS")


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMES_ROOT = REPO_ROOT / "schemes"
GRAY_TARGET_START = "2026-06-01"
DATA_SOURCE = "framework_db_aligned"
DEFAULT_BENCHMARK_ID = "model_muti_0529"
CONFIDENCE_TOLERANCE = 1e-10


@dataclass(frozen=True)
class SchemeConfig:
    scheme_id: str
    frequency: str
    horizon: int
    tenors: tuple[str, ...]
    benchmark_id: str
    data_source: str
    output_start_date: str


@dataclass(frozen=True)
class BenchmarkPoint:
    row_number: int
    feature_date: str
    target_date: str | None
    tenor: str
    horizon: int
    predicted_direction: int
    confidence: float | None
    label: int | None
    is_correct: bool | None
    feature_week_id: int | None
    route: str

    @property
    def key(self) -> tuple[str, str | None, str, int]:
        return (self.feature_date, self.target_date, self.tenor, self.horizon)


def build_alignment_report() -> dict[str, Any]:
    """只读验证已入库方案 original benchmark 与 DB feature_date 明细是否一致。"""

    engine = create_engine_from_env()
    calendar = CalendarService(engine)
    configs = _load_active_source_configs()
    report: dict[str, Any] = {
        "rule": "source benchmark T/date/predict_date aligns to database feature_date",
        "gray_target_start": GRAY_TARGET_START,
        "schemes": [],
    }

    totals = {
        "schemes_checked": 0,
        "raw_benchmark_rows": 0,
        "comparable_unique_rows": 0,
        "excluded_rows": 0,
        "backtest_rows_compared": 0,
        "live_rows_compared": 0,
        "comparison_mismatches": 0,
        "duplicate_conflicts": 0,
        "missing_database_rows": 0,
        "unexpected_database_multiples": 0,
        "legacy_rows_without_target_date": 0,
    }

    for cfg in configs:
        scheme_report = _compare_scheme(cfg, calendar, engine)
        report["schemes"].append(scheme_report)
        totals["schemes_checked"] += 1
        totals["raw_benchmark_rows"] += scheme_report["raw_benchmark_rows"]
        totals["comparable_unique_rows"] += scheme_report["comparable_unique_rows"]
        totals["excluded_rows"] += sum(scheme_report["excluded"].values())
        totals["backtest_rows_compared"] += scheme_report["backtest_rows_compared"]
        totals["live_rows_compared"] += scheme_report["live_rows_compared"]
        totals["comparison_mismatches"] += len(scheme_report["mismatches"])
        totals["duplicate_conflicts"] += len(scheme_report["duplicate_conflicts"])
        totals["missing_database_rows"] += len(scheme_report["missing_database_rows"])
        totals["unexpected_database_multiples"] += len(scheme_report["unexpected_database_multiples"])
        totals["legacy_rows_without_target_date"] += scheme_report["legacy_rows_without_target_date"]

    report["summary"] = totals
    return report


def _load_active_source_configs() -> list[SchemeConfig]:
    configs: list[SchemeConfig] = []
    for config_path in sorted(SCHEMES_ROOT.glob("*/config.yaml")):
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if raw.get("status") != "active":
            continue
        backtest = raw.get("backtest") or {}
        if not backtest.get("benchmark_required"):
            continue
        scheme_id = str(raw["scheme_id"])
        output_start_date = str(
            backtest.get("start_date")
            or backtest.get("predict_start_date")
            or "2025-01-01"
        )
        configs.append(
            SchemeConfig(
                scheme_id=scheme_id,
                frequency=str(raw["frequency"]),
                horizon=int(raw["horizon"]),
                tenors=tuple(str(tenor) for tenor in raw.get("tenors", [])),
                benchmark_id=str(backtest.get("benchmark_id") or DEFAULT_BENCHMARK_ID),
                data_source=str(backtest.get("data_source") or DATA_SOURCE),
                output_start_date=output_start_date,
            )
        )
    return configs


def _compare_scheme(cfg: SchemeConfig, calendar: CalendarService, engine: Any) -> dict[str, Any]:
    rows = _read_benchmark_rows(cfg.scheme_id)
    points, excluded = _normalize_rows(cfg, rows, calendar)
    unique_points, duplicate_conflicts = _deduplicate_points(points)
    latest_run_id = _latest_backtest_run_id(engine, cfg)
    scheme_report: dict[str, Any] = {
        "scheme_id": cfg.scheme_id,
        "frequency": cfg.frequency,
        "horizon": cfg.horizon,
        "tenors": list(cfg.tenors),
        "benchmark_id": cfg.benchmark_id,
        "data_source": cfg.data_source,
        "latest_backtest_run_id": latest_run_id,
        "raw_benchmark_rows": len(rows),
        "comparable_unique_rows": len(unique_points),
        "excluded": excluded,
        "legacy_rows_without_target_date": 0,
        "backtest_rows_compared": 0,
        "live_rows_compared": 0,
        "mismatches": [],
        "missing_database_rows": [],
        "unexpected_database_multiples": [],
        "duplicate_conflicts": duplicate_conflicts,
    }

    scheme_report["legacy_rows_without_target_date"] = sum(1 for point in unique_points if point.target_date is None)

    for point in unique_points:
        if point.route == "live":
            db_rows = _fetch_live_rows(engine, cfg, point)
            scheme_report["live_rows_compared"] += _compare_db_rows(point, db_rows, scheme_report)
        else:
            db_rows = _fetch_backtest_rows(engine, latest_run_id, point)
            scheme_report["backtest_rows_compared"] += _compare_db_rows(point, db_rows, scheme_report)

    scheme_report["conclusion"] = _scheme_conclusion(scheme_report)
    return scheme_report


def _read_benchmark_rows(scheme_id: str) -> list[dict[str, str]]:
    path = SCHEMES_ROOT / scheme_id / "benchmarks" / "original_predictions_sample.csv"
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _normalize_rows(
    cfg: SchemeConfig,
    rows: list[dict[str, str]],
    calendar: CalendarService,
) -> tuple[list[BenchmarkPoint], dict[str, int]]:
    points: list[BenchmarkPoint] = []
    excluded = {
        "unsupported_tenor": 0,
        "before_output_start": 0,
        "missing_target_date": 0,
        "missing_feature_date": 0,
    }

    for idx, row in enumerate(rows, start=2):
        tenor = _row_text(row, "target_tenor", "tenor")
        if tenor not in cfg.tenors:
            excluded["unsupported_tenor"] += 1
            continue

        feature_date = _feature_date(cfg, row, calendar)
        if not feature_date:
            excluded["missing_feature_date"] += 1
            continue
        if feature_date < cfg.output_start_date:
            excluded["before_output_start"] += 1
            continue

        target_date = _target_date(row)
        if not target_date and cfg.frequency == "weekly":
            excluded["missing_target_date"] += 1
            continue

        route = "live" if target_date and target_date >= GRAY_TARGET_START else "backtest"
        points.append(
            BenchmarkPoint(
                row_number=idx,
                feature_date=feature_date,
                target_date=target_date,
                tenor=tenor,
                horizon=cfg.horizon,
                predicted_direction=_direction(row),
                confidence=_optional_float(_row_text(row, "confidence")),
                label=_optional_int(_row_text(row, "label")),
                is_correct=_optional_bool(_row_text(row, "is_correct")),
                feature_week_id=_optional_int(_row_text(row, "feature_week_id")),
                route=route,
            )
        )
    return points, excluded


def _deduplicate_points(points: list[BenchmarkPoint]) -> tuple[list[BenchmarkPoint], list[dict[str, Any]]]:
    by_key: dict[tuple[str, str | None, str, int], BenchmarkPoint] = {}
    conflicts: list[dict[str, Any]] = []
    for point in points:
        existing = by_key.get(point.key)
        if existing is None:
            by_key[point.key] = point
            continue
        if not _same_benchmark_value(existing, point):
            conflicts.append(
                {
                    "key": point.key,
                    "first_row": existing.row_number,
                    "second_row": point.row_number,
                }
            )
    return list(by_key.values()), conflicts


def _same_benchmark_value(left: BenchmarkPoint, right: BenchmarkPoint) -> bool:
    return (
        left.predicted_direction == right.predicted_direction
        and _confidence_equal(left.confidence, right.confidence)
        and left.label == right.label
        and left.is_correct == right.is_correct
        and left.route == right.route
    )


def _latest_backtest_run_id(engine: Any, cfg: SchemeConfig) -> int:
    stmt = text(
        """
        SELECT id
        FROM v_latest_backtest_run
        WHERE benchmark_id = :benchmark_id
          AND scheme_id = :scheme_id
          AND data_source = :data_source
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(
            stmt,
            {
                "benchmark_id": cfg.benchmark_id,
                "scheme_id": cfg.scheme_id,
                "data_source": cfg.data_source,
            },
        ).mappings().first()
    if row is None:
        raise AssertionError(f"missing latest backtest run for {cfg.scheme_id}")
    return int(row["id"])


def _fetch_backtest_rows(engine: Any, run_id: int, point: BenchmarkPoint) -> list[dict[str, Any]]:
    target_filter = "AND target_date = :target_date" if point.target_date is not None else ""
    stmt = text(
        f"""
        SELECT feature_date, target_date, target_tenor, horizon, predicted_direction,
               confidence, label
        FROM t_backtest_predictions
        WHERE run_id = :run_id
          AND feature_date = :feature_date
          {target_filter}
          AND target_tenor = :target_tenor
          AND horizon = :horizon
        """
    )
    params = {
        "run_id": run_id,
        "feature_date": point.feature_date,
        "target_tenor": point.tenor,
        "horizon": point.horizon,
    }
    if point.target_date is not None:
        params["target_date"] = point.target_date
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                stmt,
                params,
            ).mappings().all()
        ]


def _fetch_live_rows(engine: Any, cfg: SchemeConfig, point: BenchmarkPoint) -> list[dict[str, Any]]:
    stmt = text(
        """
        SELECT feature_date, target_date, target_tenor, horizon, predicted_direction,
               confidence, prediction_phase
        FROM t_scheme_predictions
        WHERE scheme_id = :scheme_id
          AND feature_date = :feature_date
          AND target_date = :target_date
          AND target_tenor = :target_tenor
          AND horizon = :horizon
        """
    )
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                stmt,
                {
                    "scheme_id": cfg.scheme_id,
                    "feature_date": point.feature_date,
                    "target_date": point.target_date,
                    "target_tenor": point.tenor,
                    "horizon": point.horizon,
                },
            ).mappings().all()
        ]


def _compare_db_rows(point: BenchmarkPoint, db_rows: list[dict[str, Any]], report: dict[str, Any]) -> int:
    if not db_rows:
        report["missing_database_rows"].append(_point_key_report(point))
        return 0
    if len(db_rows) > 1:
        report["unexpected_database_multiples"].append(_point_key_report(point) | {"count": len(db_rows)})
        return 0

    row = db_rows[0]
    mismatches: dict[str, Any] = {}
    if int(row["predicted_direction"]) != point.predicted_direction:
        mismatches["predicted_direction"] = {
            "benchmark": point.predicted_direction,
            "database": int(row["predicted_direction"]),
        }
    if point.confidence is not None and not _confidence_equal(point.confidence, _optional_float(row["confidence"])):
        mismatches["confidence"] = {
            "benchmark": point.confidence,
            "database": _optional_float(row["confidence"]),
        }
    if point.label is not None and "label" in row and row["label"] is not None and int(row["label"]) != point.label:
        mismatches["label"] = {
            "benchmark": point.label,
            "database": int(row["label"]),
        }
    if point.is_correct is not None and "label" in row and row.get("label") is not None:
        db_correct = int(row["label"]) == int(row["predicted_direction"])
        if db_correct != point.is_correct:
            mismatches["is_correct"] = {
                "benchmark": point.is_correct,
                "database": db_correct,
            }
    if point.route == "live" and row.get("prediction_phase") not in {"gray_live", "scheduled_live"}:
        mismatches["prediction_phase"] = {
            "benchmark_route": point.route,
            "database": row.get("prediction_phase"),
        }

    if mismatches:
        report["mismatches"].append(_point_key_report(point) | {"fields": mismatches})
        return 0
    return 1


def _point_key_report(point: BenchmarkPoint) -> dict[str, Any]:
    return {
        "row_number": point.row_number,
        "feature_date": point.feature_date,
        "target_date": point.target_date,
        "target_tenor": point.tenor,
        "horizon": point.horizon,
        "route": point.route,
        "feature_week_id": point.feature_week_id,
    }


def _scheme_conclusion(report: dict[str, Any]) -> str:
    if (
        report["mismatches"]
        or report["duplicate_conflicts"]
        or report["missing_database_rows"]
        or report["unexpected_database_multiples"]
    ):
        return "FAIL"
    if any(count for count in report["excluded"].values()) or report["legacy_rows_without_target_date"]:
        return "PASS_WITH_LEGACY_SAMPLE_LIMITATIONS"
    return "PASS"


def _feature_date(cfg: SchemeConfig, row: dict[str, str], calendar: CalendarService) -> str | None:
    explicit = _row_text(row, "feature_date", "framework_feature_date")
    if explicit:
        return explicit

    if cfg.frequency == "weekly":
        feature_week_id = _row_text(row, "feature_week_id")
        if feature_week_id:
            return calendar.week_id_to_last_trading_day(int(float(feature_week_id)))

    predict_date = _row_text(row, "predict_date", "date", "t")
    if predict_date and _looks_like_iso_date(predict_date):
        return predict_date
    return None


def _target_date(row: dict[str, str]) -> str | None:
    return _row_text(row, "target_date", "framework_target_date") or None


def _direction(row: dict[str, str]) -> int:
    value = _row_text(row, "predicted_direction", "direction", "prediction")
    if value == "":
        raise AssertionError(f"benchmark row missing direction: {row}")
    return int(float(value))


def _row_text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip() != "":
            return _date_string(value) if isinstance(value, (date, datetime)) else str(value).strip()
    return ""


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(float(str(value).strip()))


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    number = float(value)
    if math.isnan(number):
        return None
    return number


def _optional_bool(value: Any) -> bool | None:
    if value is None or str(value).strip() == "":
        return None
    text_value = str(value).strip().lower()
    if text_value in {"true", "1", "yes"}:
        return True
    if text_value in {"false", "0", "no"}:
        return False
    raise AssertionError(f"unexpected boolean value: {value}")


def _confidence_equal(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left == right
    return abs(float(left) - float(right)) <= CONFIDENCE_TOLERANCE


def _looks_like_iso_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _date_string(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)

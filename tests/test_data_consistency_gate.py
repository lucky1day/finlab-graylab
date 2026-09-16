from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event, text

from harness.context import GateContext
from harness.gates.data_consistency_gate import (
    ConsistencyFact,
    DataConsistencyError,
    DatabaseSnapshot,
    DataConsistencyGate,
    _FrozenCalendar,
    _actual_selector,
    _read_actuals,
    _read_references,
    _validate_task_date_contract,
    aggregate_display_facts,
    validate_snapshot_completeness,
    validate_snapshot_lineage,
    validate_and_join_facts,
)
from harness.result import GateStatus
from shared.historical_reference_compatibility import HistoricalBacktestReference
from shared.legacy_prediction_migration import (
    LegacyCorrectedExactEvidence,
    LegacyCorrectedFact,
    LegacyPredictionMigration,
)
from shared.prediction_context import (
    MONTHLY_TARGET_RULE,
    WEEKLY_AVERAGE_TARGET_RULE,
    WEEKLY_TARGET_RULE,
)
from shared.task_specs import load_legacy_native_scheme_ids
from shared.task_specs import TASK_COMBINATIONS


BASE_SCHEME_ID = "daily_5y_lgbm_5y10_0629"
REGISTRY_ID = f"{BASE_SCHEME_ID}__h1__5Y"
MONTHLY_FIELDS = [
    "month",
    "source",
    "samples",
    "metric_samples",
    "correct",
    "predicted_up",
    "predicted_down",
    "predicted_flat",
    "actual_up",
    "actual_down",
    "actual_flat",
    "up_true_positive",
    "down_true_positive",
]
DETAIL_FIELDS = [
    "source",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
    "actual_direction",
]


def _write_config(project_root: Path) -> None:
    config = project_root / "schemes" / BASE_SCHEME_ID / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "\n".join(
            [
                f"scheme_id: {BASE_SCHEME_ID}",
                "runtime_type: native_adapter",
                'name: "Consistency Daily"',
                'description: "fixture"',
                "horizon: 1",
                "task_type: T+1",
                "tenors: [5Y]",
                "frequency: daily",
                "schedule:",
                '  cron: "25 9 * * 1-5"',
                '  timezone: "Asia/Shanghai"',
                "entry_point: predict.run",
                "input_spec:",
                "  data_version: shared_data_service_daily.v1",
                '  required_columns: ["date", "TB0YWI0C"]',
                "status: active",
                "version_status: active",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'facts.sqlite'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE t_scheme_registry (
                    scheme_id TEXT PRIMARY KEY,
                    base_scheme_id TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    task_type TEXT NOT NULL,
                    runtime_type TEXT NOT NULL,
                    frequency TEXT NOT NULL,
                    target_tenor TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_scheme_predictions (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER,
                    backtest_run_id INTEGER,
                    scheme_version TEXT,
                    scheme_id TEXT NOT NULL,
                    target_tenor TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    predict_date TEXT NOT NULL,
                    feature_date TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    predicted_direction INTEGER NOT NULL,
                    backtest_actual_direction INTEGER
                );
                """
            )
        )
        connection.execute(
            text(
                "CREATE TABLE t_scheme_runs "
                "(run_id INTEGER PRIMARY KEY, scheme_id TEXT NOT NULL, "
                "scheme_version TEXT, runtime_type TEXT, status TEXT, "
                "prediction_phase TEXT, input_artifact_id TEXT, "
                "data_snapshot_id TEXT, "
                "predict_date TEXT, records_written INTEGER)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE t_backtest_runs "
                "(id INTEGER PRIMARY KEY, scheme_id TEXT NOT NULL, status TEXT, "
                "code_hash TEXT, config_hash TEXT, input_artifact_hash TEXT, summary TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE t_scheme_versions "
                "(scheme_id TEXT NOT NULL, scheme_version TEXT NOT NULL, "
                "runtime_type TEXT NOT NULL, data_snapshot_id TEXT, "
                "code_hash TEXT, config_hash TEXT, manifest_hash TEXT, "
                "status TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE t_input_artifacts "
                "(artifact_id TEXT PRIMARY KEY, scheme_id TEXT NOT NULL, "
                "scheme_version TEXT, predict_date TEXT, content_hash TEXT, "
                "schema_hash TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE t_backtest_predictions "
                "(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, "
                "scheme_id TEXT NOT NULL, target_tenor TEXT NOT NULL, "
                "horizon INTEGER NOT NULL, predict_date TEXT NOT NULL, "
                "feature_date TEXT, target_date TEXT, label INTEGER, "
                "predicted_direction INTEGER)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE t_trade_calendar "
                "(rdate TEXT PRIMARY KEY, trade_flag TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE api_wind_date "
                "(rdate TEXT PRIMARY KEY, week_id INTEGER)"
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_scheme_actuals (
                    tenor TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    direction_1d INTEGER,
                    direction_5d INTEGER
                )
                """
            )
        )
        connection.execute(
            text(
                "INSERT INTO t_scheme_registry VALUES "
                "(:registry_id, :base_id, 1, 'T+1', 'native_adapter', "
                "'daily', '5Y', 'active')"
            ),
            {"registry_id": REGISTRY_ID, "base_id": BASE_SCHEME_ID},
        )
        connection.execute(
            text(
                "INSERT INTO t_scheme_runs VALUES "
                "(11, :scheme_id, 'exact-a', 'native_adapter', 'success', "
                "'scheduled_live', 'artifact-a', NULL, '2026-06-10', 1), "
                "(12, :scheme_id, 'exact-a', 'native_adapter', 'success', "
                "'scheduled_live', 'artifact-b', NULL, '2026-06-11', 1)"
            ),
            {"scheme_id": BASE_SCHEME_ID},
        )
        connection.execute(
            text(
                "INSERT INTO t_backtest_runs VALUES "
                "(7, :scheme_id, 'success', :code_hash, :config_hash, "
                ":input_hash, :summary)"
            ),
            {
                "scheme_id": BASE_SCHEME_ID,
                "code_hash": "a" * 64,
                "config_hash": "b" * 64,
                "input_hash": "d" * 64,
                "summary": json.dumps(
                    {
                        "persisted_prediction_count": 1,
                        "scheme_version": "exact-a",
                        "manifest_hash": "c" * 64,
                    }
                ),
            },
        )
        connection.execute(
            text(
                "INSERT INTO t_scheme_versions VALUES "
                "(:scheme_id, 'exact-a', 'native_adapter', NULL, "
                ":code_hash, :config_hash, "
                ":manifest_hash, 'active')"
            ),
            {
                "scheme_id": BASE_SCHEME_ID,
                "code_hash": "a" * 64,
                "config_hash": "b" * 64,
                "manifest_hash": "c" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO t_input_artifacts VALUES "
                "('artifact-a', :scheme_id, 'exact-a', '2026-06-10', "
                ":content_hash, :schema_hash), "
                "('artifact-b', :scheme_id, 'exact-a', '2026-06-11', "
                ":content_hash, :schema_hash)"
            ),
            {
                "scheme_id": BASE_SCHEME_ID,
                "content_hash": "d" * 64,
                "schema_hash": "e" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO t_backtest_predictions VALUES "
                "(1, 7, :scheme_id, '5Y', 1, '2026-05-19', "
                "'2026-05-19', '2026-05-20', 1, 1)"
            ),
            {"scheme_id": BASE_SCHEME_ID},
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_predictions
                    (id, run_id, backtest_run_id, scheme_version, scheme_id,
                     target_tenor, horizon, predict_date, feature_date,
                     target_date, predicted_direction, backtest_actual_direction)
                VALUES
                    (1, NULL, 7, 'exact-a', :scheme_id, '5Y', 1,
                     '2026-05-19', '2026-05-19', '2026-05-20', 1, 1),
                    (2, 11, NULL, 'exact-a', :scheme_id, '5Y', 1,
                     '2026-06-10', '2026-06-09', '2026-06-10', 0, NULL),
                    (3, 12, NULL, 'exact-a', :scheme_id, '5Y', 1,
                     '2026-06-11', '2026-06-10', '2026-06-11', -1, NULL)
                """
            ),
            {"scheme_id": BASE_SCHEME_ID},
        )
        connection.execute(
            text(
                "INSERT INTO t_scheme_actuals VALUES "
                "('5Y', '2026-06-10', -1, NULL)"
            )
        )
        calendar_rows = _calendar_rows(date(2026, 5, 1), date(2026, 7, 31))
        connection.execute(
            text(
                "INSERT INTO t_trade_calendar (rdate, trade_flag) "
                "VALUES (:rdate, :trade_flag)"
            ),
            [
                {"rdate": row["rdate"], "trade_flag": row["trade_flag"]}
                for row in calendar_rows
            ],
        )
        connection.execute(
            text(
                "INSERT INTO api_wind_date (rdate, week_id) "
                "VALUES (:rdate, :week_id)"
            ),
            [
                {"rdate": row["rdate"], "week_id": row["week_id"]}
                for row in calendar_rows
            ],
        )
    return engine


def _calendar_rows(start: date, end: date) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    current = start
    spring_closures = {
        *(date(2026, 2, 16) + timedelta(days=offset) for offset in range(5)),
        *(date(2027, 2, 8) + timedelta(days=offset) for offset in range(5)),
    }
    while current <= end:
        rows.append(
            {
                "rdate": current.isoformat(),
                "trade_flag": (
                    "1"
                    if current.weekday() < 5 and current not in spring_closures
                    else "0"
                ),
                "week_id": int(current.strftime("%G%V")),
            }
        )
        current += timedelta(days=1)
    return tuple(rows)


def _date_contract_snapshot(
    task_type: str,
    predict_date: str,
    feature_date: str,
    target_date: str,
    *,
    is_live: bool = True,
) -> DatabaseSnapshot:
    horizon = TASK_COMBINATIONS[task_type][0]
    scheme_id = f"contract_{task_type}"
    return DatabaseSnapshot(
        registry_rows=(
            {
                "scheme_id": f"{scheme_id}__h{horizon}__5Y",
                "base_scheme_id": scheme_id,
                "horizon": horizon,
                "task_type": task_type,
                "runtime_type": "blackbox_v2",
                "frequency": TASK_COMBINATIONS[task_type][2],
                "target_tenor": "5Y",
                "status": "active",
            },
        ),
        prediction_rows=(
            {
                "id": 1,
                "run_id": 1 if is_live else None,
                "backtest_run_id": None if is_live else 1,
                "scheme_version": "exact-a",
                "scheme_id": scheme_id,
                "target_tenor": "5Y",
                "horizon": horizon,
                "predict_date": predict_date,
                "feature_date": feature_date,
                "target_date": target_date,
                "predicted_direction": 1,
                "backtest_actual_direction": None if is_live else 1,
            },
        ),
        actual_rows=(),
        live_runs=(
            {
                "reference_id": 1,
                "scheme_id": scheme_id,
                "status": "success",
            },
        ) if is_live else (),
        backtest_runs=() if is_live else (
            {
                "reference_id": 1,
                "scheme_id": scheme_id,
                "status": "success",
            },
        ),
        calendar_rows=_calendar_rows(
            date(2025, 12, 1),
            date(2027, 3, 31),
        ),
        digest="date-contract-fixture",
    )


def _summary(*, wrong: bool = False) -> dict:
    live_row = ["2026-06", "live", 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0]
    if wrong:
        live_row[2] = 2
    return {
        "representation": "summary",
        "snapshot_id": "summary-1",
        "display_until": "2026-06-30",
        "monthly_row_fields": MONTHLY_FIELDS,
        "schemes": [
            {
                "scheme_id": REGISTRY_ID,
                "monthly_rows": [
                    ["2026-05", "backtest", 1, 1, 1, 1, 0, 0, 1, 0, 0, 1, 0],
                    live_row,
                ],
            }
        ],
    }


def _details() -> dict[tuple[str, str], dict]:
    return {
        ("2026-05", "backtest"): {
            "representation": "detail",
            "snapshot_id": "detail-may",
            "scheme_id": REGISTRY_ID,
            "month": "2026-05",
            "source": "backtest",
            "display_until": "2026-06-30",
            "row_fields": DETAIL_FIELDS,
            "rows": [
                ["backtest", "2026-05-19", "2026-05-19", "2026-05-20", 1, 1]
            ],
        },
        ("2026-06", "live"): {
            "representation": "detail",
            "snapshot_id": "detail-june",
            "scheme_id": REGISTRY_ID,
            "month": "2026-06",
            "source": "live",
            "display_until": "2026-06-30",
            "row_fields": DETAIL_FIELDS,
            "rows": [
                ["live", "2026-06-10", "2026-06-09", "2026-06-10", 0, -1],
                ["live", "2026-06-11", "2026-06-10", "2026-06-11", -1, None],
            ],
        },
    }


def _fetcher(*, wrong_summary: bool = False, wrong_detail: bool = False, mutate=None):
    calls = []
    details = _details()

    def fetch(url, **_kwargs):
        calls.append(url)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if not query:
            if mutate is not None:
                mutate()
            return _summary(wrong=wrong_summary), 200, {
                "request_id": "summary-request",
                "fetched_at": "2026-07-01T00:00:00+00:00",
            }
        key = (query["month"][0], query["source"][0])
        payload = details[key]
        if wrong_detail and key == ("2026-06", "live"):
            payload = {**payload, "rows": [*payload["rows"]]}
            payload["rows"][0] = [*payload["rows"][0]]
            payload["rows"][0][-1] = 1
        return payload, 200, {
            "request_id": f"detail-{key[0]}-{key[1]}",
            "fetched_at": "2026-07-01T00:00:01+00:00",
        }

    return fetch, calls


def _context(tmp_path: Path, engine) -> GateContext:
    _write_config(tmp_path)
    return GateContext(
        scheme_id=BASE_SCHEME_ID,
        predict_date="data-consistency",
        project_root=tmp_path,
        engine_factory=lambda: engine,
        api_base_url="https://factor.example.test",
        dashboard_scheme_ids=(BASE_SCHEME_ID,),
        dashboard_session_token="private-session",
    )


def _gate(fetcher) -> DataConsistencyGate:
    return DataConsistencyGate(
        fetcher=fetcher,
        clock=lambda: datetime(
            2026,
            6,
            30,
            12,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
    )


def test_data_consistency_gate_reconciles_fact_actual_summary_and_detail(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    fetcher, calls = _fetcher()

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.PASSED
    assert result.errors == []
    assert len(calls) == 3
    assert calls[0] == "https://factor.example.test/api/factor-lab/dashboard"
    assert "month=2026-05" in calls[1]
    assert "month=2026-06" in calls[2]
    evidence = {item.key: item.value for item in result.evidence}
    assert evidence["completeness"]["status"] == "PASSED"
    assert evidence["lineage"]["status"] == "PASSED"
    assert evidence["representation"]["status"] == "PASSED"
    assert evidence["completeness"]["pending_actual_count"] == 1


def test_duplicate_successful_live_run_does_not_create_a_second_fact_gap(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO t_scheme_runs VALUES "
                "(13, :scheme_id, 'exact-a', 'native_adapter', 'success', "
                "'scheduled_live', 'artifact-a', NULL, '2026-06-10', 0)"
            ),
            {"scheme_id": BASE_SCHEME_ID},
        )
    fetcher, _calls = _fetcher()

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.PASSED
    evidence = {item.key: item.value for item in result.evidence}
    assert evidence["completeness"]["expected_live_fact_count"] == 2


def test_blackbox_live_snapshot_can_advance_after_version_activation(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE t_scheme_registry SET runtime_type = 'blackbox_v2'"
            )
        )
        connection.execute(
            text(
                "UPDATE t_scheme_versions SET runtime_type = 'blackbox_v2', "
                "data_snapshot_id = 'snapshot-000000000000000000000001'"
            )
        )
        connection.execute(
            text(
                "UPDATE t_scheme_runs SET runtime_type = 'blackbox_v2', "
                "input_artifact_id = NULL, data_snapshot_id = CASE run_id "
                "WHEN 11 THEN 'snapshot-000000000000000000000002' "
                "ELSE 'snapshot-000000000000000000000003' END"
            )
        )
    fetcher, _calls = _fetcher()

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.PASSED


def test_live_fact_must_reference_a_run_that_produced_its_business_key(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE t_scheme_predictions SET run_id = CASE id "
                "WHEN 2 THEN 12 WHEN 3 THEN 11 ELSE run_id END "
                "WHERE id IN (2, 3)"
            )
        )
    fetcher, _calls = _fetcher()

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.FAILED
    assert any("live product run binding drift" in error for error in result.errors)


def test_representation_remains_independent_when_lineage_dates_fail(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE t_scheme_predictions SET predict_date = '2026-05-18' "
                "WHERE id = 1"
            )
        )
        connection.execute(
            text(
                "UPDATE t_backtest_predictions SET predict_date = '2026-05-18' "
                "WHERE id = 1"
            )
        )
    base_fetcher, _calls = _fetcher()

    def fetcher(url, **kwargs):
        payload, status, metadata = base_fetcher(url, **kwargs)
        query = parse_qs(urlsplit(url).query)
        if query.get("month") == ["2026-05"]:
            payload = {**payload, "rows": [list(row) for row in payload["rows"]]}
            payload["rows"][0][1] = "2026-05-18"
        return payload, status, metadata

    result = _gate(fetcher).run(_context(tmp_path, engine))

    evidence = {item.key: item.value for item in result.evidence}
    assert result.status is GateStatus.FAILED
    assert evidence["completeness"]["status"] == "PASSED"
    assert evidence["lineage"]["status"] == "FAILED"
    assert evidence["representation"]["status"] == "PASSED"
    assert evidence["joined_fact_count"] == 3
    assert evidence["business_contract_validated_fact_count"] == 0
    assert any("feature <= predict <= target" in error for error in result.errors)


def test_data_consistency_gate_detects_missing_expected_live_prediction(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM t_scheme_predictions WHERE id = 3"))
    fetcher, _calls = _fetcher()

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.FAILED
    assert any("missing live product predictions" in error for error in result.errors)


def test_data_consistency_gate_detects_prediction_exact_drift(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE t_scheme_predictions SET scheme_version = 'exact-b' WHERE id = 2")
        )
    fetcher, _calls = _fetcher()

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.FAILED
    evidence = {item.key: item.value for item in result.evidence}
    assert evidence["completeness"]["status"] == "PASSED"
    assert evidence["lineage"]["status"] == "FAILED"
    assert evidence["representation"]["status"] == "PASSED"
    assert evidence["business_contract_validated_fact_count"] == 3
    assert any("exact" in error and "drift" in error for error in result.errors)


def test_data_consistency_gate_fails_mature_actual_gap_but_keeps_future_pending(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM t_scheme_actuals WHERE trade_date = '2026-06-10'")
        )
        connection.execute(
            text(
                "INSERT INTO t_scheme_actuals VALUES "
                "('5Y', '2026-06-11', 1, NULL)"
            )
        )
    fetcher, _calls = _fetcher()

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.FAILED
    assert any("mature Actual is missing" in error for error in result.errors)


def test_data_consistency_gate_blocks_without_backtest_authority(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM t_backtest_predictions"))
    fetcher, _calls = _fetcher()

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.BLOCKED
    assert any("no immutable source predictions" in error for error in result.errors)


def test_completeness_blocks_when_an_active_scope_has_no_product_reference() -> None:
    snapshot = DatabaseSnapshot(
        registry_rows=(
            {
                "scheme_id": "missing_history__h1__5Y",
                "base_scheme_id": "missing_history",
                "horizon": 1,
                "task_type": "T+1",
                "runtime_type": "blackbox_v2",
                "frequency": "daily",
                "target_tenor": "5Y",
                "status": "active",
            },
        ),
        prediction_rows=(),
        actual_rows=(),
        live_runs=(),
        backtest_runs=(),
        calendar_rows=_calendar_rows(date(2026, 1, 1), date(2026, 2, 1)),
        digest="missing-history-authority",
    )

    errors, details = validate_snapshot_completeness(
        snapshot,
        display_until="2026-02-01",
    )

    assert errors == []
    assert details["authority_gaps"] == [
        "selected Registry scopes have no durable backtest product reference: "
        "[('missing_history', '5Y', 1)]"
    ]


def test_completeness_keeps_drift_when_other_authority_is_missing() -> None:
    snapshot = DatabaseSnapshot(
        registry_rows=(
            {
                "scheme_id": "mixed__h1__5Y",
                "base_scheme_id": "mixed",
                "horizon": 1,
                "task_type": "T+1",
                "runtime_type": "blackbox_v2",
                "frequency": "daily",
                "target_tenor": "5Y",
                "status": "active",
            },
        ),
        prediction_rows=(
            {
                "id": 1,
                "run_id": None,
                "backtest_run_id": 7,
                "scheme_version": "exact-a",
                "scheme_id": "mixed",
                "target_tenor": "5Y",
                "horizon": 1,
                "predict_date": "2026-05-19",
                "feature_date": "2026-05-19",
                "target_date": "2026-05-20",
                "predicted_direction": 1,
                "backtest_actual_direction": 1,
            },
            {
                "id": 2,
                "run_id": 11,
                "backtest_run_id": None,
                "scheme_version": "exact-a",
                "scheme_id": "mixed",
                "target_tenor": "5Y",
                "horizon": 1,
                "predict_date": "2026-06-10",
                "feature_date": "2026-06-09",
                "target_date": "2026-06-11",
                "predicted_direction": -1,
                "backtest_actual_direction": None,
            },
        ),
        actual_rows=(),
        live_runs=(
            {
                "reference_id": 11,
                "scheme_id": "mixed",
                "scheme_version": "exact-a",
                "status": "success",
                "predict_date": "2026-06-10",
            },
        ),
        backtest_runs=(
            {
                "reference_id": 7,
                "scheme_id": "mixed",
                "status": "success",
                "summary": {"persisted_prediction_count": 1},
            },
        ),
        calendar_rows=_calendar_rows(date(2026, 5, 1), date(2026, 7, 31)),
        digest="mixed-gap-and-drift",
        backtest_prediction_rows=(),
    )

    errors, details = validate_snapshot_completeness(
        snapshot,
        display_until="2026-06-30",
    )

    assert any("missing live product predictions" in error for error in errors)
    assert any("extra live product predictions" in error for error in errors)
    assert details["authority_gaps"] == [
        "backtest run 7 has no immutable source predictions"
    ]


def test_unresolved_live_run_is_not_mislabeled_as_extra_fact() -> None:
    registry = {
        "scheme_id": "calendar_gap__h1__5Y",
        "base_scheme_id": "calendar_gap",
        "horizon": 1,
        "task_type": "T+1",
        "runtime_type": "blackbox_v2",
        "frequency": "daily",
        "target_tenor": "5Y",
        "status": "active",
    }
    backtest_product = {
        "id": 1,
        "run_id": None,
        "backtest_run_id": 7,
        "scheme_version": "exact-a",
        "scheme_id": "calendar_gap",
        "target_tenor": "5Y",
        "horizon": 1,
        "predict_date": "2026-05-19",
        "feature_date": "2026-05-19",
        "target_date": "2026-05-20",
        "predicted_direction": 1,
        "backtest_actual_direction": 1,
    }
    unresolved_product = {
        "id": 2,
        "run_id": 11,
        "backtest_run_id": None,
        "scheme_version": "exact-a",
        "scheme_id": "calendar_gap",
        "target_tenor": "5Y",
        "horizon": 1,
        "predict_date": "2026-07-02",
        "feature_date": "2026-07-01",
        "target_date": "2026-07-03",
        "predicted_direction": -1,
        "backtest_actual_direction": None,
    }
    source = {
        "run_id": 7,
        "scheme_id": "calendar_gap",
        "target_tenor": "5Y",
        "horizon": 1,
        "predict_date": "2026-05-19",
        "feature_date": "2026-05-19",
        "target_date": "2026-05-20",
        "label": 1,
        "predicted_direction": 1,
    }
    snapshot = DatabaseSnapshot(
        registry_rows=(registry,),
        prediction_rows=(backtest_product, unresolved_product),
        actual_rows=(),
        live_runs=(
            {
                "reference_id": 11,
                "scheme_id": "calendar_gap",
                "scheme_version": "exact-a",
                "status": "success",
                "predict_date": "2026-07-02",
            },
        ),
        backtest_runs=(
            {
                "reference_id": 7,
                "scheme_id": "calendar_gap",
                "status": "success",
                "summary": {"persisted_prediction_count": 1},
            },
        ),
        calendar_rows=_calendar_rows(date(2026, 5, 1), date(2026, 6, 30)),
        digest="unresolved-live-run",
        backtest_prediction_rows=(source,),
    )

    errors, details = validate_snapshot_completeness(
        snapshot,
        display_until="2026-07-31",
    )

    assert errors == []
    assert any(
        "live run 11 date authority unavailable" in gap
        for gap in details["authority_gaps"]
    )

    resolved_drift_product = {
        **unresolved_product,
        "id": 3,
        "run_id": 12,
        "predict_date": "2026-06-10",
        "feature_date": "2026-06-09",
        "target_date": "2026-06-11",
    }
    mixed_snapshot = replace(
        snapshot,
        prediction_rows=(
            *snapshot.prediction_rows,
            resolved_drift_product,
        ),
        live_runs=(
            *snapshot.live_runs,
            {
                "reference_id": 12,
                "scheme_id": "calendar_gap",
                "scheme_version": "exact-a",
                "status": "success",
                "predict_date": "2026-06-10",
            },
        ),
    )

    mixed_errors, mixed_details = validate_snapshot_completeness(
        mixed_snapshot,
        display_until="2026-07-31",
    )

    assert any("missing live product predictions" in error for error in mixed_errors)
    assert any("extra live product predictions" in error for error in mixed_errors)
    assert mixed_details["authority_gaps"]


def test_actual_snapshot_query_is_shared_by_equivalent_scheme_scope(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    statements: list[str] = []

    def capture(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        statements.append(" ".join(statement.split()).lower())

    registry = {
        "base_scheme_id": "one",
        "target_tenor": "5Y",
        "horizon": 1,
        "task_type": "T+1",
    }
    predictions = [
        {
            "scheme_id": base,
            "target_tenor": "5Y",
            "horizon": 1,
            "target_date": "2026-06-10",
            "backtest_actual_direction": None,
        }
        for base in ("one", "two")
    ]
    event.listen(engine, "after_cursor_execute", capture)
    try:
        with engine.connect() as connection:
            rows = _read_actuals(
                connection,
                [registry, {**registry, "base_scheme_id": "two"}],
                predictions,
                deadline=None,
                monotonic=lambda: 0.0,
            )
    finally:
        event.remove(engine, "after_cursor_execute", capture)

    assert len(rows) == 1
    assert sum("from t_scheme_actuals" in item for item in statements) == 1


def test_backtest_source_registry_is_hydrated_by_exact_base_scope(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    source_id = "archived_source"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO t_scheme_registry VALUES "
                "(:registry_id, :base_id, 5, 'T+5', 'blackbox_v2', "
                "'daily', '5Y', 'archived')"
            ),
            {
                "registry_id": f"{source_id}__h5__5Y",
                "base_id": source_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO t_scheme_versions VALUES "
                "(:scheme_id, 'source-exact', 'blackbox_v2', "
                "'snapshot-0123456789abcdef01234567', :code_hash, "
                ":config_hash, :manifest_hash, 'retired')"
            ),
            {
                "scheme_id": source_id,
                "code_hash": "a" * 64,
                "config_hash": "b" * 64,
                "manifest_hash": "c" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO t_backtest_runs VALUES "
                "(17, :scheme_id, 'success', :code_hash, :config_hash, "
                "'snapshot-0123456789abcdef01234567', :summary)"
            ),
            {
                "scheme_id": source_id,
                "code_hash": "a" * 64,
                "config_hash": "b" * 64,
                "summary": json.dumps(
                    {
                        "scheme_version": "source-exact",
                        "manifest_hash": "c" * 64,
                    }
                ),
            },
        )
        rows = _read_references(
            connection,
            table="t_backtest_runs",
            column="id",
            values=[17],
            deadline=None,
            monotonic=lambda: 0.0,
        )

    assert rows[17]["source_registry_scopes"] == [
        {"target_tenor": "5Y", "horizon": 5, "status": "archived"}
    ]


def test_data_consistency_gate_applies_one_total_http_budget(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    base_fetcher, calls = _fetcher()
    elapsed = {"value": 0.0}

    def slow_fetcher(url, **kwargs):
        response = base_fetcher(url, **kwargs)
        elapsed["value"] += 6.0
        return response

    gate = DataConsistencyGate(
        fetcher=slow_fetcher,
        clock=lambda: datetime(
            2026,
            6,
            30,
            12,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        monotonic=lambda: elapsed["value"],
    )
    ctx = replace(_context(tmp_path, engine), timeout_sec=10)

    result = gate.run(ctx)

    assert result.status is GateStatus.FAILED
    assert result.errors == [
        "data_consistency_timeout: total gate budget exhausted"
    ]
    assert len(calls) == 2


def test_data_consistency_gate_detects_summary_count_drift(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    fetcher, _calls = _fetcher(wrong_summary=True)

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.FAILED
    assert any("Summary monthly rows mismatch" in error for error in result.errors)


def test_data_consistency_gate_detects_actual_join_drift_in_detail(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    fetcher, _calls = _fetcher(wrong_detail=True)

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.FAILED
    assert any("Detail rows mismatch" in error for error in result.errors)


def test_data_consistency_gate_rejects_duplicate_business_key_even_when_exact_differs(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_predictions
                    (id, run_id, backtest_run_id, scheme_version, scheme_id,
                     target_tenor, horizon, predict_date, feature_date,
                     target_date, predicted_direction, backtest_actual_direction)
                VALUES (4, 11, NULL, 'exact-b', :scheme_id, '5Y', 1,
                        '2026-06-12', '2026-06-10', '2026-06-11', 1, NULL)
                """
            ),
            {"scheme_id": BASE_SCHEME_ID},
        )
    fetcher, _calls = _fetcher()

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.FAILED
    assert any("exact version is not part" in error for error in result.errors)


def test_data_consistency_gate_rejects_conflicting_actual_facts(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO t_scheme_actuals VALUES "
                "('5Y', '2026-06-10', 1, NULL)"
            )
        )
    fetcher, _calls = _fetcher()

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.FAILED
    assert any("conflicting Actual facts" in error for error in result.errors)


def test_data_consistency_gate_requires_run_reference_to_same_scheme(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE t_scheme_runs SET scheme_id = 'another_scheme' "
                "WHERE run_id = 11"
            )
        )
    fetcher, _calls = _fetcher()

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.FAILED
    assert any("another scheme's live run" in error for error in result.errors)


def test_data_consistency_gate_blocks_if_selected_facts_change_during_http_window(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)

    def mutate() -> None:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE t_scheme_actuals SET direction_1d = 1 "
                    "WHERE trade_date = '2026-06-10'"
                )
            )

    fetcher, _calls = _fetcher(mutate=mutate)

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.BLOCKED
    assert result.errors == [
        "selected database facts or the Shanghai display date changed during "
        "HTTP reconciliation; retry against a stable input window"
    ]


def test_data_consistency_gate_rejects_service_controlled_early_display_cutoff(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    fetcher, _calls = _fetcher()

    def stale_summary(url, **kwargs):
        payload, status, metadata = fetcher(url, **kwargs)
        if not urlsplit(url).query:
            payload = {**payload, "display_until": "2026-06-29"}
        return payload, status, metadata

    result = _gate(stale_summary).run(_context(tmp_path, engine))

    assert result.status is GateStatus.FAILED
    assert any("independent Shanghai business date" in error for error in result.errors)


def test_data_consistency_gate_blocks_if_final_scope_can_no_longer_be_read(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)

    def mutate() -> None:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE t_scheme_registry SET status = 'paused'")
            )

    fetcher, _calls = _fetcher(mutate=mutate)

    result = _gate(fetcher).run(_context(tmp_path, engine))

    assert result.status is GateStatus.BLOCKED


def test_database_change_takes_precedence_over_http_probe_failure(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)

    def fail_after_mutation(_url, **_kwargs):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE t_scheme_actuals SET direction_1d = 1 "
                    "WHERE trade_date = '2026-06-10'"
                )
            )
        raise RuntimeError("probe failed while facts changed")

    result = _gate(fail_after_mutation).run(_context(tmp_path, engine))

    assert result.status is GateStatus.BLOCKED
    assert all("probe failed" not in error for error in result.errors)


def test_actual_selector_keeps_t1_and_t5_directions_independent() -> None:
    rule = "target_date_yield_vs_feature_date_yield"
    snapshot = DatabaseSnapshot(
        registry_rows=(
            {
                "scheme_id": "daily_1__h1__5Y",
                "base_scheme_id": "daily_1",
                    "horizon": 1,
                    "task_type": "T+1",
                    "runtime_type": "blackbox_v2",
                    "frequency": "daily",
                "target_tenor": "5Y",
                "status": "active",
            },
            {
                "scheme_id": "daily_5__h5__5Y",
                "base_scheme_id": "daily_5",
                    "horizon": 5,
                    "task_type": "T+5",
                    "runtime_type": "blackbox_v2",
                    "frequency": "daily",
                "target_tenor": "5Y",
                "status": "active",
            },
        ),
        prediction_rows=(
            {
                "id": 1,
                "run_id": 1,
                "backtest_run_id": None,
                "scheme_version": "a",
                "scheme_id": "daily_1",
                "target_tenor": "5Y",
                "horizon": 1,
                "predict_date": "2026-06-10",
                "feature_date": "2026-06-09",
                "target_date": "2026-06-10",
                "predicted_direction": 1,
                "backtest_actual_direction": None,
            },
            {
                "id": 2,
                "run_id": 2,
                "backtest_run_id": None,
                "scheme_version": "b",
                "scheme_id": "daily_5",
                "target_tenor": "5Y",
                "horizon": 5,
                "predict_date": "2026-06-10",
                "feature_date": "2026-06-09",
                "target_date": "2026-06-16",
                "predicted_direction": -1,
                "backtest_actual_direction": None,
            },
        ),
        actual_rows=(
            {
                "actual_kind": "daily_1d",
                "target_tenor": "5Y",
                "target_date": "2026-06-10",
                "target_rule": rule,
                "actual_direction": 1,
            },
            {
                "actual_kind": "daily_5d",
                "target_tenor": "5Y",
                "target_date": "2026-06-16",
                "target_rule": rule,
                "actual_direction": -1,
            },
        ),
        live_runs=(
            {"reference_id": 1, "scheme_id": "daily_1", "status": "success"},
            {"reference_id": 2, "scheme_id": "daily_5", "status": "success"},
        ),
        backtest_runs=(),
        calendar_rows=_calendar_rows(date(2026, 6, 1), date(2026, 6, 30)),
        digest="fixture",
    )

    facts = validate_and_join_facts(snapshot, display_until="2026-06-30")

    assert [fact.actual_direction for fact in facts] == [1, -1]


@pytest.mark.parametrize(
    ("task_type", "expected"),
    [
        ("weekly_point", ("weekly", WEEKLY_TARGET_RULE)),
        ("weekly_average", ("weekly", WEEKLY_AVERAGE_TARGET_RULE)),
        ("monthly", ("monthly", MONTHLY_TARGET_RULE)),
    ],
)
def test_actual_selector_uses_persisted_actual_rules(
    task_type: str,
    expected: tuple[str, str],
) -> None:
    assert _actual_selector(task_type) == expected


def test_independent_aggregation_maps_monthly_average_to_following_display_month() -> None:
    fact = ConsistencyFact(
        scheme_id="monthly-average__h1__5Y",
        target_tenor="5Y",
        horizon=1,
        task_type="monthly_average",
        predict_date="2026-05-15",
        feature_date="2026-05-15",
        target_date="2026-05-16",
        predicted_direction=-1,
        actual_direction=-1,
    )

    assert aggregate_display_facts([fact]) == {
        "monthly-average__h1__5Y": [
            ["2026-06", "backtest", 1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 1]
        ]
    }


@pytest.mark.parametrize(
    ("base_scheme_id", "task_type", "horizon", "frequency"),
    [
        ("weekly_avg_5y_lgbm_0529", "weekly_average", 6, "weekly"),
        ("monthly_5y_knn_top20_0629", "monthly", 30, "monthly"),
    ],
)
def test_registered_w4_native_runtime_contract_is_accepted(
    base_scheme_id: str,
    task_type: str,
    horizon: int,
    frequency: str,
) -> None:
    snapshot = DatabaseSnapshot(
        registry_rows=(
            {
                "scheme_id": f"{base_scheme_id}__h{horizon}__5Y",
                "base_scheme_id": base_scheme_id,
                "horizon": horizon,
                "task_type": task_type,
                "runtime_type": "native_adapter",
                "frequency": frequency,
                "target_tenor": "5Y",
                "status": "active",
            },
        ),
        prediction_rows=(),
        actual_rows=(),
        live_runs=(),
        backtest_runs=(),
        calendar_rows=_calendar_rows(date(2026, 1, 1), date(2026, 2, 1)),
        digest="native-contract",
    )

    assert validate_and_join_facts(
        snapshot,
        display_until="2026-02-01",
        legacy_native_scheme_ids=load_legacy_native_scheme_ids(
            Path(__file__).resolve().parents[1]
        ),
    ) == []


def test_unregistered_native_and_blackbox_h6_are_rejected() -> None:
    base = {
        "scheme_id": "unknown__h6__5Y",
        "base_scheme_id": "unknown",
        "horizon": 6,
        "task_type": "weekly_point",
        "target_tenor": "5Y",
        "status": "active",
        "frequency": "weekly",
    }
    for runtime_type in ("native_adapter", "blackbox_v2"):
        snapshot = DatabaseSnapshot(
            registry_rows=({**base, "runtime_type": runtime_type},),
            prediction_rows=(),
            actual_rows=(),
            live_runs=(),
            backtest_runs=(),
            calendar_rows=_calendar_rows(date(2026, 1, 1), date(2026, 2, 1)),
            digest="invalid-runtime-contract",
        )
        with pytest.raises(DataConsistencyError, match="task contract mismatch"):
            validate_and_join_facts(
                snapshot,
                display_until="2026-02-01",
                legacy_native_scheme_ids=load_legacy_native_scheme_ids(
                    Path(__file__).resolve().parents[1]
                ),
            )


def test_approved_retired_historical_reference_is_accepted() -> None:
    prediction_id = "liwei_0616_5y01_full_oos_k3_div_k10"
    source_id = f"{prediction_id}_bbv2"
    snapshot = _date_contract_snapshot(
        "T+5",
        "2026-05-19",
        "2026-05-19",
        "2026-05-26",
        is_live=False,
    )
    snapshot = replace(
        snapshot,
        registry_rows=(
            {
                **snapshot.registry_rows[0],
                "scheme_id": f"{prediction_id}__h5__5Y",
                "base_scheme_id": prediction_id,
            },
        ),
        prediction_rows=(
            {
                **snapshot.prediction_rows[0],
                "scheme_id": prediction_id,
            },
        ),
        backtest_runs=(
            {
                "reference_id": 1,
                "scheme_id": source_id,
                "status": "success",
                "source_registry_scopes": [
                    {
                        "target_tenor": "5Y",
                        "horizon": 5,
                        "status": "archived",
                    }
                ],
                "code_hash": "a" * 64,
                "config_hash": "b" * 64,
                "input_artifact_hash": "c" * 64,
                "summary": {"scheme_version": "source-exact"},
            },
        ),
        version_rows=(
            {
                "scheme_id": source_id,
                "scheme_version": "source-exact",
                "status": "retired",
            },
        ),
    )
    relationship = HistoricalBacktestReference(
        prediction_id,
        source_id,
        "fixture",
    )

    facts = validate_and_join_facts(
        snapshot,
        display_until="2026-06-30",
        historical_references=frozenset({relationship}),
    )

    assert len(facts) == 1


def test_approved_cross_source_run_uses_referenced_subset_and_snapshot_identity() -> None:
    prediction_id = "liwei_0616_5y01_full_oos_k3_div_k10"
    source_id = f"{prediction_id}_bbv2"
    snapshot_id = "snapshot-0123456789abcdef01234567"
    manifest_hash = "c" * 64
    product_version = {
        "scheme_id": prediction_id,
        "scheme_version": "product-exact",
        "runtime_type": "blackbox_v2",
        "data_snapshot_id": snapshot_id,
        "code_hash": "a" * 64,
        "config_hash": "b" * 64,
        "manifest_hash": manifest_hash,
        "status": "retired",
    }
    source_version = {
        **product_version,
        "scheme_id": source_id,
        "scheme_version": "source-exact",
    }
    summary = {
        "persisted_prediction_count": 2,
        "scheme_version": "source-exact",
        "manifest_hash": manifest_hash,
        "data_snapshot_id": snapshot_id,
    }
    snapshot = DatabaseSnapshot(
        registry_rows=(
            {
                "scheme_id": f"{prediction_id}__h5__5Y",
                "base_scheme_id": prediction_id,
                "horizon": 5,
                "task_type": "T+5",
                "runtime_type": "blackbox_v2",
                "frequency": "daily",
                "target_tenor": "5Y",
                "status": "active",
            },
        ),
        prediction_rows=(
            {
                "id": 1,
                "run_id": None,
                "backtest_run_id": 7,
                "scheme_version": "product-exact",
                "scheme_id": prediction_id,
                "target_tenor": "5Y",
                "horizon": 5,
                "predict_date": "2026-05-19",
                "feature_date": "2026-05-19",
                "target_date": "2026-05-26",
                "predicted_direction": 1,
                "backtest_actual_direction": -1,
            },
        ),
        actual_rows=(),
        live_runs=(),
        backtest_runs=(
            {
                "reference_id": 7,
                "scheme_id": source_id,
                "status": "success",
                "code_hash": "a" * 64,
                "config_hash": "b" * 64,
                "input_artifact_hash": snapshot_id,
                "summary": summary,
                "source_registry_scopes": [
                    {
                        "target_tenor": "5Y",
                        "horizon": 5,
                        "status": "archived",
                    }
                ],
            },
        ),
        calendar_rows=_calendar_rows(date(2026, 5, 1), date(2026, 6, 30)),
        digest="approved-cross-source",
        backtest_prediction_rows=(
            {
                "run_id": 7,
                "scheme_id": source_id,
                "target_tenor": "5Y",
                "horizon": 5,
                "predict_date": "2026-05-19",
                "feature_date": "2026-05-19",
                "target_date": "2026-05-26",
                "label": -1,
                "predicted_direction": 1,
            },
            {
                "run_id": 7,
                "scheme_id": source_id,
                "target_tenor": "5Y",
                "horizon": 5,
                "predict_date": "2026-05-20",
                "feature_date": "2026-05-20",
                "target_date": "2026-05-27",
                "label": 1,
                "predicted_direction": -1,
            },
        ),
        version_rows=(product_version, source_version),
    )
    relationship = HistoricalBacktestReference(
        prediction_id,
        source_id,
        "fixture",
    )

    completeness_errors, _details = validate_snapshot_completeness(
        snapshot,
        display_until="2026-06-30",
        historical_references=frozenset({relationship}),
    )
    lineage_errors, lineage = validate_snapshot_lineage(
        snapshot,
        historical_references=frozenset({relationship}),
    )

    assert completeness_errors == []
    assert lineage_errors == []
    assert lineage["authority_gaps"] == []

    active_source = {**source_version, "status": "active"}
    unrelated_retired = {
        **source_version,
        "scheme_version": "other-retired-exact",
    }
    wrong_status_snapshot = replace(
        snapshot,
        version_rows=(
            product_version,
            active_source,
            unrelated_retired,
        ),
    )
    with pytest.raises(
        DataConsistencyError,
        match="lacks retired source evidence",
    ):
        validate_and_join_facts(
            wrong_status_snapshot,
            display_until="2026-06-30",
            historical_references=frozenset({relationship}),
        )
    wrong_lineage_errors, _wrong_lineage = validate_snapshot_lineage(
        wrong_status_snapshot,
        historical_references=frozenset({relationship}),
    )
    assert any(
        "historical backtest source exact is not retired" in error
        for error in wrong_lineage_errors
    )


def _sha256_json(value: object) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _legacy_migration_snapshot() -> tuple[
    DatabaseSnapshot,
    LegacyPredictionMigration,
    LegacyCorrectedExactEvidence,
]:
    scheme_id = "legacy_scheme"
    fact = {
        "scheme_id": scheme_id,
        "target_tenor": "5Y",
        "horizon": 5,
        "predict_date": "2026-05-19",
        "feature_date": "2026-05-19",
        "target_date": "2026-05-25",
        "predicted_direction": 1,
        "actual_direction": -1,
    }
    summary = {
        "raw_row_count": 1,
        "row_count": 1,
        "evaluation_filter": {"included_row_count": 1},
    }
    mismatch = {
        "predict_date": "2026-05-19",
        "feature_date": "2026-05-19",
        "target_date": "2026-05-25",
        "expected_target_date": "2026-05-26",
    }
    corrected_fact = {
        **fact,
        "scheme_id": f"{scheme_id}_bbv2",
        "target_date": "2026-05-26",
    }
    snapshot = DatabaseSnapshot(
        registry_rows=(
            {
                "scheme_id": f"{scheme_id}__h5__5Y",
                "base_scheme_id": scheme_id,
                "horizon": 5,
                "task_type": "T+5",
                "runtime_type": "blackbox_v2",
                "frequency": "daily",
                "target_tenor": "5Y",
                "status": "active",
            },
        ),
        prediction_rows=(
            {
                "id": 1,
                "run_id": None,
                "backtest_run_id": 7,
                "scheme_version": None,
                "scheme_id": scheme_id,
                "target_tenor": "5Y",
                "horizon": 5,
                "predict_date": "2026-05-19",
                "feature_date": "2026-05-19",
                "target_date": "2026-05-25",
                "predicted_direction": 1,
                "backtest_actual_direction": -1,
            },
        ),
        actual_rows=(),
        live_runs=(),
        backtest_runs=(
            {
                "reference_id": 7,
                "scheme_id": scheme_id,
                "status": "success",
                "code_hash": None,
                "config_hash": None,
                "input_artifact_hash": "e" * 64,
                "summary": summary,
            },
        ),
        calendar_rows=_calendar_rows(date(2026, 5, 1), date(2026, 6, 30)),
        digest="legacy-migration-fixture",
        backtest_prediction_rows=(
            {
                "run_id": 7,
                "scheme_id": scheme_id,
                "target_tenor": "5Y",
                "horizon": 5,
                "predict_date": "2026-05-19",
                "feature_date": "2026-05-19",
                "target_date": "2026-05-25",
                "label": -1,
                "predicted_direction": 1,
            },
        ),
    )
    compatibility = LegacyPredictionMigration(
        prediction_scheme_id=scheme_id,
        historical_source_scheme_id=scheme_id,
        designated_source_scheme_id=f"{scheme_id}_bbv2",
        designated_exact="0123456789ab",
        designated_code_hash="a" * 64,
        designated_config_hash="b" * 64,
        designated_manifest_hash="c" * 64,
        designated_input_artifact_id="snapshot-" + "9" * 24,
        designated_corrected_facts_sha256=_sha256_json([corrected_fact]),
        target_tenor="5Y",
        horizon=5,
        expected_fact_count=1,
        input_artifact_hash="e" * 64,
        backtest_summary_sha256=_sha256_json(summary),
        source_facts_sha256=_sha256_json([fact]),
        product_facts_sha256=_sha256_json([fact]),
        target_contract_mismatch_count=1,
        target_contract_mismatch_sha256=_sha256_json([mismatch]),
        reason="fixture",
    )
    corrected = LegacyCorrectedExactEvidence(
        source_registry_scheme_id=f"{scheme_id}_bbv2__h5__5Y",
        source_scheme_id=f"{scheme_id}_bbv2",
        registry_status="archived",
        designated_exact="0123456789ab",
        runtime_type="blackbox_v2",
        version_status="retired",
        code_hash="a" * 64,
        config_hash="b" * 64,
        manifest_hash="c" * 64,
        input_artifact_id="snapshot-" + "9" * 24,
        backtest_status="success",
        persisted_prediction_count=1,
        corrected_facts=(LegacyCorrectedFact(**corrected_fact),),
        corrected_facts_sha256=_sha256_json([corrected_fact]),
    )
    return snapshot, compatibility, corrected


def test_precise_legacy_migration_accepts_designated_corrected_exact() -> None:
    snapshot, compatibility, corrected = _legacy_migration_snapshot()
    migrations = frozenset({compatibility})
    corrected_evidence = frozenset({corrected})

    completeness_errors, completeness = validate_snapshot_completeness(
        snapshot,
        display_until="2026-06-30",
        legacy_migrations=migrations,
        corrected_exact_evidence=corrected_evidence,
    )
    lineage_errors, lineage = validate_snapshot_lineage(
        snapshot,
        historical_references=frozenset(),
        legacy_migrations=migrations,
        corrected_exact_evidence=corrected_evidence,
    )
    facts = validate_and_join_facts(
        snapshot,
        display_until="2026-06-30",
        legacy_migrations=migrations,
        corrected_exact_evidence=corrected_evidence,
    )

    assert completeness_errors == []
    assert lineage_errors == []
    assert completeness["legacy_migration_fact_count"] == 1
    assert lineage["legacy_migration_fact_count"] == 1
    assert lineage["legacy_migrations"] == [
        {
            "prediction_scheme_id": "legacy_scheme",
            "designated_source_scheme_id": "legacy_scheme_bbv2",
            "designated_exact": "0123456789ab",
            "fact_count": 1,
            "target_contract_mismatch_count": 1,
        }
    ]
    assert len(facts) == 1


def test_legacy_migration_fails_closed_when_a_fact_drifts() -> None:
    snapshot, compatibility, corrected = _legacy_migration_snapshot()
    snapshot = replace(
        snapshot,
        prediction_rows=(
            {**snapshot.prediction_rows[0], "predicted_direction": -1},
        ),
    )

    with pytest.raises(DataConsistencyError, match="evidence drift"):
        validate_snapshot_lineage(
            snapshot,
            historical_references=frozenset(),
            legacy_migrations=frozenset({compatibility}),
            corrected_exact_evidence=frozenset({corrected}),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_scheme_id", "different_bbv2"),
        ("designated_exact", "fedcba987654"),
        ("registry_status", "active"),
        ("version_status", "active"),
        ("code_hash", "9" * 64),
        ("config_hash", "8" * 64),
        ("manifest_hash", "7" * 64),
        ("corrected_facts_sha256", "6" * 64),
    ],
)
def test_legacy_migration_rejects_designated_exact_evidence_drift(
    field: str,
    value: object,
) -> None:
    snapshot, compatibility, corrected = _legacy_migration_snapshot()
    corrected = replace(corrected, **{field: value})

    with pytest.raises(DataConsistencyError, match="evidence drift"):
        validate_snapshot_lineage(
            snapshot,
            historical_references=frozenset(),
            legacy_migrations=frozenset({compatibility}),
            corrected_exact_evidence=frozenset({corrected}),
        )


def test_legacy_target_exception_keeps_feature_trading_day_contract() -> None:
    calendar = _FrozenCalendar.from_rows(
        _calendar_rows(date(2026, 5, 1), date(2026, 6, 30))
    )

    with pytest.raises(DataConsistencyError, match="feature_date must be a trading day"):
        _validate_task_date_contract(
            task_type="T+5",
            horizon=5,
            predict_date="2026-05-17",
            feature_date="2026-05-17",
            target_date="2026-05-25",
            is_live=False,
            calendar=calendar,
            business_key=("legacy_scheme", "5Y", 5, "2026-05-25"),
            allow_legacy_target_date=True,
        )


@pytest.mark.parametrize(
    ("task_type", "predict_date", "feature_date", "target_date"),
    [
        ("T+1", "2026-06-10", "2026-06-09", "2026-06-11"),
        ("T+5", "2026-06-10", "2026-06-09", "2026-06-17"),
        ("weekly_point", "2026-06-13", "2026-06-12", "2026-06-26"),
        ("weekly_average", "2026-06-13", "2026-06-11", "2026-06-19"),
        ("monthly", "2026-06-15", "2026-06-12", "2026-07-15"),
        ("monthly_average", "2026-06-12", "2026-06-12", "2026-06-13"),
        ("quarterly_average", "2026-06-29", "2026-06-29", "2026-06-30"),
        ("annual_average", "2027-02-04", "2027-02-04", "2027-02-05"),
    ],
)
def test_fact_validation_rejects_ordered_dates_outside_task_business_contract(
    task_type: str,
    predict_date: str,
    feature_date: str,
    target_date: str,
) -> None:
    snapshot = _date_contract_snapshot(
        task_type,
        predict_date,
        feature_date,
        target_date,
    )

    with pytest.raises(DataConsistencyError, match="business contract"):
        validate_and_join_facts(snapshot, display_until="2027-03-31")


@pytest.mark.parametrize(
    ("task_type", "predict_date", "feature_date", "target_date"),
    [
        ("T+1", "2026-06-10", "2026-06-09", "2026-06-10"),
        ("T+5", "2026-06-10", "2026-06-09", "2026-06-16"),
        ("weekly_point", "2026-06-13", "2026-06-12", "2026-06-19"),
        ("weekly_average", "2026-06-13", "2026-06-12", "2026-06-19"),
        ("monthly", "2026-06-15", "2026-06-15", "2026-07-15"),
        ("monthly_average", "2026-06-15", "2026-06-15", "2026-06-16"),
        ("quarterly_average", "2026-06-30", "2026-06-30", "2026-07-01"),
        ("annual_average", "2027-02-05", "2027-02-05", "2027-02-06"),
    ],
)
def test_fact_validation_accepts_each_task_business_date_contract(
    task_type: str,
    predict_date: str,
    feature_date: str,
    target_date: str,
) -> None:
    snapshot = _date_contract_snapshot(
        task_type,
        predict_date,
        feature_date,
        target_date,
    )

    facts = validate_and_join_facts(snapshot, display_until="2027-03-31")

    assert len(facts) == 1
    assert facts[0].task_type == task_type


def test_daily_live_rejects_ordered_dates_when_predict_date_is_not_trading_day() -> None:
    snapshot = _date_contract_snapshot(
        "T+1",
        "2026-06-14",
        "2026-06-12",
        "2026-06-15",
    )

    with pytest.raises(DataConsistencyError, match="trading day"):
        validate_and_join_facts(snapshot, display_until="2026-06-30")


def test_daily_backtest_rejects_ordered_dates_when_feature_is_not_trading_day() -> None:
    snapshot = _date_contract_snapshot(
        "T+1",
        "2026-06-14",
        "2026-06-14",
        "2026-06-15",
        is_live=False,
    )

    with pytest.raises(DataConsistencyError, match="trading day"):
        validate_and_join_facts(snapshot, display_until="2026-06-30")


def test_monthly_backtest_keeps_natural_fifteenth_on_non_trading_day() -> None:
    snapshot = _date_contract_snapshot(
        "monthly",
        "2026-02-15",
        "2026-02-13",
        "2026-03-13",
        is_live=False,
    )

    facts = validate_and_join_facts(snapshot, display_until="2026-06-30")

    assert len(facts) == 1
    assert facts[0].predict_date == "2026-02-15"
    assert facts[0].feature_date == "2026-02-13"
    assert facts[0].target_date == "2026-03-13"


def test_weekly_live_rejects_ordered_dates_when_predict_date_is_not_saturday() -> None:
    snapshot = _date_contract_snapshot(
        "weekly_point",
        "2026-06-12",
        "2026-06-11",
        "2026-06-19",
    )

    with pytest.raises(DataConsistencyError, match="natural Saturday"):
        validate_and_join_facts(snapshot, display_until="2026-06-30")


def test_data_consistency_cli_accepts_repeated_selected_schemes() -> None:
    from harness.cli import _build_parser

    args = _build_parser().parse_args(
        [
            "gate",
            "data-consistency",
            "--scheme-id",
            BASE_SCHEME_ID,
            "--scheme-id",
            "other_scheme",
            "--session-fd",
            "4",
        ]
    )

    assert args.gate_name == "data-consistency"
    assert args.scheme_id == [BASE_SCHEME_ID, "other_scheme"]

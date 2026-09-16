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
    DataConsistencyAuthorityUnavailable,
    DataConsistencyError,
    DatabaseSnapshot,
    DataConsistencyGate,
    _read_actuals,
    aggregate_display_facts,
    validate_snapshot_completeness,
    validate_and_join_facts,
)
from harness.result import GateStatus
from shared.historical_reference_compatibility import HistoricalBacktestReference
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
                "'scheduled_live', 'artifact-a', '2026-06-10', 1), "
                "(12, :scheme_id, 'exact-a', 'native_adapter', 'success', "
                "'scheduled_live', 'artifact-b', '2026-06-11', 1)"
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
                "(:scheme_id, 'exact-a', :code_hash, :config_hash, "
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
    assert evidence["lineage_validated_fact_count"] == 0
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

    with pytest.raises(
        DataConsistencyAuthorityUnavailable,
        match="no durable backtest product reference",
    ):
        validate_snapshot_completeness(
            snapshot,
            display_until="2026-02-01",
        )


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
                "source_registry_status": "archived",
                "source_has_retired_version": True,
                "code_hash": "a" * 64,
                "config_hash": "b" * 64,
                "input_artifact_hash": "c" * 64,
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

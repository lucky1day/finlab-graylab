from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import RowMapping

from backend.factor_lab_dashboard_semantics import (
    DASHBOARD_SCHEMA_VERSION,
    ROW_FIELDS,
    DashboardDataError,
    choose_latest_backtest_runs,
    choose_live_prediction_rows,
    collapse_actual_facts,
    compact_detail_row,
    validate_dashboard_payload,
)
from tests.factor_lab_dashboard_conformance import (
    dashboard_v1_conformance_samples,
)


def _live_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 1,
        "scheme_id": "weekly_blackbox",
        "target_tenor": "CDB10Y",
        "horizon": 1,
        "predict_date": "2026-07-02",
        "feature_date": "2026-07-01",
        "target_date": "2026-07-10",
        "prediction_phase": "scheduled_live",
        "predicted_direction": 1,
        "actual_direction": None,
        "extra": {"frequency": "weekly"},
    }
    row.update(overrides)
    return row


def _minimal_payload() -> dict[str, object]:
    return {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "snapshot_id": "snapshot-test-1",
        "generated_at": "2026-07-22T12:00:00+08:00",
        "display_until": "2026-07-22",
        "stale": False,
        "snapshot_age_ms": 0,
        "row_fields": list(ROW_FIELDS),
        "target_labels": {"5Y": "5Y国债活跃"},
        "schemes": [
            {
                "scheme_id": "base__h1__5Y",
                "base_scheme_id": "base",
                "name": "完整方案",
                "description": "",
                "horizon": 1,
                "task_type": "T+1",
                "frequency": "daily",
                "target_tenor": "5Y",
                "target_label": "5Y国债活跃",
                "status": "active",
                "deployed_at": "2026-06-09",
                "live_rows": [
                    [
                        "2026-07-02",
                        "2026-07-01",
                        "2026-07-03",
                        "scheduled_live",
                        1,
                        None,
                    ]
                ],
                "backtest": None,
            }
        ],
    }


def _detail_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "predict_date": "2026-07-02",
        "feature_date": "2026-07-01",
        "target_date": "2026-07-03",
        "prediction_phase": "scheduled_live",
        "predicted_direction": 1,
        "actual_direction": 1,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "sample",
    dashboard_v1_conformance_samples(),
    ids=lambda sample: sample["name"],
)
def test_payload_validator_matches_shared_v1_conformance_corpus(
    sample: dict[str, object],
) -> None:
    if sample["valid"]:
        assert validate_dashboard_payload(sample["payload"]) is None  # type: ignore[arg-type]
        return

    with pytest.raises(DashboardDataError):
        validate_dashboard_payload(sample["payload"])  # type: ignore[arg-type]


def test_weekly_h1_prefers_latest_predict_date_then_latest_id() -> None:
    rows = [
        _live_row(id=1, feature_date="2026-07-01", predict_date="2026-07-02"),
        _live_row(id=2, feature_date="2026-07-02", predict_date="2026-07-03"),
        _live_row(id=3, feature_date="2026-07-02", predict_date="2026-07-02"),
        _live_row(id=4, feature_date="2026-07-02", predict_date="2026-07-02"),
    ]

    selected = choose_live_prediction_rows(rows, display_until="2026-07-31")

    # 名称由计划固定；legacy 规则在 feature 相同时优先更早获批日，再取较大 ID。
    assert [row["id"] for row in selected] == [4]


def test_canonical_accepts_row_mappings_and_isolates_four_dimensional_key() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    statement = text(
        """
        SELECT :id AS id, :scheme_id AS scheme_id,
               :target_tenor AS target_tenor, :horizon AS horizon,
               :predict_date AS predict_date, :feature_date AS feature_date,
               :target_date AS target_date, :extra AS extra
        """
    )
    candidates = [
        _live_row(id=1, extra='{"frequency":"weekly"}'),
        _live_row(
            id=6,
            predict_date="2026-07-03",
            extra='{"frequency":"weekly"}',
        ),
        _live_row(id=2, scheme_id="weekly_blackbox_v2", extra="{}"),
        _live_row(id=3, target_tenor="CDB5Y", extra="{}"),
        _live_row(id=4, horizon=2, extra="{}"),
        _live_row(id=5, target_date="2026-07-17", extra="{}"),
    ]
    with engine.connect() as conn:
        rows = [
            conn.execute(statement, candidate).mappings().one()
            for candidate in candidates
        ]

    selected = choose_live_prediction_rows(rows, display_until="2026-07-31")

    assert {row["id"] for row in selected} == {1, 2, 3, 4, 5}
    assert all(isinstance(row, RowMapping) for row in selected)


def test_future_predict_date_is_filtered_after_canonical_choice() -> None:
    rows = [
        _live_row(id=1, extra={}, predict_date="2026-07-02"),
        _live_row(id=2, extra={}, predict_date="2026-07-04"),
    ]

    selected = choose_live_prediction_rows(rows, display_until="2026-07-03")

    assert selected == []


def _registry_for_backtest_rank() -> list[dict[str, object]]:
    return [
        {
            "scheme_id": "base__h1__5Y",
            "base_scheme_id": "base",
            "runtime_type": "native_adapter",
            "target_tenor": "5Y",
            "horizon": 1,
            "status": "active",
        }
    ]


def _backtest_run_for_rank(
    *,
    run_id: int,
    updated_at: object,
) -> dict[str, object]:
    return {
        "id": run_id,
        "benchmark_id": "benchmark",
        "scheme_id": "base",
        "data_source": "framework_db_aligned",
        "status": "success",
        "updated_at": updated_at,
    }


@pytest.mark.parametrize(
    "naive_updated_at",
    [
        datetime(2026, 7, 22, 10, 0, 0),
        "2026-07-22 10:00:00",
    ],
    ids=["naive-datetime", "sql-naive-string"],
)
def test_backtest_rank_interprets_naive_timestamp_as_shanghai(
    naive_updated_at: object,
) -> None:
    runs = [
        _backtest_run_for_rank(run_id=1, updated_at=naive_updated_at),
        _backtest_run_for_rank(
            run_id=2,
            updated_at="2026-07-22T02:30:00+00:00",
        ),
    ]

    selected = choose_latest_backtest_runs(runs, _registry_for_backtest_rank())

    assert selected["base__h1__5Y"]["id"] == 2


def test_backtest_rank_normalizes_aware_timestamps_to_utc_before_id_tie_break() -> None:
    runs = [
        _backtest_run_for_rank(
            run_id=1,
            updated_at="2026-07-22T10:00:00+08:00",
        ),
        _backtest_run_for_rank(
            run_id=2,
            updated_at="2026-07-22T02:00:00+00:00",
        ),
    ]

    selected = choose_latest_backtest_runs(runs, _registry_for_backtest_rank())

    assert selected["base__h1__5Y"]["id"] == 2


def test_backtest_rank_uses_real_instant_for_mixed_timezones() -> None:
    runs = [
        _backtest_run_for_rank(
            run_id=1,
            updated_at="2026-07-22T10:00:00+08:00",
        ),
        _backtest_run_for_rank(
            run_id=2,
            updated_at="2026-07-22T03:00:00+00:00",
        ),
    ]

    selected = choose_latest_backtest_runs(runs, _registry_for_backtest_rank())

    assert selected["base__h1__5Y"]["id"] == 2


@pytest.mark.parametrize(
    "invalid_updated_at",
    ["not-a-timestamp", " 2026-07-22 10:00:00 "],
    ids=["unparseable", "padded"],
)
def test_backtest_rank_rejects_invalid_nonempty_timestamp(
    invalid_updated_at: str,
) -> None:
    runs = [
        _backtest_run_for_rank(run_id=1, updated_at=invalid_updated_at),
    ]

    with pytest.raises(DashboardDataError, match="updated_at is invalid"):
        choose_latest_backtest_runs(runs, _registry_for_backtest_rank())


def test_duplicate_actuals_with_same_direction_collapse() -> None:
    same = [
        {
            "target_date": "2026-07-01",
            "target_tenor": "CDB10Y",
            "target_rule": "close",
            "actual_direction": 1,
        },
        {
            "target_date": "2026-07-01",
            "target_tenor": "CDB10Y",
            "target_rule": "close",
            "actual_direction": 1,
        },
    ]

    collapsed = collapse_actual_facts(same, fact_name="monthly actuals")

    assert collapsed == {("CDB10Y", "2026-07-01", "close"): 1}


def test_conflicting_actuals_fail_closed() -> None:
    same = [
        {
            "target_date": "2026-07-01",
            "target_tenor": "CDB10Y",
            "target_rule": "close",
            "actual_direction": 1,
        },
        {
            "target_date": "2026-07-01",
            "target_tenor": "CDB10Y",
            "target_rule": "close",
            "actual_direction": 1,
        },
    ]
    conflict = same + [
        {
            "target_date": "2026-07-01",
            "target_tenor": "CDB10Y",
            "target_rule": "close",
            "actual_direction": -1,
        }
    ]

    with pytest.raises(DashboardDataError, match="monthly actuals"):
        collapse_actual_facts(conflict, fact_name="monthly actuals")


def test_compact_detail_preserves_feature_date_and_pending_actual() -> None:
    compact = compact_detail_row(
        {
            "predict_date": date(2026, 7, 2),
            "feature_date": date(2026, 7, 1),
            "target_date": date(2026, 7, 3),
            "prediction_phase": "gray_live",
            "predicted_direction": -1,
            "actual_direction": None,
        },
        source="live",
    )

    assert compact == [
        "2026-07-02",
        "2026-07-01",
        "2026-07-03",
        "gray_live",
        -1,
        None,
    ]
    assert len(compact) == 6


@pytest.mark.parametrize(
    ("source", "overrides"),
    [
        pytest.param("live", {"prediction_phase": ""}, id="empty-live-phase"),
        pytest.param(
            "live",
            {"prediction_phase": "preview_live"},
            id="invalid-live-phase",
        ),
        pytest.param(
            "backtest",
            {"prediction_phase": "scheduled_live"},
            id="nonempty-backtest-phase",
        ),
        pytest.param(
            "live",
            {"predicted_direction": 2},
            id="invalid-predicted-direction",
        ),
        pytest.param(
            "live",
            {"actual_direction": 2},
            id="invalid-live-actual-direction",
        ),
        pytest.param(
            "backtest",
            {"prediction_phase": None, "actual_direction": None},
            id="pending-backtest-actual",
        ),
    ],
)
def test_compact_detail_rejects_invalid_phase_or_direction(
    source: str,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(DashboardDataError):
        compact_detail_row(_detail_row(**overrides), source=source)


def test_payload_rejects_unknown_task_type_or_row_width() -> None:
    valid_payload = _minimal_payload()
    assert validate_dashboard_payload(valid_payload) is None

    unknown_task = deepcopy(valid_payload)
    unknown_task["schemes"][0]["task_type"] = "weekly"  # type: ignore[index]
    with pytest.raises(DashboardDataError, match="task_type"):
        validate_dashboard_payload(unknown_task)

    wrong_width = deepcopy(valid_payload)
    wrong_width["schemes"][0]["live_rows"][0] = ["2026-07-02"]  # type: ignore[index]
    with pytest.raises(DashboardDataError, match="width"):
        validate_dashboard_payload(wrong_width)


def test_payload_rejects_unbounded_python_integer_as_contract_error() -> None:
    payload = _minimal_payload()
    payload["snapshot_age_ms"] = 10**1000

    with pytest.raises(DashboardDataError, match="snapshot_age_ms"):
        validate_dashboard_payload(payload)


@pytest.mark.parametrize(
    "field",
    [
        "snapshot_id",
        "scheme_id",
        "base_scheme_id",
        "name",
        "task_type",
        "frequency",
        "target_tenor",
        "target_label",
        "benchmark_id",
        "benchmark_label",
        "data_source",
        "data_source_label",
    ],
)
def test_payload_rejects_noncanonical_padded_structured_strings(field: str) -> None:
    payload = _minimal_payload()
    scheme = payload["schemes"][0]  # type: ignore[index]
    if field == "snapshot_id":
        payload[field] = " snapshot-test-1 "
    elif field in {
        "scheme_id",
        "base_scheme_id",
        "name",
        "task_type",
        "frequency",
        "target_tenor",
        "target_label",
    }:
        scheme[field] = f" {scheme[field]} "
    else:
        scheme["backtest"] = {
            "benchmark_id": "benchmark",
            "benchmark_label": "benchmark label",
            "data_source": "framework_db_aligned",
            "data_source_label": "当前DB对齐回测",
            "latest_run_date": "2026-05-31",
            "rows": [],
        }
        backtest = scheme["backtest"]
        backtest[field] = f" {backtest[field]} "

    with pytest.raises(DashboardDataError, match=field):
        validate_dashboard_payload(payload)


def test_payload_description_remains_free_text() -> None:
    payload = _minimal_payload()
    payload["schemes"][0]["description"] = "  deliberate spacing  "  # type: ignore[index]

    assert validate_dashboard_payload(payload) is None


def test_payload_rejects_task1_minimal_shape_without_v1_top_level() -> None:
    payload = {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "row_fields": list(ROW_FIELDS),
        "schemes": [
            {
                "task_type": "T+1",
                "live_rows": [],
                "backtest": None,
            }
        ],
    }

    with pytest.raises(DashboardDataError, match="snapshot_id"):
        validate_dashboard_payload(payload)


def test_payload_requires_explicit_backtest_key_for_every_scheme() -> None:
    payload = _minimal_payload()
    del payload["schemes"][0]["backtest"]  # type: ignore[index]

    with pytest.raises(DashboardDataError, match="backtest"):
        validate_dashboard_payload(payload)


@pytest.mark.parametrize("source", ["live", "backtest"])
def test_payload_detail_dates_must_be_iso_strings(source: str) -> None:
    payload = _minimal_payload()
    scheme = payload["schemes"][0]  # type: ignore[index]
    if source == "live":
        scheme["live_rows"][0][0] = date(2026, 7, 2)
    else:
        scheme["backtest"] = {
            "benchmark_id": "benchmark",
            "benchmark_label": "benchmark",
            "data_source": "framework_db_aligned",
            "data_source_label": "当前DB对齐回测",
            "latest_run_date": "2026-05-31",
            "rows": [
                [
                    date(2026, 5, 20),
                    "2026-05-20",
                    "2026-05-27",
                    None,
                    1,
                    1,
                ]
            ],
        }

    with pytest.raises(DashboardDataError, match="ISO date string"):
        validate_dashboard_payload(payload)


@pytest.mark.parametrize("unknown_field", ["internal_metrics", "artifact_uri"])
def test_payload_backtest_rejects_unknown_fields(unknown_field: str) -> None:
    payload = _minimal_payload()
    scheme = payload["schemes"][0]  # type: ignore[index]
    scheme["backtest"] = {
        "benchmark_id": "benchmark",
        "benchmark_label": "benchmark",
        "data_source": "framework_db_aligned",
        "data_source_label": "当前DB对齐回测",
        "latest_run_date": "2026-05-31",
        "rows": [],
        unknown_field: {},
    }

    with pytest.raises(DashboardDataError, match="unknown fields"):
        validate_dashboard_payload(payload)


@pytest.mark.parametrize(
    ("scope", "unknown_field"),
    [("top", "runtime_metrics"), ("scheme", "runtime_type")],
)
def test_payload_public_objects_reject_unknown_fields(
    scope: str,
    unknown_field: str,
) -> None:
    payload = _minimal_payload()
    if scope == "top":
        payload[unknown_field] = {}
    else:
        payload["schemes"][0][unknown_field] = "internal"  # type: ignore[index]

    with pytest.raises(DashboardDataError, match="unknown fields"):
        validate_dashboard_payload(payload)

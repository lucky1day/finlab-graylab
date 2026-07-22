from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import RowMapping

from backend.factor_lab_dashboard_semantics import (
    DASHBOARD_SCHEMA_VERSION,
    ROW_FIELDS,
    DashboardDataError,
    choose_live_prediction_rows,
    collapse_actual_facts,
    compact_detail_row,
    validate_dashboard_payload,
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
        "row_fields": list(ROW_FIELDS),
        "schemes": [
            {
                "task_type": "T+1",
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

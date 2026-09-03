from __future__ import annotations

from copy import deepcopy

from migrations.scheme_prediction_fact_024 import (
    classify_scheme_prediction_fact_state,
)


def _legacy_rows(*, filled: bool) -> list[dict[str, object]]:
    return [
        {
            "run_id": run_id,
            "scheme_id": "t5_daily",
            "scheme_version": "e257093fb4f3" if filled else None,
            "predict_date": predict_date,
            "started_at": "2026-06-10 11:52:43",
            "prediction_count": 4,
            "null_prediction_versions": 0 if filled else 4,
            "mismatched_prediction_rows": 0,
        }
        for run_id, predict_date in (
            (15, "2026-06-05"),
            (16, "2026-06-08"),
            (17, "2026-06-09"),
        )
    ]


def _source_state() -> dict[str, object]:
    return {
        "columns": {
            "run_id": {},
            "prediction_phase": {},
            "updated_at": {},
            "created_at": {},
        },
        "indexes": {
            "idx_scheme_predictions_phase": (
                "scheme_id",
                "prediction_phase",
                "predict_date",
            )
        },
        "foreign_keys": {},
        "checks": {},
        "invalid_source_rows": 0,
        "live_rows_without_version": 12,
        "live_rows_with_backtest_actual": 0,
        "invalid_backtest_actual_rows": 0,
        "extra_phase_rows": 3941,
        "missing_canonical_rows": -1,
        "orphan_run_count": 0,
        "invalid_selected_rows": 0,
        "duplicate_selected_keys": 0,
        "mismatched_selected_scheme_rows": 0,
        "invalid_active_runtime_count": 0,
        "legacy_rows": _legacy_rows(filled=False),
    }


def _complete_state() -> dict[str, object]:
    return {
        "columns": {
            "run_id": {},
            "backtest_run_id": {
                "column_type": "bigint",
                "nullable": "YES",
            },
            "backtest_actual_direction": {
                "column_type": "tinyint",
                "nullable": "YES",
            },
            "created_at": {},
        },
        "indexes": {
            "idx_scheme_predictions_backtest_run": ("backtest_run_id",)
        },
        "foreign_keys": {
            "fk_scheme_predictions_run": {
                "column": "run_id",
                "referenced_table": "t_scheme_runs",
                "referenced_column": "run_id",
                "delete_rule": "RESTRICT",
                "update_rule": "NO ACTION",
            },
            "fk_scheme_predictions_backtest_run": {
                "column": "backtest_run_id",
                "referenced_table": "t_backtest_runs",
                "referenced_column": "id",
                "delete_rule": "RESTRICT",
                "update_rule": "NO ACTION",
            },
        },
        "checks": {
            "ck_scheme_predictions_source": {
                "enforced": True,
                "clause": (
                    "run_idisnotnullbacktest_run_idisnull"
                    "backtest_actual_directionisnullrun_idisnull"
                    "backtest_run_idisnotnull"
                    "backtest_actual_directionin(-(1),0,1)"
                ),
            }
        },
        "invalid_source_rows": 0,
        "live_rows_without_version": 0,
        "live_rows_with_backtest_actual": 0,
        "invalid_backtest_actual_rows": 0,
        "extra_phase_rows": 0,
        "missing_canonical_rows": 0,
        "orphan_run_count": 0,
        "invalid_selected_rows": 0,
        "duplicate_selected_keys": 0,
        "mismatched_selected_scheme_rows": 0,
        "invalid_active_runtime_count": 0,
        "legacy_rows": _legacy_rows(filled=True),
    }


def test_024_source_and_complete_states_are_accepted() -> None:
    assert classify_scheme_prediction_fact_state(_source_state()) == (
        "COMPATIBLE_PARTIAL"
    )
    assert classify_scheme_prediction_fact_state(_complete_state()) == "COMPLETE"


def test_024_partial_state_is_replayable() -> None:
    state = _source_state()
    state["columns"]["backtest_run_id"] = {
        "column_type": "bigint",
        "nullable": "YES",
    }

    assert classify_scheme_prediction_fact_state(state) == "COMPATIBLE_PARTIAL"


def test_024_rejects_orphan_source_and_schema_drift() -> None:
    orphaned = _complete_state()
    orphaned["orphan_run_count"] = 1
    assert classify_scheme_prediction_fact_state(orphaned) == "UNSAFE"

    drifted = deepcopy(_complete_state())
    drifted["foreign_keys"]["fk_scheme_predictions_run"][
        "delete_rule"
    ] = "CASCADE"
    assert classify_scheme_prediction_fact_state(drifted) == "UNSAFE"


def test_024_rejects_incomplete_or_cross_source_actuals() -> None:
    missing = _complete_state()
    missing["missing_canonical_rows"] = 1
    assert classify_scheme_prediction_fact_state(missing) == "COMPATIBLE_PARTIAL"

    live_actual = _complete_state()
    live_actual["live_rows_with_backtest_actual"] = 1
    assert classify_scheme_prediction_fact_state(live_actual) == "UNSAFE"


def test_024_phase_extra_cleanup_remains_replayable() -> None:
    state = _complete_state()
    state["extra_phase_rows"] = 1

    assert classify_scheme_prediction_fact_state(state) == "COMPATIBLE_PARTIAL"


def test_024_rejects_missing_backtest_actual_direction() -> None:
    state = _complete_state()
    state["invalid_backtest_actual_rows"] = 1

    assert classify_scheme_prediction_fact_state(state) == "UNSAFE"


def test_024_rejects_mismatched_legacy_prediction_identity() -> None:
    state = _source_state()
    state["legacy_rows"][0]["mismatched_prediction_rows"] = 1

    assert classify_scheme_prediction_fact_state(state) == "UNSAFE"


def test_024_rejects_legacy_row_without_run_source() -> None:
    state = _source_state()
    state["invalid_source_rows"] = 1

    assert classify_scheme_prediction_fact_state(state) == "UNSAFE"


def test_024_rejects_selected_backtest_parent_child_scheme_mismatch() -> None:
    state = _source_state()
    state["mismatched_selected_scheme_rows"] = 1

    assert classify_scheme_prediction_fact_state(state) == "UNSAFE"

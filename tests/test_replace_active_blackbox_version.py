from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from shared.models import PredictionRecord


class _Result:
    def __init__(self, rows: list[dict] | None = None, *, rowcount: int = 1) -> None:
        self.rows = rows or []
        self.rowcount = rowcount

    def mappings(self) -> "_Result":
        return self

    def one_or_none(self) -> dict | None:
        if len(self.rows) > 1:
            raise AssertionError("expected at most one row")
        return self.rows[0] if self.rows else None

    def all(self) -> list[dict]:
        return list(self.rows)


class _Connection:
    def __init__(self, state: dict) -> None:
        self.state = state

    def execute(self, statement, params=None) -> _Result:
        sql = " ".join(str(statement).split())
        values = params or {}
        self.state.setdefault("sql", []).append((sql, values))
        if "active replacement latest passed all-stage fence" in sql:
            rows = [
                row
                for row in self.state["harness_runs"]
                if row["scheme_id"] == values["scheme_id"]
                and row["scheme_version"] == values["scheme_version"]
                and row["stage"] == "all"
                and row["status"] == "passed"
            ]
            rows.sort(
                key=lambda row: (row["finished_at"], row["harness_run_id"]),
                reverse=True,
            )
            return _Result(rows[:1])
        if "active replacement version set" in sql:
            return _Result(
                [
                    dict(row)
                    for row in self.state["versions"]
                    if row["scheme_id"] == values["scheme_id"]
                ]
            )
        if "SELECT scheme_id, scheme_version, runtime_type" in sql and (
            "FROM t_scheme_versions" in sql
        ):
            rows = [
                dict(row)
                for row in self.state["versions"]
                if row["scheme_id"] == values["scheme_id"]
                and row["scheme_version"] == values["scheme_version"]
            ]
            return _Result(rows)
        if "SELECT scheme_id, base_scheme_id, name, description" in sql and (
            "FROM t_scheme_registry" in sql
        ):
            return _Result([dict(row) for row in self.state["registry"]])
        if "INSERT INTO t_scheme_versions" in sql:
            existing = next(
                (
                    row
                    for row in self.state["versions"]
                    if row["scheme_id"] == values["scheme_id"]
                    and row["scheme_version"] == values["scheme_version"]
                ),
                None,
            )
            incoming = {
                key: values.get(key)
                for key in (
                    "scheme_id",
                    "scheme_version",
                    "runtime_type",
                    "algorithm_version",
                    "contract_version",
                    "runtime_profile",
                    "environment_fingerprint",
                    "data_snapshot_id",
                    "code_hash",
                    "config_hash",
                    "manifest_hash",
                    "git_commit",
                    "status",
                    "created_by",
                    "approved_by",
                    "approved_at",
                )
            }
            if existing is None:
                self.state["versions"].append(incoming)
            else:
                existing.update(incoming)
            return _Result()
        if "active replacement retire previous version" in sql:
            row = next(
                (
                    row
                    for row in self.state["versions"]
                    if row["scheme_id"] == values["scheme_id"]
                    and row["scheme_version"] == values["previous_scheme_version"]
                    and row["status"] == "active"
                ),
                None,
            )
            if row is None:
                return _Result(rowcount=0)
            row["status"] = "retired"
            return _Result(rowcount=1)
        raise AssertionError(f"unexpected SQL: {sql}")


class _Begin:
    def __init__(self, engine: "_Engine") -> None:
        self.engine = engine
        self.staged = deepcopy(engine.state)

    def __enter__(self) -> _Connection:
        return _Connection(self.staged)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self.engine.state = self.staged


class _Engine:
    def __init__(self, *, active_versions: tuple[str, ...] = ("old-version",)) -> None:
        self.state = {
            "harness_runs": [
                {
                    "harness_run_id": "hr-new",
                    "scheme_id": "demo_blackbox",
                    "scheme_version": "new-version",
                    "stage": "all",
                    "status": "passed",
                    "finished_at": datetime(2026, 7, 30, 10, 0),
                }
            ],
            "versions": [
                _version_row(version, "active") for version in active_versions
            ],
            "registry": [_registry_row()],
            "sql": [],
        }

    def begin(self) -> _Begin:
        return _Begin(self)


def _version_row(version: str, status: str) -> dict:
    return {
        "scheme_id": "demo_blackbox",
        "scheme_version": version,
        "runtime_type": "blackbox_v2",
        "algorithm_version": "1.0.0",
        "contract_version": "1.0",
        "runtime_profile": "blackbox-v2-v1",
        "environment_fingerprint": "e" * 64,
        "data_snapshot_id": "snapshot-old",
        "code_hash": "o" * 64,
        "config_hash": "f" * 64,
        "manifest_hash": "m" * 64,
        "git_commit": None,
        "status": status,
        "created_by": "test",
        "approved_by": "previous-owner",
        "approved_at": datetime(2026, 7, 29, 8, 0),
    }


def _registry_row() -> dict:
    return {
        "scheme_id": "demo_blackbox__h1__10Y",
        "base_scheme_id": "demo_blackbox",
        "name": "Demo Blackbox",
        "description": "demo",
        "horizon": 1,
        "task_type": "T+1",
        "runtime_type": "blackbox_v2",
        "tenors": ["10Y"],
        "frequency": "daily",
        "target_tenor": "10Y",
        "schedule_cron": "3 7 * * 1-5",
        "schedule_timezone": "Asia/Shanghai",
        "status": "active",
    }


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id="demo_blackbox",
        scheme_version="new-version",
        runtime_type="blackbox_v2",
        status="active",
        version_status="active",
        environment_fingerprint="n" * 64,
        data_snapshot_id="snapshot-new",
        algorithm_version="1.0.0",
        contract_version="1.0",
        runtime_profile="blackbox-v2-v1",
        code_hash="n" * 64,
        config_hash="f" * 64,
        manifest_hash="m" * 64,
        name="Demo Blackbox",
        description="demo",
        horizon=1,
        task_type="T+1",
        tenors=("10Y",),
        frequency="daily",
        schedule=SimpleNamespace(
            cron="3 7 * * 1-5",
            timezone="Asia/Shanghai",
        ),
    )


def test_replacement_activates_new_version_and_retires_previous_atomically() -> None:
    from scheduler.repository import replace_active_blackbox_version

    engine = _Engine()
    approved_at = datetime(2026, 7, 30, 10, 30)

    state = replace_active_blackbox_version(
        engine,
        _config(),
        previous_scheme_version="old-version",
        expected_harness_run_id="hr-new",
        approved_by="replacement-owner",
        approved_at=approved_at,
    )

    statuses = {
        row["scheme_version"]: row["status"] for row in engine.state["versions"]
    }
    assert statuses == {"old-version": "retired", "new-version": "active"}
    new_row = next(
        row
        for row in engine.state["versions"]
        if row["scheme_version"] == "new-version"
    )
    assert new_row["approved_by"] == "replacement-owner"
    assert new_row["approved_at"] == approved_at
    assert {row["status"] for row in engine.state["registry"]} == {"active"}
    assert state.scheme_version == "new-version"
    assert state.version_status == "active"
    assert state.registry_status == "active"


def test_replacement_rejects_more_than_one_active_previous_version_without_writes() -> None:
    from scheduler.repository import replace_active_blackbox_version

    engine = _Engine(active_versions=("old-version", "unexpected-active"))
    before = deepcopy(engine.state)

    with pytest.raises(ValueError, match="exactly one active previous version"):
        replace_active_blackbox_version(
            engine,
            _config(),
            previous_scheme_version="old-version",
            expected_harness_run_id="hr-new",
            approved_by="replacement-owner",
            approved_at=datetime(2026, 7, 30, 10, 30),
        )

    assert engine.state == before


def test_replacement_rejects_stale_all_stage_fence_without_writes() -> None:
    from scheduler.repository import replace_active_blackbox_version

    engine = _Engine()
    before = deepcopy(engine.state)

    with pytest.raises(RuntimeError, match="latest passed all-stage harness run changed"):
        replace_active_blackbox_version(
            engine,
            _config(),
            previous_scheme_version="old-version",
            expected_harness_run_id="hr-stale",
            approved_by="replacement-owner",
            approved_at=datetime(2026, 7, 30, 10, 30),
        )

    assert engine.state == before


def test_controlled_replacement_dry_run_does_not_call_repository() -> None:
    from scripts.replace_active_blackbox_version import (
        replace_active_blackbox_version_controlled,
    )

    engine = Mock()
    config = _config()
    passed = SimpleNamespace(
        harness_run_id="hr-new",
        environment_fingerprint="n" * 64,
        data_snapshot_id="snapshot-new",
        generation_id="generation-new",
    )
    with (
        patch(
            "scripts.replace_active_blackbox_version.load_scheme_config",
            return_value=config,
        ),
        patch(
            "scripts.replace_active_blackbox_version._verify_passed_all",
            return_value=passed,
        ),
        patch(
            "scripts.replace_active_blackbox_version."
            "_replacement_database_snapshot",
            return_value={
                "versions": [{"scheme_version": "old-version", "status": "active"}],
                "registry": [{"status": "active"}],
            },
        ),
        patch(
            "scripts.replace_active_blackbox_version."
            "replace_active_blackbox_version"
        ) as replace,
    ):
        summary = replace_active_blackbox_version_controlled(
            engine,
            project_root=".",
            scheme_id="demo_blackbox",
            previous_scheme_version="old-version",
            expected_new_scheme_version="new-version",
            expected_harness_run_id="hr-new",
            approved_by="replacement-owner",
            apply=False,
        )

    assert summary["applied"] is False
    assert summary["new_scheme_version"] == "new-version"
    assert summary["previous_scheme_version"] == "old-version"
    assert summary["harness_run_id"] == "hr-new"
    replace.assert_not_called()


def test_gray_prediction_replacement_commits_new_run_and_removes_old_audit() -> None:
    from scheduler.repository import replace_approved_blackbox_gray_prediction

    calls: list[tuple[str, object]] = []

    class Connection:
        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            calls.append((sql, params))
            if "gray replacement old prediction" in sql:
                return _Result(
                    [
                        {
                            "id": 91,
                            "run_id": 41,
                            "scheme_version": "old-version",
                            "prediction_phase": "gray_live",
                        }
                    ]
                )
            if "gray replacement serving references" in sql:
                return _ScalarResult(0)
            if "gray replacement schedule references" in sql:
                return _ScalarResult(0)
            if "gray replacement delete old prediction" in sql:
                return _Result(rowcount=1)
            if "gray replacement delete old run log" in sql:
                return _Result(rowcount=1)
            if "gray replacement delete old run" in sql:
                return _Result(rowcount=1)
            raise AssertionError(f"unexpected SQL: {sql}")

    @contextmanager
    def approved_transaction(*args, **kwargs):
        del args, kwargs
        yield Connection(), [record], "new-version"

    record = PredictionRecord(
        scheme_id="demo_blackbox",
        target_tenor="10Y",
        horizon=1,
        predict_date="2026-07-25",
        feature_date="2026-07-24",
        target_date="2026-07-31",
        predicted_direction=1,
        prediction_phase="gray_live",
        extra={"data_snapshot_id": "snapshot-new"},
    )
    old_run = {
        "run_id": 41,
        "scheme_id": "demo_blackbox",
        "scheme_version": "old-version",
        "runtime_type": "blackbox_v2",
        "run_type": "active",
        "prediction_phase": "gray_live",
        "predict_date": "2026-07-25",
        "status": "success",
        "schedule_item_id": None,
        "attempt_no": None,
        "trigger_origin": None,
        "execution_token": None,
        "process_id": None,
        "process_group_id": None,
    }
    with (
        patch(
            "scheduler.repository._approved_blackbox_write_transaction",
            side_effect=approved_transaction,
        ),
        patch(
            "scheduler.repository._read_schedule_run_conn",
            return_value=old_run,
        ),
        patch(
            "scheduler.repository._create_scheme_run_conn",
            return_value=51,
        ) as create_run,
        patch(
            "scheduler.repository._insert_run_predictions_conn",
            return_value=1,
        ) as insert_prediction,
        patch("scheduler.repository._finish_scheme_run_conn") as finish_run,
        patch("scheduler.repository._write_run_log_conn") as write_log,
    ):
        new_run_id = replace_approved_blackbox_gray_prediction(
            Mock(),
            _config(),
            previous_scheme_version="old-version",
            previous_run_id=41,
            record=record,
            harness_run_id="hr-new",
            duration_sec=1.5,
        )

    assert new_run_id == 51
    create_run.assert_called_once()
    insert_prediction.assert_called_once()
    finish_run.assert_called_once()
    write_log.assert_called_once()
    sql = "\n".join(item[0] for item in calls)
    assert "gray replacement delete old prediction" in sql
    assert "gray replacement delete old run log" in sql
    assert "gray replacement delete old run" in sql


def test_controlled_gray_replacement_dry_run_computes_without_database_writes() -> None:
    from scripts.replace_blackbox_gray_history import (
        replace_blackbox_gray_history_controlled,
    )

    record = PredictionRecord(
        scheme_id="demo_blackbox",
        target_tenor="10Y",
        horizon=1,
        predict_date="2026-07-25",
        feature_date="2026-07-24",
        target_date="2026-07-31",
        predicted_direction=1,
        prediction_phase="gray_live",
        extra={"data_snapshot_id": "snapshot-new"},
    )
    old_rows = [
        {
            "prediction_id": 91,
            "run_id": 41,
            "predict_date": "2026-07-25",
            "feature_date": "2026-07-24",
            "target_date": "2026-07-31",
            "predicted_direction": 1,
        }
    ]
    passed = SimpleNamespace(harness_run_id="hr-new")
    with (
        patch(
            "scripts.replace_blackbox_gray_history.load_scheme_config",
            return_value=_config(),
        ),
        patch(
            "scripts.replace_blackbox_gray_history._verify_passed_all",
            return_value=passed,
        ),
        patch(
            "scripts.replace_blackbox_gray_history.read_blackbox_current_state",
            return_value={
                "generation_id": "generation-new",
                "refresh_date": "2026-07-30",
            },
        ),
        patch(
            "scripts.replace_blackbox_gray_history._load_old_gray_rows",
            return_value=old_rows,
        ),
        patch(
            "scripts.replace_blackbox_gray_history.run_configured_scheme",
            return_value=[record],
        ),
        patch(
            "scripts.replace_blackbox_gray_history._normalize_live_records",
            return_value=[record],
        ),
        patch(
            "scripts.replace_blackbox_gray_history."
            "replace_approved_blackbox_gray_prediction"
        ) as replace_gray,
    ):
        summary = replace_blackbox_gray_history_controlled(
            Mock(),
            project_root=".",
            scheme_id="demo_blackbox",
            previous_scheme_version="old-version",
            expected_new_scheme_version="new-version",
            expected_harness_run_id="hr-new",
            expected_count=1,
            algo_env="forecast_env_blackbox_v1",
            timeout_sec=600,
            apply=False,
        )

    assert summary["applied"] is False
    assert summary["computed_count"] == 1
    assert summary["direction_diff_count"] == 0
    replace_gray.assert_not_called()


class _ScalarResult(_Result):
    def __init__(self, value: int) -> None:
        super().__init__()
        self.value = value

    def scalar_one(self) -> int:
        return self.value

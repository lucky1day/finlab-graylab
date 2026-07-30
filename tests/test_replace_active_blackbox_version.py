from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


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

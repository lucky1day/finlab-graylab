from __future__ import annotations

import inspect
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from harness import signal_gap_plan
from shared.data_bridge import authority as databridge_authority


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rollback_count = 0

    def exec_driver_sql(self, statement: str) -> None:
        self.statements.append(statement)

    def rollback(self) -> None:
        self.rollback_count += 1

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()

    def connect(self) -> _Connection:
        return self.connection


def _config(
    scheme_id: str = "demo_native",
    *,
    runtime_type: str = "native_adapter",
    status: str = "active",
    frequency: str = "daily",
    version_status: str = "active",
    horizon: int | None = None,
    task_type: str | None = None,
    tenors: tuple[str, ...] = ("5Y",),
) -> SimpleNamespace:
    resolved_horizon = horizon or (
        6 if frequency == "weekly" else (30 if frequency == "monthly" else 5)
    )
    resolved_task_type = task_type or (
        "weekly_point"
        if frequency == "weekly"
        else ("monthly" if frequency == "monthly" else "T+5")
    )
    return SimpleNamespace(
        scheme_id=scheme_id,
        scheme_version="version-1",
        runtime_type=runtime_type,
        status=status,
        frequency=frequency,
        version_status=version_status,
        horizon=resolved_horizon,
        task_type=resolved_task_type,
        tenors=list(tenors),
    )


def _target(
    scheme_id: str = "demo_native",
    *,
    runtime_type: str = "native_adapter",
    frequency: str = "daily",
) -> signal_gap_plan.RegistryTarget:
    horizon = 6 if frequency == "weekly" else (30 if frequency == "monthly" else 5)
    task_type = (
        "weekly_point"
        if frequency == "weekly"
        else ("monthly" if frequency == "monthly" else "T+5")
    )
    return signal_gap_plan.RegistryTarget(
        registry_scheme_id=f"{scheme_id}__h{horizon}__5Y",
        base_scheme_id=scheme_id,
        runtime_type=runtime_type,
        frequency=frequency,
        task_type=task_type,
        target_tenor="5Y",
        horizon=horizon,
        scheme_version="version-1",
    )


def _case(
    scheme_id: str = "demo_native",
    *,
    runtime_type: str = "native_adapter",
    frequency: str = "daily",
) -> signal_gap_plan.ExpectedSignalCase:
    return signal_gap_plan.ExpectedSignalCase(
        registry_scheme_id=f"{scheme_id}__h5__5Y",
        base_scheme_id=scheme_id,
        runtime_type=runtime_type,
        frequency=frequency,
        task_type="T+5",
        target_tenor="5Y",
        horizon=5,
        predict_date="2026-08-10",
        feature_date="2026-08-07",
        target_date="2026-08-14",
    )


def _observed(
    scheme_id: str = "demo_native",
    *,
    phase: str = "gray_live",
    scheme_version: str = "version-1",
    target_tenor: str = "5Y",
    horizon: int = 5,
) -> signal_gap_plan.ObservedSignal:
    return signal_gap_plan.ObservedSignal(
        base_scheme_id=scheme_id,
        target_tenor=target_tenor,
        horizon=horizon,
        target_date="2026-08-14",
        predict_date="2026-08-10",
        feature_date="2026-08-07",
        phase=phase,
        scheme_version=scheme_version,
        run_status="success",
    )


def _authority() -> databridge_authority.StableDataBridgeCurrentAuthority:
    files = tuple(
        databridge_authority.StableDataBridgeFileIdentity(
            filename=filename,
            rows=2,
            columns=3,
            min_key=min_key,
            max_key=max_key,
            sha256=character * 64,
            business_hash=character * 64,
        )
        for filename, min_key, max_key, character in (
            ("daily_output.csv", "2026-08-06", "2026-08-07", "a"),
            ("monthly_output.csv", "202607", "202608", "b"),
            ("weekly_output.csv", "202631", "202632", "c"),
        )
    )
    return databridge_authority.StableDataBridgeCurrentAuthority(
        authority_schema_version="stable-databridge-current-authority-v2",
        generation_id="generation-1",
        refresh_date="2026-08-10",
        schema_version="data-bridge-v1",
        business_digest="d" * 64,
        files=files,
        cutoffs=(
            databridge_authority.StableDataBridgeCutoff(
                feature_date="2026-08-07",
                daily_cutoff_key="2026-08-07",
                weekly_cutoff_key="202632",
                monthly_cutoff_key="202608",
            ),
        ),
        publication_identity_sha256="e" * 64,
        stable_identity_sha256="f" * 64,
    )


def _snapshot(
    *,
    target: signal_gap_plan.RegistryTarget | None = None,
    case: signal_gap_plan.ExpectedSignalCase | None = None,
    live_signals: tuple[signal_gap_plan.ObservedSignal, ...] = (),
    blockers: tuple[dict[str, Any], ...] = (),
    authority: databridge_authority.StableDataBridgeCurrentAuthority | None = None,
    authority_error: str | None = None,
) -> signal_gap_plan.SignalGapSnapshot:
    resolved_target = target or _target()
    return signal_gap_plan.SignalGapSnapshot(
        registry_targets=(resolved_target,),
        expected_cases=(() if case is None else (case,)),
        live_signals=live_signals,
        control_plane_blockers=blockers,
        databridge_authority=authority,
        databridge_authority_error=authority_error,
    )


def _plan(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: signal_gap_plan.SignalGapSnapshot,
    *,
    configs: tuple[SimpleNamespace, ...] | None = None,
    base_scheme_id: str | None = None,
) -> tuple[dict[str, Any], _Engine, dict[str, Any]]:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        signal_gap_plan,
        "_discover_scheme_configs",
        lambda: configs if configs is not None else (_config(),),
    )

    def reader(_connection: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return snapshot

    monkeypatch.setattr(signal_gap_plan, "read_signal_gap_snapshot", reader)
    engine = _Engine()
    plan = signal_gap_plan.plan_signal_gaps(
        engine,
        predict_date="2026-08-10",
        base_scheme_id=base_scheme_id,
        databridge_config=object(),
    )
    return plan, engine, captured


def test_public_api_is_single_date_only() -> None:
    signature = inspect.signature(signal_gap_plan.plan_signal_gaps)

    assert list(signature.parameters) == [
        "engine",
        "predict_date",
        "base_scheme_id",
        "databridge_config",
    ]
    assert signature.parameters["predict_date"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["base_scheme_id"].default is None
    assert not hasattr(signal_gap_plan, "SignalGapPlanScope")
    assert not hasattr(signal_gap_plan, "canonical_plan_sha256")


def test_native_gap_uses_new_schema_and_null_input_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, engine, captured = _plan(monkeypatch, _snapshot(case=_case()))

    assert plan["schema_version"] == "single-date-active-live-gap-plan-v1"
    assert plan["status"] == "READY"
    assert plan["predict_date"] == "2026-08-10"
    assert plan["actions"][0]["action"] == "GRAY_LIVE_GAP"
    assert plan["actions"][0]["input_authority"] is None
    assert plan["counts"]["expected"] == 1
    assert engine.connection.statements == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
    ]
    assert engine.connection.rollback_count == 1
    assert captured["predict_date"] == "2026-08-10"
    serialized = str(plan).lower()
    for removed in (
        "segment",
        "canonical",
        "start_date",
        "as_of_date",
        "selection",
        "code_sha256",
        "config_sha256",
        "plan_sha256",
        "stable-databridge-current-authority-v1",
    ):
        assert removed not in serialized


@pytest.mark.parametrize("phase", ["gray_live", "scheduled_live"])
def test_valid_existing_live_signal_is_skip_present(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    plan, _, _ = _plan(
        monkeypatch,
        _snapshot(case=_case(), live_signals=(_observed(phase=phase),)),
    )

    assert plan["actions"][0]["action"] == "SKIP_PRESENT"
    assert plan["status"] == "READY"


def test_targeted_active_scheme_not_due_is_explicit_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _, _ = _plan(
        monkeypatch,
        _snapshot(),
        base_scheme_id="demo_native",
    )

    assert plan["status"] == "READY"
    assert plan["actions"][0]["action"] == "SKIP_NOT_DUE"
    assert plan["counts"]["expected"] == 0
    assert plan["counts"]["actionable"] == 0


def test_targeted_not_due_ignores_live_rows_for_that_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _, _ = _plan(
        monkeypatch,
        _snapshot(live_signals=(_observed(),)),
        base_scheme_id="demo_native",
    )

    assert plan["status"] == "READY"
    assert plan["actions"][0]["action"] == "SKIP_NOT_DUE"
    assert plan["counts"]["blocked"] == 0


def test_full_scan_omits_not_due_active_schemes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    due_target = _target("due_native")
    not_due_target = _target("not_due_weekly", frequency="weekly")
    snapshot = signal_gap_plan.SignalGapSnapshot(
        registry_targets=(due_target, not_due_target),
        expected_cases=(_case("due_native"),),
        live_signals=(),
    )
    plan, _, captured = _plan(
        monkeypatch,
        snapshot,
        configs=(
            _config("due_native"),
            _config("not_due_weekly", frequency="weekly"),
            _config("paused_native", status="paused"),
        ),
    )

    assert [item["base_scheme_id"] for item in plan["actions"]] == [
        "due_native"
    ]
    assert [item.base_scheme_id for item in captured["execution_authority"]] == [
        "due_native",
        "not_due_weekly",
    ]


def test_full_snapshot_validation_only_selects_due_execution_authority() -> None:
    calendar = SimpleNamespace(
        is_trading_day=lambda value: value == "2026-08-10",
        weekly_predict_dates=frozenset({"2026-08-11"}),
    )
    daily = SimpleNamespace(base_scheme_id="daily", frequency="daily")
    weekly = SimpleNamespace(base_scheme_id="weekly", frequency="weekly")

    assert signal_gap_plan._due_execution_authority(
        (daily, weekly),
        predict_date="2026-08-10",
        calendar=calendar,
        targeted=False,
    ) == (daily,)
    assert signal_gap_plan._due_execution_authority(
        (weekly,),
        predict_date="2026-08-10",
        calendar=calendar,
        targeted=True,
    ) == (weekly,)


def test_weekly_due_is_saturday_even_when_friday_is_holiday() -> None:
    class Calendar:
        weekly_predict_dates = frozenset({"2026-08-17"})

        def is_trading_day(self, value: str) -> bool:
            return value in {"2026-08-13", "2026-08-17", "2026-08-21"}

        def previous_trading_day(self, value: str) -> str:
            assert value == "2026-08-15"
            return "2026-08-13"

        def week_id_for_date(self, value: str) -> int | None:
            return {
                "2026-08-13": 202632,
                "2026-08-17": 202633,
                "2026-08-21": 202633,
            }.get(value)

        def week_id_to_last_trading_day(self, week_id: int) -> str:
            return {202632: "2026-08-13", 202633: "2026-08-21"}[week_id]

        def next_trading_days(self, value: str, count: int) -> list[str]:
            assert value == "2026-08-13"
            return ["2026-08-17", "2026-08-21"][:count]

    calendar = Calendar()
    target = _target("weekly_native", frequency="weekly")

    assert signal_gap_plan._is_frequency_due(
        "weekly",
        predict_date="2026-08-15",
        calendar=calendar,
    )
    assert not signal_gap_plan._is_frequency_due(
        "weekly",
        predict_date="2026-08-17",
        calendar=calendar,
    )
    case = signal_gap_plan._expected_case(
        target,
        predict_date="2026-08-15",
        calendar=calendar,
    )
    assert case.feature_date == "2026-08-13"
    assert case.target_date == "2026-08-21"


class _MappingRows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingRows:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _RegistryReadConnection:
    def __init__(
        self,
        *,
        registry_rows: list[dict[str, Any]],
        version_rows: list[dict[str, Any]],
    ) -> None:
        self.registry_rows = registry_rows
        self.version_rows = version_rows

    def execute(self, statement: Any, _parameters: Any) -> _MappingRows:
        sql = str(statement)
        if "t_scheme_registry" in sql:
            return _MappingRows(self.registry_rows)
        if "t_scheme_versions" in sql:
            return _MappingRows(self.version_rows)
        raise AssertionError(sql)


def _execution_identity(
    *,
    version_status: str = "active",
    target_tenors: tuple[str, ...] = ("5Y",),
    runtime_type: str = "native_adapter",
) -> SimpleNamespace:
    return SimpleNamespace(
        base_scheme_id="demo_native",
        scheme_version="version-1",
        runtime_type=runtime_type,
        frequency="daily",
        horizon=5,
        task_type="T+5",
        target_tenors=target_tenors,
        version_status=version_status,
        status="active",
    )


def _registry_row(
    tenor: str,
    *,
    status: str = "active",
    horizon: int = 5,
    task_type: str = "T+5",
    frequency: str = "daily",
    runtime_type: str = "native_adapter",
) -> dict[str, Any]:
    return {
        "scheme_id": f"demo_native__h{horizon}__{tenor}",
        "base_scheme_id": "demo_native",
        "runtime_type": runtime_type,
        "frequency": frequency,
        "task_type": task_type,
        "target_tenor": tenor,
        "horizon": horizon,
        "status": status,
    }


def _version_row(
    *,
    status: str = "active",
    runtime_type: str = "native_adapter",
    scheme_version: str = "version-1",
) -> dict[str, Any]:
    return {
        "scheme_id": "demo_native",
        "scheme_version": scheme_version,
        "runtime_type": runtime_type,
        "status": status,
    }


@pytest.mark.parametrize(
    ("runtime_type", "version_status"),
    [("blackbox_v2", "shadow"), ("native_adapter", "paused")],
)
def test_reader_blocks_nonactive_exact_config_version(
    runtime_type: str,
    version_status: str,
) -> None:
    connection = _RegistryReadConnection(
        registry_rows=[_registry_row("5Y", runtime_type=runtime_type)],
        version_rows=[_version_row(runtime_type=runtime_type)],
    )

    targets, blockers = signal_gap_plan._read_registry_targets(
        connection,
        execution_authority=(
            _execution_identity(
                version_status=version_status,
                runtime_type=runtime_type,
            ),
        ),
    )

    assert targets == ()
    assert blockers == (
        {
            "code": "SCHEME_CONFIG_VERSION_NOT_ACTIVE",
            "base_scheme_id": "demo_native",
        },
    )


def test_reader_blocks_nonactive_native_database_exact_version() -> None:
    connection = _RegistryReadConnection(
        registry_rows=[_registry_row("5Y")],
        version_rows=[_version_row(status="paused")],
    )

    targets, blockers = signal_gap_plan._read_registry_targets(
        connection,
        execution_authority=(_execution_identity(),),
    )

    assert targets == ()
    assert blockers == (
        {
            "code": "ACTIVE_VERSION_EXACT_IDENTITY_MISSING",
            "base_scheme_id": "demo_native",
        },
    )


def test_reader_blocks_multiple_active_blackbox_versions() -> None:
    connection = _RegistryReadConnection(
        registry_rows=[_registry_row("5Y", runtime_type="blackbox_v2")],
        version_rows=[
            _version_row(runtime_type="blackbox_v2"),
            _version_row(
                runtime_type="blackbox_v2",
                scheme_version="other-active-version",
            ),
        ],
    )

    targets, blockers = signal_gap_plan._read_registry_targets(
        connection,
        execution_authority=(
            _execution_identity(runtime_type="blackbox_v2"),
        ),
    )

    assert targets == ()
    assert blockers == (
        {
            "code": "ACTIVE_VERSION_CARDINALITY_INVALID",
            "base_scheme_id": "demo_native",
        },
    )


@pytest.mark.parametrize(
    ("registry_rows", "expected_tenors"),
    [
        (
            [_registry_row("5Y"), _registry_row("10Y", status="paused")],
            ("5Y", "10Y"),
        ),
        ([_registry_row("5Y")], ("5Y", "10Y")),
        ([_registry_row("5Y"), _registry_row("10Y")], ("5Y",)),
        ([_registry_row("5Y", horizon=1, task_type="T+1")], ("5Y",)),
    ],
)
def test_reader_blocks_registry_target_multiset_or_identity_drift(
    registry_rows: list[dict[str, Any]],
    expected_tenors: tuple[str, ...],
) -> None:
    connection = _RegistryReadConnection(
        registry_rows=registry_rows,
        version_rows=[_version_row()],
    )

    targets, blockers = signal_gap_plan._read_registry_targets(
        connection,
        execution_authority=(
            _execution_identity(target_tenors=expected_tenors),
        ),
    )

    assert targets == ()
    assert blockers == (
        {
            "code": "ACTIVE_REGISTRY_CONFIG_IDENTITY_MISMATCH",
            "base_scheme_id": "demo_native",
        },
    )


def test_full_scan_with_no_active_configs_is_ready_and_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        signal_gap_plan,
        "_discover_scheme_configs",
        lambda: (_config("paused", status="paused"),),
    )
    engine = _Engine()

    plan = signal_gap_plan.plan_signal_gaps(
        engine,
        predict_date="2026-08-10",
        databridge_config=object(),
    )

    assert plan["status"] == "READY"
    assert plan["counts"]["active_target"] == 0
    assert plan["counts"]["expected"] == 0
    assert plan["actions"] == []
    assert engine.connection.statements == []


class _LiveReadConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sql = ""
        self.parameters: dict[str, Any] = {}

    def execute(
        self,
        statement: Any,
        parameters: dict[str, Any],
    ) -> _MappingRows:
        self.sql = str(statement)
        self.parameters = parameters
        return _MappingRows(self.rows)


def _prediction_row(
    *,
    predict_date: str,
    feature_date: str,
    target_date: str,
    target_tenor: str = "5Y",
    horizon: int = 5,
) -> dict[str, Any]:
    return {
        "id": 1,
        "scheme_id": "demo_native",
        "target_tenor": target_tenor,
        "horizon": horizon,
        "predict_date": predict_date,
        "feature_date": feature_date,
        "target_date": target_date,
        "prediction_phase": "gray_live",
        "scheme_version": "version-1",
        "run_id": "run-1",
        "run_status": "success",
        "run_scheme_id": "demo_native",
        "run_scheme_version": "version-1",
        "run_runtime_type": "native_adapter",
        "run_prediction_phase": "gray_live",
        "run_predict_date": predict_date,
    }


def test_live_reader_is_bounded_and_ignores_unrelated_history() -> None:
    target = _target()
    case = _case()
    connection = _LiveReadConnection(
        [
            _prediction_row(
                predict_date="2020-01-02",
                feature_date="2020-01-01",
                target_date="2020-01-09",
            )
        ]
    )

    signals = signal_gap_plan._read_live_signals(
        connection,
        (target,),
        predict_date="2026-08-10",
        expected_business_keys={case.business_key},
    )
    plan = signal_gap_plan._build_signal_gap_plan(
        signal_gap_plan.SignalGapSnapshot(
            registry_targets=(target,),
            expected_cases=(case,),
            live_signals=signals,
        ),
        predict_date="2026-08-10",
        base_scheme_id=None,
    )

    assert signals == ()
    assert plan["status"] == "READY"
    assert plan["actions"][0]["action"] == "GRAY_LIVE_GAP"
    assert "p.predict_date = :predict_date" in connection.sql
    assert "p.target_date IN" in connection.sql
    assert connection.parameters["predict_date"] == "2026-08-10"
    assert connection.parameters["expected_target_dates"] == ["2026-08-14"]


def test_live_reader_keeps_wrong_predict_date_on_expected_business_key() -> None:
    target = _target()
    case = _case()
    connection = _LiveReadConnection(
        [
            _prediction_row(
                predict_date="2026-08-09",
                feature_date="2026-08-07",
                target_date="2026-08-14",
            )
        ]
    )

    signals = signal_gap_plan._read_live_signals(
        connection,
        (target,),
        predict_date="2026-08-10",
        expected_business_keys={case.business_key},
    )
    plan = signal_gap_plan._build_signal_gap_plan(
        signal_gap_plan.SignalGapSnapshot(
            registry_targets=(target,),
            expected_cases=(case,),
            live_signals=signals,
        ),
        predict_date="2026-08-10",
        base_scheme_id=None,
    )

    assert len(signals) == 1
    assert plan["status"] == "BLOCKED"
    assert plan["actions"][0]["action"] == "BLOCKED_DATA_CONTRACT"


@pytest.mark.parametrize(
    ("base_scheme_id", "configs", "failure_code"),
    [
        ("missing", (_config(),), "SCHEME_CONFIG_NOT_FOUND"),
        (
            "paused_native",
            (_config("paused_native", status="paused"),),
            "SCHEME_CONFIG_NOT_ACTIVE",
        ),
    ],
)
def test_targeted_missing_or_inactive_config_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    base_scheme_id: str,
    configs: tuple[SimpleNamespace, ...],
    failure_code: str,
) -> None:
    monkeypatch.setattr(signal_gap_plan, "_discover_scheme_configs", lambda: configs)
    engine = _Engine()

    plan = signal_gap_plan.plan_signal_gaps(
        engine,
        predict_date="2026-08-10",
        base_scheme_id=base_scheme_id,
        databridge_config=object(),
    )

    assert plan["status"] == "BLOCKED"
    assert plan["failure_code"] == failure_code
    assert plan["counts"]["blocked"] == 1


@pytest.mark.parametrize(
    "blocker",
    [
        "ACTIVE_VERSION_EXACT_IDENTITY_MISSING",
        "ACTIVE_REGISTRY_TARGET_MISSING",
    ],
)
def test_targeted_inactive_version_or_registry_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    plan, _, _ = _plan(
        monkeypatch,
        _snapshot(
            blockers=(
                {"code": blocker, "base_scheme_id": "demo_native"},
            ),
        ),
        base_scheme_id="demo_native",
    )

    assert plan["status"] == "BLOCKED"
    assert plan["failure_code"] == blocker
    assert plan["counts"]["blocked"] == 1


def test_targeted_selector_rejects_composite_or_list_syntax() -> None:
    engine = _Engine()

    for invalid in (
        "demo_native__h5__5Y",
        "demo_native,other",
        ["demo_native"],
    ):
        with pytest.raises(signal_gap_plan.SignalGapPlanError, match="base_scheme_id"):
            signal_gap_plan.plan_signal_gaps(
                engine,
                predict_date="2026-08-10",
                base_scheme_id=invalid,  # type: ignore[arg-type]
                databridge_config=object(),
            )


def test_blackbox_gap_binds_v2_source_identity_and_single_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target("demo_blackbox", runtime_type="blackbox_v2")
    case = _case("demo_blackbox", runtime_type="blackbox_v2")
    authority = _authority()
    plan, _, _ = _plan(
        monkeypatch,
        _snapshot(target=target, case=case, authority=authority),
        configs=(_config("demo_blackbox", runtime_type="blackbox_v2"),),
    )

    input_authority = plan["actions"][0]["input_authority"]
    assert input_authority == {
        **databridge_authority.blackbox_gray_replay_source_identity(authority),
        "cutoff": {
            "feature_date": "2026-08-07",
            "daily_cutoff_key": "2026-08-07",
            "weekly_cutoff_key": "202632",
            "monthly_cutoff_key": "202608",
            "source_weekly_cutoff_key": None,
            "source_monthly_cutoff_key": None,
        },
    }
    assert "authority_schema_version" not in input_authority
    assert "publication_identity_sha256" not in input_authority
    assert "cutoffs" not in input_authority


@pytest.mark.parametrize(
    ("authority_error", "expected_action"),
    [
        ("MISSING", "BLOCKED_NO_GENERATION"),
        ("INVALID", "BLOCKED_DATA_CONTRACT"),
    ],
)
def test_blackbox_gap_fails_closed_without_valid_current(
    monkeypatch: pytest.MonkeyPatch,
    authority_error: str,
    expected_action: str,
) -> None:
    target = _target("demo_blackbox", runtime_type="blackbox_v2")
    case = _case("demo_blackbox", runtime_type="blackbox_v2")
    plan, _, _ = _plan(
        monkeypatch,
        _snapshot(
            target=target,
            case=case,
            authority_error=authority_error,
        ),
        configs=(_config("demo_blackbox", runtime_type="blackbox_v2"),),
    )

    assert plan["actions"][0]["action"] == expected_action
    assert plan["status"] == "BLOCKED"


@pytest.mark.parametrize(
    "signals",
    [
        (_observed(), _observed()),
        (_observed(scheme_version="wrong-version"),),
        (_observed(phase="canonical"),),
        (_observed(target_tenor="10Y"),),
    ],
)
def test_duplicate_or_drifted_live_result_blocks_data_contract(
    monkeypatch: pytest.MonkeyPatch,
    signals: tuple[signal_gap_plan.ObservedSignal, ...],
) -> None:
    plan, _, _ = _plan(
        monkeypatch,
        _snapshot(case=_case(), live_signals=signals),
    )

    assert plan["status"] == "BLOCKED"
    assert plan["actions"][0]["action"] == "BLOCKED_DATA_CONTRACT"


def test_cli_accepts_only_single_date_and_optional_single_base_scheme() -> None:
    from harness.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(
        [
            "signal-gap-plan",
            "--predict-date",
            "2026-08-10",
            "--scheme-id",
            "demo_native",
        ]
    )

    assert args.predict_date == "2026-08-10"
    assert args.scheme_id == "demo_native"
    help_text = parser._subparsers._group_actions[0].choices[
        "signal-gap-plan"
    ].format_help()
    assert "--predict-date" in help_text
    assert "--scheme-id" in help_text
    for removed in (
        "--start",
        "--as-of",
        "--target-date-start",
        "--target-date-end",
        "--task-type",
        "--base-scheme-id",
    ):
        assert removed not in help_text

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "signal-gap-plan",
                "--predict-date",
                "2026-08-10",
                "--scheme-id",
                "demo_native",
                "--scheme-id",
                "other",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["signal-gap-plan", "--predict-date", "2026-8-10"]
        )

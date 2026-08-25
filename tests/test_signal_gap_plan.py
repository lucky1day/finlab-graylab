from __future__ import annotations

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










def test_period_average_due_uses_task_bucket_anchor_not_frequency_shortcut() -> None:
    from datetime import date, timedelta

    rows = []
    current = date(2024, 1, 1)
    while current <= date(2024, 6, 30):
        rows.append({"rdate": current.isoformat(), "trade_flag": "1"})
        current += timedelta(days=1)
    calendar = SimpleNamespace(period_calendar_rows=lambda: tuple(rows))

    assert signal_gap_plan._is_frequency_due(
        "quarterly",
        predict_date="2024-03-29",
        calendar=calendar,
        task_type="quarterly_average",
    )
    assert not signal_gap_plan._is_frequency_due(
        "quarterly",
        predict_date="2024-03-28",
        calendar=calendar,
        task_type="quarterly_average",
    )














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

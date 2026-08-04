from __future__ import annotations

from dataclasses import replace
import unittest
from typing import Any

from harness import signal_gap_plan


_CODE_HASH = "a" * 64
_CONFIG_HASH = "b" * 64


class _Mappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Mappings:
        return self

    def all(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


class _Connection:
    def __init__(
        self,
        registry_rows: list[dict[str, Any]],
        version_rows: list[dict[str, Any]],
    ) -> None:
        self._registry_rows = registry_rows
        self._version_rows = version_rows

    def execute(self, statement: Any) -> _Mappings:
        sql = str(statement)
        if "FROM t_scheme_registry" in sql:
            return _Mappings(self._registry_rows)
        if "FROM t_scheme_versions" in sql:
            return _Mappings(self._version_rows)
        raise AssertionError(f"unexpected statement: {sql}")


def _registry_row(
    base_scheme_id: str,
    *,
    frequency: str,
    task_type: str,
    target_tenor: str,
    horizon: int,
) -> dict[str, Any]:
    return {
        "scheme_id": (
            f"{base_scheme_id}__h{horizon}__{target_tenor}"
        ),
        "base_scheme_id": base_scheme_id,
        "runtime_type": "native_adapter",
        "frequency": frequency,
        "task_type": task_type,
        "target_tenor": target_tenor,
        "horizon": horizon,
    }


def _active_scope_fixture() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    tuple[signal_gap_plan.DiscoveredSchemeIdentity, ...],
]:
    registry_rows = [
        _registry_row(
            f"scheme_{index:02d}",
            frequency="daily",
            task_type="T+1",
            target_tenor="1Y",
            horizon=1,
        )
        for index in range(32)
    ]
    registry_rows.extend(
        _registry_row(
            f"scheme_{index:02d}",
            frequency="weekly",
            task_type="weekly_point",
            target_tenor="10Y",
            horizon=6,
        )
        for index in range(32, 45)
    )
    registry_rows.extend(
        _registry_row(
            f"scheme_{index:02d}",
            frequency="monthly",
            task_type="monthly",
            target_tenor="10Y",
            horizon=30,
        )
        for index in range(45, 50)
    )
    registry_rows.extend(
        _registry_row(
            f"scheme_{index:02d}",
            frequency="monthly",
            task_type="monthly",
            target_tenor="10Y",
            horizon=30,
        )
        for index in range(3)
    )
    base_scheme_ids = sorted(
        {str(row["base_scheme_id"]) for row in registry_rows}
    )
    version_rows = [
        {
            "scheme_id": base_scheme_id,
            "scheme_version": f"{base_scheme_id}-v1",
            "runtime_type": "native_adapter",
            "code_hash": _CODE_HASH,
            "config_hash": _CONFIG_HASH,
        }
        for base_scheme_id in base_scheme_ids
    ]
    execution_authority = tuple(
        signal_gap_plan.DiscoveredSchemeIdentity(
            base_scheme_id=base_scheme_id,
            scheme_version=f"{base_scheme_id}-v1",
            runtime_type="native_adapter",
            code_sha256=_CODE_HASH,
            config_sha256=_CONFIG_HASH,
            status="active",
        )
        for base_scheme_id in base_scheme_ids
    )
    return registry_rows, version_rows, execution_authority


def _valid_target() -> signal_gap_plan.RegistryTarget:
    return signal_gap_plan.RegistryTarget(
        registry_scheme_id="alpha__h1__1Y",
        base_scheme_id="alpha",
        runtime_type="native_adapter",
        frequency="daily",
        task_type="T+1",
        target_tenor="1Y",
        horizon=1,
        scheme_version="alpha-v1",
        live_target_start_date=(
            signal_gap_plan.PLATFORM_LIVE_TARGET_START_DATE
        ),
        live_boundary_source=(
            signal_gap_plan.PLATFORM_LIVE_BOUNDARY_VERSION
        ),
        input_mode="generation_v1",
        code_sha256=_CODE_HASH,
        config_sha256=_CONFIG_HASH,
    )


class SignalGapPlanTests(unittest.TestCase):
    def test_read_registry_versions_accepts_53_targets_and_50_executions(
        self,
    ) -> None:
        registry_rows, version_rows, execution_authority = (
            _active_scope_fixture()
        )

        try:
            raw_rows, targets, blockers, active_version_digest = (
                signal_gap_plan._read_registry_versions(
                    _Connection(registry_rows, version_rows),
                    execution_authority=execution_authority,
                )
            )
        except signal_gap_plan.SignalGapPlanError as exc:
            self.fail(
                "valid dynamic active scope was rejected: "
                f"{exc.code}"
            )

        self.assertEqual(len(raw_rows), 53)
        self.assertEqual(len(targets), 53)
        self.assertEqual(
            len({target.base_scheme_id for target in targets}),
            50,
        )
        self.assertEqual(
            {
                frequency: sum(
                    target.frequency == frequency for target in targets
                )
                for frequency in ("daily", "weekly", "monthly")
            },
            {"daily": 32, "weekly": 13, "monthly": 8},
        )
        self.assertEqual(blockers, ())
        self.assertTrue(active_version_digest)

    def test_read_registry_versions_rejects_empty_active_registry(
        self,
    ) -> None:
        with self.assertRaises(
            signal_gap_plan.SignalGapPlanError
        ) as raised:
            signal_gap_plan._read_registry_versions(
                _Connection([], []),
                execution_authority=(),
            )

        self.assertEqual(raised.exception.code, "NO_ACTIVE_REGISTRY_TARGETS")

    def test_registry_target_task_contract_remains_fail_closed(self) -> None:
        invalid = replace(_valid_target(), horizon=5)

        with self.assertRaises(
            signal_gap_plan.SignalGapPlanError
        ) as raised:
            signal_gap_plan._validate_registry_target(invalid)

        self.assertEqual(raised.exception.code, "INVALID_REGISTRY_TARGET")
        self.assertEqual(
            raised.exception.detail,
            "task contract drift for alpha__h1__1Y",
        )

"""Blackbox V2 自动调度的版本化精确身份准入。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


SCHEMA_VERSION = "blackbox-scheduler-admission-v1"
VALID_MODES = frozenset({"formal", "gray"})
LEGACY_AUTOMATIC = "legacy_automatic"
DAILY_LEDGER = "daily_ledger"
RECURRING = "recurring"
DIRECT_SCHEDULED = "direct_scheduled"
VALID_CONTROL_PLANES = frozenset(
    {
        LEGACY_AUTOMATIC,
        DAILY_LEDGER,
        RECURRING,
        DIRECT_SCHEDULED,
    }
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADMISSION_PATH = (
    PROJECT_ROOT / "deploy" / "blackbox_scheduler_admission_v1.json"
)
_ROOT_FIELDS = frozenset({"schema_version", "schemes"})
_SCHEME_FIELDS = frozenset(
    {
        "scheme_id",
        "scheme_version",
        "runtime_type",
        "frequency",
        "task_type",
        "horizon",
        "target_tenor",
        "mode",
        "capabilities",
    }
)


@dataclass(frozen=True)
class SchedulerAdmissionEntry:
    """一个 Blackbox 精确身份在各调度控制面的能力。"""

    mode: str
    runtime_type: str
    frequency: str
    task_type: str
    horizon: int
    target_tenor: str
    capabilities: frozenset[str]


def _entry(
    *,
    mode: str,
    frequency: str,
    task_type: str,
    horizon: int,
    target_tenor: str,
    capabilities: frozenset[str],
) -> SchedulerAdmissionEntry:
    return SchedulerAdmissionEntry(
        mode=mode,
        runtime_type="blackbox_v2",
        frequency=frequency,
        task_type=task_type,
        horizon=horizon,
        target_tenor=target_tenor,
        capabilities=capabilities,
    )


_FORMAL_DAILY_CAPABILITIES = frozenset(
    {LEGACY_AUTOMATIC, DAILY_LEDGER, DIRECT_SCHEDULED}
)
_FORMAL_WEEKLY_CAPABILITIES = frozenset(
    {LEGACY_AUTOMATIC, RECURRING, DIRECT_SCHEDULED}
)
_DAILY_GRAY_CAPABILITIES = frozenset({DAILY_LEDGER})
_NO_CAPABILITIES: frozenset[str] = frozenset()


EXPECTED_EXACT_ADMISSIONS: Mapping[
    tuple[str, str],
    SchedulerAdmissionEntry,
] = (
    MappingProxyType(
        {
            (
                "one_y_t5_liq_excess_a_v1",
                "8d583560c9f1",
            ): _entry(
                mode="formal",
                frequency="daily",
                task_type="T+5",
                horizon=5,
                target_tenor="1Y",
                capabilities=_FORMAL_DAILY_CAPABILITIES,
            ),
            (
                "one_y_t5_liq_excess_a_w252_l7_v1",
                "103c93bbc913",
            ): _entry(
                mode="formal",
                frequency="daily",
                task_type="T+5",
                horizon=5,
                target_tenor="1Y",
                capabilities=_FORMAL_DAILY_CAPABILITIES,
            ),
            (
                "one_y_t5_liq_excess_a_w350_l7_v1",
                "86b458c568a5",
            ): _entry(
                mode="formal",
                frequency="daily",
                task_type="T+5",
                horizon=5,
                target_tenor="1Y",
                capabilities=_FORMAL_DAILY_CAPABILITIES,
            ),
            (
                "one_y_t5_liq_excess_b_w252_l7_v1",
                "ba00891cd179",
            ): _entry(
                mode="formal",
                frequency="daily",
                task_type="T+5",
                horizon=5,
                target_tenor="1Y",
                capabilities=_FORMAL_DAILY_CAPABILITIES,
            ),
            (
                "weekly_10y_lgbm_point_v1",
                "0666a6989d6b",
            ): _entry(
                mode="formal",
                frequency="weekly",
                task_type="weekly_point",
                horizon=1,
                target_tenor="10Y",
                capabilities=_FORMAL_WEEKLY_CAPABILITIES,
            ),
            (
                "cgb_a4_fundseason_1y",
                "04e7af163fb0",
            ): _entry(
                mode="gray",
                frequency="monthly",
                task_type="monthly",
                horizon=1,
                target_tenor="1Y",
                capabilities=_NO_CAPABILITIES,
            ),
            (
                "cgb_a4_fundseason_3y",
                "89d31f8bcb95",
            ): _entry(
                mode="gray",
                frequency="monthly",
                task_type="monthly",
                horizon=1,
                target_tenor="3Y",
                capabilities=_NO_CAPABILITIES,
            ),
            (
                "cgb_a4_fundseason_5y",
                "7d47e0328532",
            ): _entry(
                mode="gray",
                frequency="monthly",
                task_type="monthly",
                horizon=1,
                target_tenor="5Y",
                capabilities=_NO_CAPABILITIES,
            ),
            (
                "cgb_a4_fundseason_7y",
                "ddba87ece7ae",
            ): _entry(
                mode="gray",
                frequency="monthly",
                task_type="monthly",
                horizon=1,
                target_tenor="7Y",
                capabilities=_NO_CAPABILITIES,
            ),
            (
                "cgb_a4_fundseason_10y",
                "85a65700499b",
            ): _entry(
                mode="gray",
                frequency="monthly",
                task_type="monthly",
                horizon=1,
                target_tenor="10Y",
                capabilities=_NO_CAPABILITIES,
            ),
            (
                "ten_y_t5_maj3_k3_ic_static_v1",
                "c54b90bcafa7",
            ): _entry(
                mode="gray",
                frequency="daily",
                task_type="T+5",
                horizon=5,
                target_tenor="10Y",
                capabilities=_DAILY_GRAY_CAPABILITIES,
            ),
            (
                "ten_y_t5_maj4_k3_ic_static_v1",
                "6bdabf86b4a6",
            ): _entry(
                mode="gray",
                frequency="daily",
                task_type="T+5",
                horizon=5,
                target_tenor="10Y",
                capabilities=_DAILY_GRAY_CAPABILITIES,
            ),
            (
                "ten_y_t5_maj4_k3_ic_yearly_v1",
                "af04567a19c3",
            ): _entry(
                mode="gray",
                frequency="daily",
                task_type="T+5",
                horizon=5,
                target_tenor="10Y",
                capabilities=_DAILY_GRAY_CAPABILITIES,
            ),
            (
                "ten_y_t5_say_k5_sharpe_static_v1",
                "e8137af4b655",
            ): _entry(
                mode="gray",
                frequency="daily",
                task_type="T+5",
                horizon=5,
                target_tenor="10Y",
                capabilities=_DAILY_GRAY_CAPABILITIES,
            ),
        }
    )
)
RESERVED_BLACKBOX_SCHEME_IDS = frozenset(
    scheme_id
    for scheme_id, _scheme_version in EXPECTED_EXACT_ADMISSIONS
)


class BlackboxSchedulerAdmissionError(ValueError):
    """Blackbox 自动调度 admission 配置无效。"""


@dataclass(frozen=True)
class BlackboxSchedulerAdmissionPolicy:
    """按 ``scheme_id + scheme_version`` 冻结的自动调度权限。"""

    entries: Mapping[
        tuple[str, str],
        SchedulerAdmissionEntry,
    ]

    def mode(self, config: object) -> str | None:
        """返回完整匹配的 Blackbox 业务模式；漂移或非 Blackbox 返回空。"""
        entry = self._matching_entry(config)
        return entry.mode if entry is not None else None

    def allows(self, config: object, *, plane: str) -> bool:
        """判断方案是否获准进入指定调度控制面。"""
        if plane not in VALID_CONTROL_PLANES:
            raise BlackboxSchedulerAdmissionError(
                f"unknown Blackbox scheduler control plane: {plane}"
            )
        runtime_type = getattr(
            config,
            "runtime_type",
            "native_adapter",
        )
        if runtime_type != "blackbox_v2":
            scheme_id = str(
                getattr(config, "scheme_id", "")
            ).strip()
            return scheme_id not in RESERVED_BLACKBOX_SCHEME_IDS
        entry = self._matching_entry(config)
        return (
            entry is not None
            and plane in entry.capabilities
        )

    def _matching_entry(
        self,
        config: object,
    ) -> SchedulerAdmissionEntry | None:
        if getattr(
            config,
            "runtime_type",
            "native_adapter",
        ) != "blackbox_v2":
            return None
        identity = (
            str(getattr(config, "scheme_id", "")).strip(),
            str(getattr(config, "scheme_version", "")).strip(),
        )
        entry = self.entries.get(identity)
        if entry is None:
            return None
        tenors = tuple(
            str(tenor)
            for tenor in getattr(config, "tenors", ())
        )
        if (
            getattr(config, "runtime_type", None)
            != entry.runtime_type
            or str(getattr(config, "frequency", "")).strip()
            != entry.frequency
            or str(getattr(config, "task_type", "")).strip()
            != entry.task_type
            or getattr(config, "horizon", None) != entry.horizon
            or tenors != (entry.target_tenor,)
        ):
            return None
        return entry


def load_blackbox_scheduler_admission(
    path: str | Path = DEFAULT_ADMISSION_PATH,
) -> BlackboxSchedulerAdmissionPolicy:
    """加载 Blackbox 自动调度 admission，配置异常时 fail-closed。"""
    admission_path = Path(path)
    try:
        payload = json.loads(
            admission_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except FileNotFoundError as exc:
        raise BlackboxSchedulerAdmissionError(
            f"Blackbox scheduler admission not found: {admission_path}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission is not valid UTF-8: "
            f"{admission_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission is invalid JSON: "
            f"{admission_path}"
        ) from exc
    except OSError as exc:
        raise BlackboxSchedulerAdmissionError(
            f"Blackbox scheduler admission is unreadable: {admission_path}"
        ) from exc

    if not isinstance(payload, dict):
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission root must be an object"
        )
    if set(payload) != _ROOT_FIELDS:
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission root must contain exact fields: "
            f"{sorted(_ROOT_FIELDS)}"
        )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission has unsupported schema_version"
        )

    rows = payload["schemes"]
    if not isinstance(rows, list):
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission schemes must be a list"
        )

    entries: dict[
        tuple[str, str],
        SchedulerAdmissionEntry,
    ] = {}
    seen_scheme_ids: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise BlackboxSchedulerAdmissionError(
                "Blackbox scheduler admission scheme entries must be objects"
            )
        if set(row) != _SCHEME_FIELDS:
            raise BlackboxSchedulerAdmissionError(
                "Blackbox scheduler admission scheme entry must contain "
                f"exact fields at index {index}: {sorted(_SCHEME_FIELDS)}"
            )
        scheme_id = _required_text(row, "scheme_id", index)
        scheme_version = _required_text(
            row,
            "scheme_version",
            index,
        )
        if scheme_id in seen_scheme_ids:
            raise BlackboxSchedulerAdmissionError(
                "Blackbox scheduler admission contains duplicate "
                f"scheme_id: {scheme_id}"
            )
        seen_scheme_ids.add(scheme_id)
        runtime_type = _required_text(
            row,
            "runtime_type",
            index,
        )
        frequency = _required_text(row, "frequency", index)
        task_type = _required_text(row, "task_type", index)
        horizon = _required_positive_int(
            row,
            "horizon",
            index,
        )
        target_tenor = _required_text(
            row,
            "target_tenor",
            index,
        )
        mode = _required_text(row, "mode", index)
        if mode not in VALID_MODES:
            raise BlackboxSchedulerAdmissionError(
                "Blackbox scheduler admission has illegal mode at "
                f"index {index}: {mode}"
            )
        identity = (scheme_id, scheme_version)
        if identity in entries:
            raise BlackboxSchedulerAdmissionError(
                "Blackbox scheduler admission contains duplicate identity: "
                f"{scheme_id}@{scheme_version}"
            )
        entries[identity] = SchedulerAdmissionEntry(
            mode=mode,
            runtime_type=runtime_type,
            frequency=frequency,
            task_type=task_type,
            horizon=horizon,
            target_tenor=target_tenor,
            capabilities=_required_capabilities(
                row,
                index,
            ),
        )

    if entries != EXPECTED_EXACT_ADMISSIONS:
        expected_ids = set(EXPECTED_EXACT_ADMISSIONS)
        actual_ids = set(entries)
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        definition_drift = sorted(
            identity
            for identity in expected_ids & actual_ids
            if entries[identity]
            != EXPECTED_EXACT_ADMISSIONS[identity]
        )
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission must equal exact frozen "
            f"admissions: missing={missing} extra={extra} "
            f"definition_drift={definition_drift}"
        )

    return BlackboxSchedulerAdmissionPolicy(
        entries=MappingProxyType(entries)
    )


def _required_text(
    row: Mapping[str, object],
    field: str,
    index: int,
) -> str:
    value = row[field]
    if not isinstance(value, str) or not value.strip():
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission requires non-empty "
            f"{field} at index {index}"
        )
    return value.strip()


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """拒绝 JSON 任意对象层级的重复键，避免解析器静默覆盖。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BlackboxSchedulerAdmissionError(
                f"Blackbox scheduler admission duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _required_positive_int(
    row: Mapping[str, object],
    field: str,
    index: int,
) -> int:
    value = row[field]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
    ):
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission requires positive integer "
            f"{field} at index {index}"
        )
    return value


def _required_capabilities(
    row: Mapping[str, object],
    index: int,
) -> frozenset[str]:
    value = row["capabilities"]
    if not isinstance(value, list):
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission capabilities must be a list "
            f"at index {index}"
        )
    if any(
        not isinstance(item, str) or not item.strip()
        for item in value
    ):
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission capabilities must contain "
            f"non-empty strings at index {index}"
        )
    normalized = [item.strip() for item in value]
    if len(normalized) != len(set(normalized)):
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission contains duplicate capability "
            f"at index {index}"
        )
    illegal = sorted(set(normalized) - VALID_CONTROL_PLANES)
    if illegal:
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission contains illegal capabilities "
            f"at index {index}: {illegal}"
        )
    return frozenset(normalized)

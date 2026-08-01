"""launchd daily-gray 任务的精确、不可变身份策略。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from scheduler.discovery import SchemeConfig, discover_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = (
    PROJECT_ROOT / "deploy" / "daily_gray_launchd_policy_v1.json"
)
SCHEMA_VERSION = "daily-gray-launchd-policy-v1"
PLIST_LABEL = "com.bond-factor-lab.daily-gray"
ENTRYPOINT = "scheduler.daily_gray_runner"
PREDICTION_PHASE = "gray_live"
EXPECTED_EXECUTION_COUNT = 28
EXPECTED_TARGET_COUNT = 32
EXECUTION_CLASSES = frozenset({"heavy", "light"})
FROZEN_HEAVY_SCHEME_IDS = frozenset(
    {
        "daily_5y_2_v28",
        "daily_7y_1_v28",
        "liwei_0616_10y01_full_oos_k3_div_k10",
        "liwei_0616_5y01_full_oos_k3_div_k10",
        "liwei_0616_5y_auc_static_all_k3_div_k10",
        "liwei_0616_5y_auc_yearly_all_k3_div_k10",
        "liwei_0616_5y_ic_yearly_all_k3_div_k10",
        "liwei_0616_7y01_cons_say_k3_div_k10",
        "liwei_0616_7y03_cons_all_k3_div_k8",
    }
)
FROZEN_PUBLISHER_MAP: Mapping[str, str] = MappingProxyType(
    {
        "liwei_0616_10y01_cons_say_k3_div_k10": (
            "liwei_0616_10y01_full_oos_k3_div_k10"
        ),
        "liwei_0616_10y02_cons_say_k3_div_k5": (
            "liwei_0616_10y01_full_oos_k3_div_k10"
        ),
        "liwei_0616_cons_sda_k3_div_k10": (
            "liwei_0616_5y01_full_oos_k3_div_k10"
        ),
    }
)

_ROOT_CONSTANTS: Mapping[str, object] = MappingProxyType(
    {
        "schema_version": SCHEMA_VERSION,
        "plist_label": PLIST_LABEL,
        "entrypoint": ENTRYPOINT,
        "prediction_phase": PREDICTION_PHASE,
        "expected_execution_count": EXPECTED_EXECUTION_COUNT,
        "expected_target_count": EXPECTED_TARGET_COUNT,
    }
)
_ROOT_FIELDS = frozenset((*_ROOT_CONSTANTS, "schemes"))
_REQUIRED_SCHEME_FIELDS = frozenset(
    {
        "scheme_id",
        "scheme_version",
        "runtime_type",
        "frequency",
        "task_type",
        "horizon",
        "target_tenors",
        "execution_class",
    }
)
_OPTIONAL_SCHEME_FIELDS = frozenset({"publisher_scheme_id"})


class DailyGrayLaunchdPolicyError(ValueError):
    """daily-gray launchd policy 无效或与 strict discovery 漂移。"""


@dataclass(frozen=True)
class DailyGrayLaunchdSchemePolicy:
    """单个 active daily 执行身份的冻结策略。"""

    scheme_id: str
    scheme_version: str
    runtime_type: str
    frequency: str
    task_type: str
    horizon: int
    target_tenors: tuple[str, ...]
    execution_class: str
    publisher_scheme_id: str | None = None


@dataclass(frozen=True)
class DailyGrayLaunchdPolicy:
    """一个 launchd daily-gray 任务的完整冻结策略。"""

    schema_version: str
    plist_label: str
    entrypoint: str
    prediction_phase: str
    expected_execution_count: int
    expected_target_count: int
    schemes: Mapping[str, DailyGrayLaunchdSchemePolicy]


def load_daily_gray_launchd_policy(
    path: Path = DEFAULT_POLICY_PATH,
    *,
    discovered: Iterable[SchemeConfig] | None = None,
) -> DailyGrayLaunchdPolicy:
    """加载 policy，并与当前 strict active-daily discovery 精确核对。"""

    payload = _load_json(Path(path))
    _validate_root(payload)
    rows = _parse_scheme_rows(payload["schemes"])

    if discovered is None:
        try:
            discovered_configs = tuple(discover_schemes(strict=True))
        except Exception as exc:
            raise DailyGrayLaunchdPolicyError(
                f"strict discovery failed: {exc}"
            ) from exc
    else:
        discovered_configs = tuple(discovered)
    active_daily = tuple(
        config
        for config in discovered_configs
        if config.status == "active" and config.frequency == "daily"
    )
    discovered_by_id = _index_discovery(active_daily)
    policy_by_id = {row.scheme_id: row for row in rows}

    _validate_identity_sets(policy_by_id, discovered_by_id)
    _validate_cardinality(rows, active_daily)
    for row in rows:
        _validate_discovery_match(row, discovered_by_id[row.scheme_id])
    _validate_dependencies(policy_by_id)
    _validate_frozen_execution_semantics(policy_by_id)

    return DailyGrayLaunchdPolicy(
        schema_version=SCHEMA_VERSION,
        plist_label=PLIST_LABEL,
        entrypoint=ENTRYPOINT,
        prediction_phase=PREDICTION_PHASE,
        expected_execution_count=EXPECTED_EXECUTION_COUNT,
        expected_target_count=EXPECTED_TARGET_COUNT,
        schemes=MappingProxyType(policy_by_id),
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_object_keys,
        )
    except _DuplicateJsonObjectKeyError as exc:
        raise DailyGrayLaunchdPolicyError(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise DailyGrayLaunchdPolicyError(
            f"{path}: invalid JSON: {exc.msg}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise DailyGrayLaunchdPolicyError(
            f"{path}: policy is not valid UTF-8"
        ) from exc
    except OSError as exc:
        raise DailyGrayLaunchdPolicyError(
            f"cannot read daily-gray launchd policy {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DailyGrayLaunchdPolicyError("policy root must be an object")
    return payload


class _DuplicateJsonObjectKeyError(ValueError):
    """JSON object 内出现重复 key。"""


def _reject_duplicate_json_object_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonObjectKeyError(
                f"duplicate JSON object key: {key}"
            )
        result[key] = value
    return result


def _validate_root(payload: Mapping[str, Any]) -> None:
    fields = set(payload)
    missing = sorted(_ROOT_FIELDS - fields)
    unknown = sorted(fields - _ROOT_FIELDS)
    if missing:
        raise DailyGrayLaunchdPolicyError(
            f"policy root missing fields: {missing}"
        )
    if unknown:
        raise DailyGrayLaunchdPolicyError(
            f"policy root has unknown fields: {unknown}"
        )
    for field, expected in _ROOT_CONSTANTS.items():
        actual = payload[field]
        if type(actual) is not type(expected) or actual != expected:
            raise DailyGrayLaunchdPolicyError(
                f"{field} must be {expected!r}; got {actual!r}"
            )
    if not isinstance(payload["schemes"], list):
        raise DailyGrayLaunchdPolicyError("schemes must be an array")


def _parse_scheme_rows(value: object) -> tuple[DailyGrayLaunchdSchemePolicy, ...]:
    if not isinstance(value, list):
        raise DailyGrayLaunchdPolicyError("schemes must be an array")
    rows: list[DailyGrayLaunchdSchemePolicy] = []
    scheme_ids: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise DailyGrayLaunchdPolicyError(
                f"schemes[{index}] must be an object"
            )
        row = _parse_scheme_row(raw, index)
        if row.scheme_id in seen:
            raise DailyGrayLaunchdPolicyError(
                f"duplicate scheme_id: {row.scheme_id}"
            )
        seen.add(row.scheme_id)
        scheme_ids.append(row.scheme_id)
        rows.append(row)
    if scheme_ids != sorted(scheme_ids):
        raise DailyGrayLaunchdPolicyError(
            "scheme rows must be sorted by scheme_id"
        )
    return tuple(rows)


def _parse_scheme_row(
    raw: Mapping[str, Any],
    index: int,
) -> DailyGrayLaunchdSchemePolicy:
    fields = set(raw)
    missing = sorted(_REQUIRED_SCHEME_FIELDS - fields)
    unknown = sorted(
        fields - _REQUIRED_SCHEME_FIELDS - _OPTIONAL_SCHEME_FIELDS
    )
    context = f"schemes[{index}]"
    if missing:
        raise DailyGrayLaunchdPolicyError(
            f"{context} missing fields: {missing}"
        )
    if unknown:
        raise DailyGrayLaunchdPolicyError(
            f"{context} has unknown fields: {unknown}"
        )

    scheme_id = _require_nonempty_string(raw, "scheme_id", context)
    scheme_version = _require_nonempty_string(
        raw, "scheme_version", scheme_id
    )
    runtime_type = _require_nonempty_string(raw, "runtime_type", scheme_id)
    frequency = _require_nonempty_string(raw, "frequency", scheme_id)
    if frequency != "daily":
        raise DailyGrayLaunchdPolicyError(
            f"{scheme_id}: frequency must be daily; got {frequency!r}"
        )
    task_type = _require_nonempty_string(raw, "task_type", scheme_id)
    horizon = raw["horizon"]
    if type(horizon) is not int or horizon <= 0:
        raise DailyGrayLaunchdPolicyError(
            f"{scheme_id}: horizon must be a positive integer"
        )
    target_tenors_raw = raw["target_tenors"]
    if not isinstance(target_tenors_raw, list) or not target_tenors_raw:
        raise DailyGrayLaunchdPolicyError(
            f"{scheme_id}: target_tenors must be a non-empty array"
        )
    target_tenors = tuple(target_tenors_raw)
    if any(
        not isinstance(tenor, str) or not tenor.strip()
        for tenor in target_tenors
    ):
        raise DailyGrayLaunchdPolicyError(
            f"{scheme_id}: target_tenors must contain non-empty strings"
        )
    if len(set(target_tenors)) != len(target_tenors):
        raise DailyGrayLaunchdPolicyError(
            f"{scheme_id}: target_tenors contains duplicates"
        )
    execution_class = _require_nonempty_string(
        raw, "execution_class", scheme_id
    )
    if execution_class not in EXECUTION_CLASSES:
        raise DailyGrayLaunchdPolicyError(
            f"{scheme_id}: execution_class must be heavy or light; "
            f"got {execution_class!r}"
        )
    publisher_scheme_id = None
    if "publisher_scheme_id" in raw:
        publisher_scheme_id = _require_nonempty_string(
            raw, "publisher_scheme_id", scheme_id
        )

    return DailyGrayLaunchdSchemePolicy(
        scheme_id=scheme_id,
        scheme_version=scheme_version,
        runtime_type=runtime_type,
        frequency=frequency,
        task_type=task_type,
        horizon=horizon,
        target_tenors=target_tenors,
        execution_class=execution_class,
        publisher_scheme_id=publisher_scheme_id,
    )


def _require_nonempty_string(
    raw: Mapping[str, Any],
    field: str,
    context: str,
) -> str:
    value = raw[field]
    if not isinstance(value, str) or not value.strip():
        raise DailyGrayLaunchdPolicyError(
            f"{context}: {field} must be a non-empty string"
        )
    if value != value.strip():
        raise DailyGrayLaunchdPolicyError(
            f"{context}: {field} must not contain surrounding whitespace"
        )
    return value


def _index_discovery(
    discovered: Iterable[SchemeConfig],
) -> dict[str, SchemeConfig]:
    result: dict[str, SchemeConfig] = {}
    for config in discovered:
        if config.scheme_id in result:
            raise DailyGrayLaunchdPolicyError(
                "strict discovery contains duplicate active daily scheme: "
                f"{config.scheme_id}"
            )
        result[config.scheme_id] = config
    return result


def _validate_identity_sets(
    policy_by_id: Mapping[str, DailyGrayLaunchdSchemePolicy],
    discovered_by_id: Mapping[str, SchemeConfig],
) -> None:
    policy_ids = set(policy_by_id)
    discovered_ids = set(discovered_by_id)
    unknown = sorted(policy_ids - discovered_ids)
    missing = sorted(discovered_ids - policy_ids)
    if unknown:
        raise DailyGrayLaunchdPolicyError(
            f"unknown policy identities absent from active daily discovery: {unknown}"
        )
    if missing:
        raise DailyGrayLaunchdPolicyError(
            f"missing active daily identities from policy: {missing}"
        )


def _validate_cardinality(
    rows: tuple[DailyGrayLaunchdSchemePolicy, ...],
    discovered: tuple[SchemeConfig, ...],
) -> None:
    policy_targets = sum(len(row.target_tenors) for row in rows)
    discovery_targets = sum(len(config.tenors) for config in discovered)
    if len(rows) != EXPECTED_EXECUTION_COUNT:
        raise DailyGrayLaunchdPolicyError(
            "policy execution cardinality drift: "
            f"expected={EXPECTED_EXECUTION_COUNT}, actual={len(rows)}"
        )
    if policy_targets != EXPECTED_TARGET_COUNT:
        raise DailyGrayLaunchdPolicyError(
            "policy target cardinality drift: "
            f"expected={EXPECTED_TARGET_COUNT}, actual={policy_targets}"
        )
    if len(discovered) != EXPECTED_EXECUTION_COUNT:
        raise DailyGrayLaunchdPolicyError(
            "active daily discovery execution cardinality drift: "
            f"expected={EXPECTED_EXECUTION_COUNT}, actual={len(discovered)}"
        )
    if discovery_targets != EXPECTED_TARGET_COUNT:
        raise DailyGrayLaunchdPolicyError(
            "active daily discovery target cardinality drift: "
            f"expected={EXPECTED_TARGET_COUNT}, actual={discovery_targets}"
        )


def _validate_discovery_match(
    row: DailyGrayLaunchdSchemePolicy,
    config: SchemeConfig,
) -> None:
    comparisons = (
        ("scheme_version", row.scheme_version, config.scheme_version),
        ("runtime_type", row.runtime_type, config.runtime_type),
        ("frequency", row.frequency, config.frequency),
        ("task_type", row.task_type, config.task_type),
        ("horizon", row.horizon, config.horizon),
        ("target_tenors", row.target_tenors, tuple(config.tenors)),
    )
    for field, policy_value, discovery_value in comparisons:
        if policy_value != discovery_value:
            raise DailyGrayLaunchdPolicyError(
                f"{row.scheme_id}: {field} drift: "
                f"policy={policy_value!r}, discovery={discovery_value!r}"
            )


def _validate_dependencies(
    policy_by_id: Mapping[str, DailyGrayLaunchdSchemePolicy],
) -> None:
    dependencies: dict[str, str] = {}
    for row in policy_by_id.values():
        publisher = row.publisher_scheme_id
        if publisher is None:
            continue
        if publisher == row.scheme_id:
            raise DailyGrayLaunchdPolicyError(
                f"{row.scheme_id}: publisher self dependency is forbidden"
            )
        publisher_row = policy_by_id.get(publisher)
        if publisher_row is None:
            raise DailyGrayLaunchdPolicyError(
                f"{row.scheme_id}: publisher does not exist: {publisher}"
            )
        if publisher_row.execution_class != "heavy":
            raise DailyGrayLaunchdPolicyError(
                f"{row.scheme_id}: publisher must be heavy: {publisher}"
            )
        dependencies[row.scheme_id] = publisher

    for scheme_id in dependencies:
        visited: set[str] = set()
        current = scheme_id
        while current in dependencies:
            if current in visited:
                raise DailyGrayLaunchdPolicyError(
                    f"dependency cycle detected from {scheme_id}"
                )
            visited.add(current)
            current = dependencies[current]


def _validate_frozen_execution_semantics(
    policy_by_id: Mapping[str, DailyGrayLaunchdSchemePolicy],
) -> None:
    actual_heavy = frozenset(
        row.scheme_id
        for row in policy_by_id.values()
        if row.execution_class == "heavy"
    )
    if actual_heavy != FROZEN_HEAVY_SCHEME_IDS:
        raise DailyGrayLaunchdPolicyError(
            "heavy scheme set drift: "
            f"missing={sorted(FROZEN_HEAVY_SCHEME_IDS - actual_heavy)}, "
            f"unexpected={sorted(actual_heavy - FROZEN_HEAVY_SCHEME_IDS)}"
        )

    actual_publishers = {
        row.scheme_id: row.publisher_scheme_id
        for row in policy_by_id.values()
        if row.publisher_scheme_id is not None
    }
    if actual_publishers != dict(FROZEN_PUBLISHER_MAP):
        raise DailyGrayLaunchdPolicyError(
            "publisher map drift: "
            f"expected={dict(FROZEN_PUBLISHER_MAP)!r}, "
            f"actual={actual_publishers!r}"
        )

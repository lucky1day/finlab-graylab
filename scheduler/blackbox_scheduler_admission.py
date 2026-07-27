"""Blackbox V2 自动调度的版本化精确身份准入。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


SCHEMA_VERSION = "blackbox-scheduler-admission-v1"
VALID_MODES = frozenset({"formal", "gray"})
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADMISSION_PATH = (
    PROJECT_ROOT / "deploy" / "blackbox_scheduler_admission_v1.json"
)
_ROOT_FIELDS = frozenset({"schema_version", "schemes"})
_SCHEME_FIELDS = frozenset(
    {"scheme_id", "scheme_version", "mode"}
)
EXPECTED_EXACT_ADMISSIONS: Mapping[tuple[str, str], str] = (
    MappingProxyType(
        {
            (
                "one_y_t5_liq_excess_a_v1",
                "8d583560c9f1",
            ): "formal",
            (
                "one_y_t5_liq_excess_a_w252_l7_v1",
                "103c93bbc913",
            ): "formal",
            (
                "one_y_t5_liq_excess_a_w350_l7_v1",
                "86b458c568a5",
            ): "formal",
            (
                "one_y_t5_liq_excess_b_w252_l7_v1",
                "ba00891cd179",
            ): "formal",
            (
                "weekly_10y_lgbm_point_v1",
                "0666a6989d6b",
            ): "formal",
            (
                "cgb_a4_fundseason_1y",
                "04e7af163fb0",
            ): "gray",
            (
                "cgb_a4_fundseason_3y",
                "89d31f8bcb95",
            ): "gray",
            (
                "cgb_a4_fundseason_5y",
                "7d47e0328532",
            ): "gray",
            (
                "cgb_a4_fundseason_7y",
                "ddba87ece7ae",
            ): "gray",
            (
                "cgb_a4_fundseason_10y",
                "85a65700499b",
            ): "gray",
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

    entries: Mapping[tuple[str, str], str]

    def mode(self, config: object) -> str | None:
        """返回 Blackbox 精确身份的模式；未列出或非 Blackbox 返回空。"""
        if getattr(config, "runtime_type", "native_adapter") != "blackbox_v2":
            return None
        identity = (
            str(getattr(config, "scheme_id", "")).strip(),
            str(getattr(config, "scheme_version", "")).strip(),
        )
        return self.entries.get(identity)

    def is_scheduled(self, config: object) -> bool:
        """Native 保持原行为；Blackbox 仅 formal 精确身份可自动调度。"""
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
        return self.mode(config) == "formal"


def load_blackbox_scheduler_admission(
    path: str | Path = DEFAULT_ADMISSION_PATH,
) -> BlackboxSchedulerAdmissionPolicy:
    """加载 Blackbox 自动调度 admission，配置异常时 fail-closed。"""
    admission_path = Path(path)
    try:
        payload = json.loads(
            admission_path.read_text(encoding="utf-8")
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

    entries: dict[tuple[str, str], str] = {}
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
        entries[identity] = mode

    if entries != EXPECTED_EXACT_ADMISSIONS:
        expected_ids = set(EXPECTED_EXACT_ADMISSIONS)
        actual_ids = set(entries)
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        mode_drift = sorted(
            identity
            for identity in expected_ids & actual_ids
            if entries[identity]
            != EXPECTED_EXACT_ADMISSIONS[identity]
        )
        raise BlackboxSchedulerAdmissionError(
            "Blackbox scheduler admission must equal exact frozen "
            f"admissions: missing={missing} extra={extra} "
            f"mode_drift={mode_drift}"
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

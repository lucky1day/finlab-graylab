"""Blackbox V2 平台输入制品的纯配置注册表。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


VERSIONED_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*-v[1-9][0-9]*$")


@dataclass(frozen=True)
class PlatformInputSpec:
    """一个版本化平台输入制品的静态契约。"""

    artifact_id: str
    provider_version: str
    filename: str
    columns: tuple[str, ...]


class PlatformInputRegistry:
    """封闭注册表，负责校验并规范化方案声明。"""

    def __init__(self, specs: Iterable[PlatformInputSpec]) -> None:
        by_id: dict[str, PlatformInputSpec] = {}
        filenames: set[str] = set()
        for spec in specs:
            self._validate_spec(spec)
            if spec.artifact_id in by_id:
                raise ValueError(
                    "platform input registry contains duplicate artifact_id: "
                    f"{spec.artifact_id}"
                )
            if spec.filename in filenames:
                raise ValueError(
                    "platform input registry contains duplicate filename: "
                    f"{spec.filename}"
                )
            by_id[spec.artifact_id] = spec
            filenames.add(spec.filename)
        self._by_id: Mapping[str, PlatformInputSpec] = MappingProxyType(by_id)

    def get(self, artifact_id: str) -> PlatformInputSpec:
        """按稳定 ID 返回制品契约，未知 ID fail-closed。"""
        try:
            return self._by_id[artifact_id]
        except KeyError as exc:
            raise ValueError(
                f"platform_inputs contains unknown artifact ID: {artifact_id!r}"
            ) from exc

    def normalize_ids(self, value: object) -> tuple[str, ...]:
        """校验非空、唯一声明并返回按 ID 排序的 tuple。"""
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError(
                "platform_inputs must be a non-empty list of registered IDs"
            )
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    "platform_inputs must contain non-empty string IDs"
                )
            artifact_id = item.strip()
            if not VERSIONED_ID_PATTERN.fullmatch(artifact_id):
                raise ValueError(
                    "platform_inputs must contain versioned IDs ending in -vN"
                )
            if artifact_id in seen:
                raise ValueError(
                    f"platform_inputs contains duplicate ID: {artifact_id}"
                )
            self.get(artifact_id)
            normalized.append(artifact_id)
            seen.add(artifact_id)
        return tuple(sorted(normalized))

    @staticmethod
    def _validate_spec(spec: PlatformInputSpec) -> None:
        if not isinstance(spec, PlatformInputSpec):
            raise ValueError("platform input registry entries must be specs")
        if not VERSIONED_ID_PATTERN.fullmatch(spec.artifact_id):
            raise ValueError(
                "platform input artifact_id must be a versioned ID ending in -vN"
            )
        if not VERSIONED_ID_PATTERN.fullmatch(spec.provider_version):
            raise ValueError(
                "platform input provider_version must be a versioned ID ending in -vN"
            )
        path = Path(spec.filename)
        if (
            not spec.filename
            or "/" in spec.filename
            or "\\" in spec.filename
            or path.is_absolute()
            or len(path.parts) != 1
            or path.name != spec.filename
            or spec.filename in {".", ".."}
        ):
            raise ValueError(
                "platform input filename must be one safe relative basename"
            )
        if (
            not isinstance(spec.columns, tuple)
            or not spec.columns
            or any(not isinstance(column, str) or not column for column in spec.columns)
            or len(set(spec.columns)) != len(spec.columns)
        ):
            raise ValueError(
                "platform input columns must be a non-empty unique string tuple"
            )


PLATFORM_INPUT_REGISTRY = PlatformInputRegistry(
    (
        PlatformInputSpec(
            artifact_id="api-wind-date-v1",
            provider_version="api-wind-date-provider-v1",
            filename="api_wind_date.csv",
            columns=("rdate", "week_id"),
        ),
    )
)


def normalize_platform_input_ids(value: object) -> tuple[str, ...]:
    """使用平台注册表规范化一组制品 ID。"""
    return PLATFORM_INPUT_REGISTRY.normalize_ids(value)

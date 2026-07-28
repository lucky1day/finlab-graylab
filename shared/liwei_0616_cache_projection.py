"""Liwei 日频缓存的有效周月辅助输入投影。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import numpy as np
import pandas as pd


PROJECTION_SCHEMA_VERSION = "liwei-0616-auxiliary-dependency-projection-v1"


@dataclass(frozen=True, init=False)
class AuxiliaryDependencyProjection:
    """绑定有效辅助输入帧及其可复核证明。"""

    _frame: pd.DataFrame = field(repr=False)
    _proof: Mapping[str, object] = field(repr=False)
    content_sha256: str

    def __init__(
        self,
        frame: pd.DataFrame,
        proof: Mapping[str, object],
        content_sha256: str,
    ) -> None:
        object.__setattr__(self, "_frame", frame.copy(deep=True))
        object.__setattr__(self, "_proof", _deep_freeze(proof))
        object.__setattr__(self, "content_sha256", content_sha256)

    @property
    def frame(self) -> pd.DataFrame:
        """返回与内部已哈希帧隔离的副本。"""
        return self._frame.copy(deep=True)

    @property
    def proof(self) -> Mapping[str, object]:
        """返回与内部递归冻结证明隔离的可读副本。"""
        return cast(dict[str, object], _deep_thaw(self._proof))


def build_auxiliary_dependency_projection(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: Mapping[str, int | str] | None,
    prepare_model_frames: Callable[
        ..., tuple[pd.DataFrame, pd.DataFrame]
    ],
    build_wkmo_features: Callable[
        [pd.DataFrame, pd.DataFrame], pd.DataFrame
    ],
    proof_files: tuple[Path, ...],
) -> AuxiliaryDependencyProjection:
    """通过方案注入的精确 core 回调构造纯共享有效输入投影。"""
    normalized_daily = _normalize_daily_frame(daily_df)
    normalized_date_to_week = _normalize_date_to_week(date_to_week)
    date_to_week_mode = (
        "fallback" if normalized_date_to_week is None else "explicit"
    )
    daily_dates = normalized_daily["date"]

    aligned_weekly, aligned_monthly = prepare_model_frames(
        daily_df=normalized_daily,
        weekly_df=weekly_df,
        monthly_df=monthly_df,
        daily_dates=daily_dates,
        date_to_week=normalized_date_to_week,
    )
    if not isinstance(aligned_weekly, pd.DataFrame) or not isinstance(
        aligned_monthly, pd.DataFrame
    ):
        raise ValueError("prepare_model_frames must return two dataframes")

    features = build_wkmo_features(aligned_weekly, aligned_monthly)
    if not isinstance(features, pd.DataFrame):
        raise ValueError("build_wkmo_features must return a dataframe")
    if len(features) != len(normalized_daily):
        raise ValueError("effective auxiliary feature rows must match daily grid")
    if features.columns.has_duplicates:
        raise ValueError("effective auxiliary feature columns must be unique")
    if "date" in features.columns:
        raise ValueError("effective auxiliary features must not contain date")

    frame = features.reset_index(drop=True).copy()
    normalized_dates = daily_dates.dt.strftime("%Y-%m-%d").tolist()
    frame.insert(0, "date", normalized_dates)
    if frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
        raise ValueError("projection dates must be unique and monotonic")

    mapping_payload = (
        []
        if normalized_date_to_week is None
        else [
            [key, normalized_date_to_week[key]]
            for key in sorted(normalized_date_to_week)
        ]
    )
    mapping_entries = [
        {"date": key, "week_id": week_id}
        for key, week_id in mapping_payload
    ]
    proof: dict[str, object] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "date_to_week_mode": date_to_week_mode,
        "date_to_week_sha256": _sha256_json(mapping_payload),
        "date_to_week_entries": mapping_entries,
        "proof_files": _proof_file_records(proof_files),
        "columns": list(frame.columns),
        "dtypes": [str(frame[column].dtype) for column in frame.columns],
        "daily_grid_sha256": _sha256_json(normalized_dates),
        "feature_cutoff": normalized_dates[-1],
    }
    content_payload = {
        "proof": proof,
        "rows": [
            [_encode_scalar(value) for value in row]
            for row in frame.itertuples(index=False, name=None)
        ],
    }
    return AuxiliaryDependencyProjection(
        frame=frame,
        proof=proof,
        content_sha256=_sha256_json(content_payload),
    )


def _normalize_daily_frame(daily_df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(daily_df, pd.DataFrame):
        raise ValueError("daily_df must be a dataframe")
    if "date" not in daily_df.columns:
        raise ValueError("daily dataframe missing date column")
    if daily_df.empty:
        raise ValueError("daily dataframe must not be empty")

    dates: list[pd.Timestamp] = []
    for raw_date in daily_df["date"]:
        try:
            parsed = pd.Timestamp(raw_date)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("daily dataframe contains invalid date") from exc
        if pd.isna(parsed) or parsed.tzinfo is not None:
            raise ValueError("daily dataframe contains invalid date")
        dates.append(parsed.normalize())

    normalized = daily_df.copy()
    normalized["date"] = pd.Series(dates, index=normalized.index)
    if normalized["date"].duplicated().any():
        raise ValueError("daily dataframe contains duplicate dates")
    return normalized.sort_values(
        "date", kind="stable"
    ).reset_index(drop=True)


def _normalize_date_to_week(
    date_to_week: Mapping[str, int | str] | None,
) -> dict[str, int] | None:
    if date_to_week is None:
        return None
    if not isinstance(date_to_week, Mapping):
        raise ValueError("date_to_week must be a mapping or None")

    normalized: dict[str, int] = {}
    for raw_date, raw_week in date_to_week.items():
        try:
            parsed = pd.Timestamp(raw_date)
            week_id = int(raw_week)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("date_to_week contains invalid entry") from exc
        if pd.isna(parsed) or parsed.tzinfo is not None:
            raise ValueError("date_to_week contains invalid date")
        date_key = parsed.normalize().strftime("%Y-%m-%d")
        if date_key in normalized:
            raise ValueError("date_to_week contains duplicate normalized dates")
        normalized[date_key] = week_id
    return {key: normalized[key] for key in sorted(normalized)}


def _proof_file_records(proof_files: tuple[Path, ...]) -> list[dict[str, str]]:
    if not proof_files:
        raise ValueError("proof files must not be empty")

    records: list[dict[str, str]] = []
    for raw_path in proof_files:
        path = Path(raw_path)
        if not path.is_file():
            raise ValueError(f"proof file is missing or not a regular file: {path}")
        records.append(
            {
                "name": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return records


def _encode_scalar(value: Any) -> dict[str, object]:
    if value is None or value is pd.NA or value is pd.NaT:
        return {"type": "null", "value": None}
    if isinstance(value, (np.bool_, bool)):
        return {"type": "bool", "value": bool(value)}
    if isinstance(value, (np.integer, int)):
        return {"type": "int", "value": str(int(value))}
    if isinstance(value, (np.floating, float)):
        number = float(value)
        if np.isnan(number):
            return {"type": "null", "value": None}
        return {"type": "float", "value": number.hex()}
    if isinstance(value, pd.Timestamp):
        return {"type": "timestamp", "value": value.isoformat()}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    raise ValueError(
        "effective auxiliary projection contains unsupported scalar "
        f"{type(value).__name__}"
    )


def _sha256_json(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("projection proof is not canonical JSON data") from exc
    return hashlib.sha256(payload).hexdigest()


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                deepcopy(key): _deep_freeze(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return deepcopy(value)


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            deepcopy(key): _deep_thaw(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return deepcopy(value)

"""基于源数据与冻结日历的输入截止键解析。"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import (
    date,
    datetime,
)
from pathlib import Path
from typing import (
    Any,
    Iterable,
    Mapping,
)
import pandas as pd

from shared import data_service as _data_service
from shared.blackbox_v2.snapshot import (
    BlackboxSnapshot,
    CutoffKeys,
    normalize_daily_key,
    normalize_period_key,
)
from shared._input_artifacts.contract import (
    BLACKBOX_SCHEMA_PATH,
    _load_blackbox_schema,
)


def resolve_blackbox_input_cutoffs(
    snapshot: BlackboxSnapshot,
    *,
    feature_date: str,
    engine=None,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
) -> CutoffKeys:
    """从数据桥 as-of 输出解析截止键，不在算法层推导周/月标识。"""
    _, expected_columns = _load_blackbox_schema(schema_path)
    own_engine = engine is None
    engine = engine or _data_service.create_sqlalchemy_engine()
    try:
        weekly_as_of = _data_service.build_weekly_output_from_db(
            schema_columns=expected_columns["weekly_output.csv"],
            as_of_date=feature_date,
            engine=engine,
        )
        monthly_as_of = _data_service.build_monthly_output_from_db(
            end_date=feature_date,
            engine=engine,
            include_databridge_additions=True,
        )
        snapshot_keys = _load_snapshot_cutoff_keys(snapshot)
        normalized_feature_date = _normalize_feature_date(feature_date)
        daily_position = bisect_right(
            snapshot_keys["date"],
            normalized_feature_date,
        )
        if daily_position == 0:
            raise ValueError(
                "daily snapshot has no row on or before "
                f"{normalized_feature_date}"
            )
        daily_cutoff_key = snapshot_keys["date"][daily_position - 1]
        weekly_cutoff_key = _cached_authoritative_cutoff(
                weekly_as_of,
                key_column="week_id",
                available_keys=snapshot_keys["week_id"],
                filename="weekly_output.csv",
            )
        _require_snapshot_calendar_week_id(
            snapshot_keys,
            daily_cutoff_key=daily_cutoff_key,
            weekly_cutoff_key=weekly_cutoff_key,
        )
        return CutoffKeys(
            daily_cutoff_key=daily_cutoff_key,
            weekly_cutoff_key=weekly_cutoff_key,
            monthly_cutoff_key=_cached_authoritative_cutoff(
                monthly_as_of,
                key_column="month_id",
                available_keys=snapshot_keys["month_id"],
                filename="monthly_output.csv",
            ),
        )
    finally:
        if own_engine:
            engine.dispose()


def resolve_blackbox_input_cutoffs_bulk(
    snapshot: BlackboxSnapshot,
    *,
    feature_dates: Iterable[str],
    engine=None,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
) -> dict[str, CutoffKeys]:
    """一次读取平台来源与快照，为多个 feature_date 解析三频截止键。"""
    normalized_dates = tuple(dict.fromkeys(
        _normalize_feature_date(value) for value in feature_dates
    ))
    if not normalized_dates:
        return {}
    snapshot_keys = _load_snapshot_cutoff_keys(snapshot)
    own_engine = engine is None
    engine = engine or _data_service.create_sqlalchemy_engine()
    try:
        return _resolve_blackbox_input_cutoffs_bulk_from_keys(
            snapshot_keys,
            feature_dates=normalized_dates,
            connection=engine,
            schema_path=schema_path,
        )
    finally:
        if own_engine:
            engine.dispose()


def _resolve_blackbox_input_cutoffs_bulk_from_keys(
    snapshot_keys: Mapping[str, Any],
    *,
    feature_dates: Iterable[str],
    connection: Any,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
) -> dict[str, CutoffKeys]:
    """使用已冻结 key 集合和 caller Connection 批量解析有效截止键。"""
    resolved = _resolve_blackbox_input_cutoffs_with_source_keys_bulk_from_keys(
        snapshot_keys,
        feature_dates=feature_dates,
        connection=connection,
        schema_path=schema_path,
    )
    return {
        feature_date: item.cutoff_keys
        for feature_date, item in resolved.items()
    }


@dataclass(frozen=True, slots=True)
class _ResolvedPeriodCutoff:
    """同一 source snapshot 内的 exact key 与实际连续性 key。"""

    source_key: str
    effective_key: str


@dataclass(frozen=True, slots=True)
class _ResolvedBlackboxInputCutoffs:
    """保留 authority 所需 source period key，外部仍只接收 CutoffKeys。"""

    cutoff_keys: CutoffKeys
    source_weekly_cutoff_key: str
    source_monthly_cutoff_key: str


def _resolve_blackbox_input_cutoffs_with_source_keys_bulk_from_keys(
    snapshot_keys: Mapping[str, Any],
    *,
    feature_dates: Iterable[str],
    connection: Any,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
    allow_legacy_v1_period_fallback: bool = False,
) -> dict[str, _ResolvedBlackboxInputCutoffs]:
    """解析有效 cutoff，并保留 fallback 前的 source weekly/monthly key。"""
    normalized_dates = list(dict.fromkeys(
        _normalize_feature_date(value) for value in feature_dates
    ))
    if not normalized_dates:
        return {}

    _, expected_columns = _load_blackbox_schema(schema_path)
    metadata = _data_service.read_factor_metadata_from_db(connection)
    weekly_codes = expected_columns["weekly_output.csv"][1:]
    weekly_raw = _data_service.read_weekly_long_from_db(
        weekly_codes,
        "api_wind_weekly",
        connection,
    )
    weekly_derivative = _data_service.read_weekly_long_from_db(
        weekly_codes,
        "api_wind_derivative_weekly",
        connection,
    )
    monthly_selected = _data_service.select_monthly_factor_metadata(
        metadata,
        include_databridge_additions=True,
    )
    monthly_codes = (
        monthly_selected["indicators_code"]
        .astype(str)
        .str.strip()
        .tolist()
    )
    monthly_raw = _data_service.read_monthly_long_from_db(
        monthly_codes,
        "api_wind_monthly",
        connection,
    )
    monthly_derivative = _data_service.read_monthly_long_from_db(
        monthly_codes,
        "api_wind_derivative_monthly",
        connection,
        include_month_id=True,
    )
    max_date = max(normalized_dates)
    weekly_index = _data_service.build_weekly_cutoff_index_from_frames(
        expected_columns["weekly_output.csv"],
        weekly_raw,
        weekly_derivative,
        end_date=max_date,
    )
    monthly_index = _data_service.build_monthly_cutoff_index_from_frames(
        metadata,
        monthly_raw,
        monthly_derivative,
        end_date=max_date,
        include_databridge_additions=True,
    )
    daily_dates = snapshot_keys["date"]
    weekly_by_date = _resolve_period_cutoffs_bulk(
        normalized_dates,
        weekly_index,
        key_column="week_id",
        available_keys=snapshot_keys["week_id"],
        filename="weekly_output.csv",
        allow_legacy_v1_period_fallback=(
            allow_legacy_v1_period_fallback
        ),
    )
    monthly_by_date = _resolve_period_cutoffs_bulk(
        normalized_dates,
        monthly_index,
        key_column="month_id",
        available_keys=snapshot_keys["month_id"],
        filename="monthly_output.csv",
        allow_legacy_v1_period_fallback=(
            allow_legacy_v1_period_fallback
        ),
    )
    resolved: dict[str, _ResolvedBlackboxInputCutoffs] = {}
    for feature_date in normalized_dates:
        daily_position = bisect_right(daily_dates, feature_date)
        if daily_position == 0:
            raise ValueError(f"daily snapshot has no row on or before {feature_date}")
        weekly = weekly_by_date[feature_date]
        monthly = monthly_by_date[feature_date]
        daily_cutoff_key = daily_dates[daily_position - 1]
        if "calendar_week_id_by_date" in snapshot_keys:
            _require_snapshot_calendar_week_id(
                snapshot_keys,
                daily_cutoff_key=daily_cutoff_key,
                weekly_cutoff_key=weekly.effective_key,
            )
        resolved[feature_date] = _ResolvedBlackboxInputCutoffs(
            cutoff_keys=CutoffKeys(
                daily_cutoff_key=daily_cutoff_key,
                weekly_cutoff_key=weekly.effective_key,
                monthly_cutoff_key=monthly.effective_key,
            ),
            source_weekly_cutoff_key=weekly.source_key,
            source_monthly_cutoff_key=monthly.source_key,
        )
    return resolved


def _require_snapshot_calendar_week_id(
    snapshot_keys: Mapping[str, Any],
    *,
    daily_cutoff_key: str,
    weekly_cutoff_key: str,
) -> None:
    calendar = snapshot_keys.get("calendar_week_id_by_date")
    if not isinstance(calendar, Mapping):
        raise ValueError("snapshot calendar index is missing")
    frozen_week_id = calendar.get(daily_cutoff_key)
    if frozen_week_id != weekly_cutoff_key:
        raise ValueError(
            "Request weekly_cutoff_key does not match frozen calendar: "
            f"{daily_cutoff_key} -> {frozen_week_id}, got {weekly_cutoff_key}"
        )


def validate_blackbox_request_calendar(
    snapshot: BlackboxSnapshot,
    *,
    daily_cutoff_key: str,
    weekly_cutoff_key: str,
) -> None:
    """以 generation 缓存索引校验 Request 周历映射。"""
    calendar = snapshot.calendar_week_ids_by_date
    if isinstance(calendar, Mapping):
        frozen_week_id = calendar.get(daily_cutoff_key)
        if frozen_week_id != weekly_cutoff_key:
            raise ValueError(
                "Request weekly_cutoff_key does not match frozen calendar: "
                f"{daily_cutoff_key} -> {frozen_week_id}, "
                f"got {weekly_cutoff_key}"
            )
        return
    _require_snapshot_calendar_week_id(
        _load_snapshot_cutoff_keys(snapshot),
        daily_cutoff_key=daily_cutoff_key,
        weekly_cutoff_key=weekly_cutoff_key,
    )


def _load_snapshot_cutoff_keys(snapshot: BlackboxSnapshot) -> dict[str, Any]:
    cached = (
        snapshot.daily_cutoff_keys,
        snapshot.weekly_cutoff_keys,
        snapshot.monthly_cutoff_keys,
        snapshot.calendar_week_ids_by_date,
    )
    if all(value is not None for value in cached):
        return {
            "date": list(snapshot.daily_cutoff_keys or ()),
            "week_id": set(snapshot.weekly_cutoff_keys or ()),
            "month_id": set(snapshot.monthly_cutoff_keys or ()),
            "calendar_week_id_by_date": dict(
                snapshot.calendar_week_ids_by_date or {}
            ),
        }
    paths = {
        "date": snapshot.data_dir / "daily_output.csv",
        "week_id": snapshot.data_dir / "weekly_output.csv",
        "month_id": snapshot.data_dir / "monthly_output.csv",
    }
    loaded: dict[str, Any] = {}
    for key, path in paths.items():
        try:
            frame = pd.read_csv(path, usecols=[key], dtype={key: "string"})
        except ValueError as exc:
            raise ValueError(f"{path.name} is missing {key} cutoff column") from exc
        if key == "date":
            parsed = pd.to_datetime(frame[key], errors="coerce").dt.date
            if parsed.isna().any():
                raise ValueError("daily_output.csv contains an invalid date cutoff key")
            values = [value.isoformat() for value in parsed]
            if values != sorted(set(values)):
                raise ValueError("daily_output.csv date cutoff keys must be unique and ascending")
            loaded[key] = values
            continue
        values = [normalize_period_key(value, key) for value in frame[key].tolist()]
        if values != sorted(set(values)):
            raise ValueError(f"{path.name} {key} cutoff keys must be unique and ascending")
        loaded[key] = set(values)
    calendar_path = snapshot.data_dir / "api_wind_date.csv"
    try:
        calendar = pd.read_csv(
            calendar_path,
            usecols=["rdate", "week_id"],
            dtype="string",
            keep_default_na=False,
        )
    except ValueError as exc:
        raise ValueError(
            "api_wind_date.csv is missing calendar columns"
        ) from exc
    calendar_pairs = [
        (
            normalize_daily_key(row.rdate),
            normalize_period_key(row.week_id, "week_id"),
        )
        for row in calendar.itertuples(index=False)
    ]
    if not calendar_pairs or calendar_pairs != sorted(set(calendar_pairs)):
        raise ValueError(
            "api_wind_date.csv calendar keys must be unique and ascending"
        )
    loaded["calendar_week_id_by_date"] = dict(calendar_pairs)
    return loaded


def _cached_authoritative_cutoff(
    as_of: pd.DataFrame,
    *,
    key_column: str,
    available_keys: set[str],
    filename: str,
) -> str:
    if key_column not in as_of.columns or as_of.empty:
        raise ValueError(f"platform as-of data has no {key_column}")
    cutoff = normalize_period_key(as_of[key_column].tolist()[-1], key_column)
    if cutoff not in available_keys:
        raise ValueError(
            f"platform {key_column} cutoff {cutoff} does not exist in {filename}"
        )
    return cutoff


def _resolve_period_cutoffs_bulk(
    feature_dates: list[str],
    index: pd.DataFrame,
    *,
    key_column: str,
    available_keys: set[str],
    filename: str,
    allow_legacy_v1_period_fallback: bool = False,
) -> dict[str, _ResolvedPeriodCutoff]:
    required = {key_column, "available_date"}
    if index.empty or not required.issubset(index.columns):
        raise ValueError(f"platform as-of data has no {key_column}")
    events = sorted(
        (
            _normalize_feature_date(value),
            normalize_period_key(key, key_column),
        )
        for value, key in zip(index["available_date"], index[key_column])
    )
    result: dict[str, _ResolvedPeriodCutoff] = {}
    latest: str | None = None
    position = 0
    for feature_date in sorted(feature_dates):
        while position < len(events) and events[position][0] <= feature_date:
            key = events[position][1]
            latest = key if latest is None or key > latest else latest
            position += 1
        if latest is None:
            raise ValueError(f"platform as-of data has no {key_column} for {feature_date}")
        selected = latest
        if selected not in available_keys:
            if allow_legacy_v1_period_fallback:
                prior_keys = [
                    key for key in available_keys if key <= selected
                ]
                if prior_keys:
                    selected = max(prior_keys)
            if selected not in available_keys:
                raise ValueError(
                    f"platform {key_column} cutoff {latest} does not exist in {filename}"
                )
        result[feature_date] = _ResolvedPeriodCutoff(
            source_key=latest,
            effective_key=selected,
        )
    return result


def _normalize_feature_date(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("feature_date must use YYYY-MM-DD") from exc

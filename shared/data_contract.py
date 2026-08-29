"""Native 源快照水位证据。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

import pandas as pd

SHANGHAI = ZoneInfo("Asia/Shanghai")
FACTOR_SOURCE_TABLES = (
    "api_wind_daily",
    "api_wind_derivative_daily",
    "api_wind_weekly",
    "api_wind_derivative_weekly",
    "api_wind_monthly",
    "api_wind_derivative_monthly",
)
METADATA_SOURCE_TABLE = "api_wind_indicators_all"
CALENDAR_SOURCE_TABLES = (
    "api_wind_date",
    "t_trade_calendar",
)


@dataclass(frozen=True)
class SourceTableEvidence:
    """一个源表在 generation snapshot 内的只读水位证据。"""

    table_name: str
    row_count: int
    latest_create_time: str | None
    latest_update_time: str | None = None
    latest_business_key: str | None = None


@dataclass(frozen=True)
class SourceCommitEvidence:
    """用于 Native manifest 的稳定源快照水位 token。"""

    feature_date: str
    source_commit_token: str
    tables: tuple[SourceTableEvidence, ...]


def capture_source_commit_evidence_from_connection(
    connection: Connection,
    *,
    feature_date: str,
    metadata: pd.DataFrame | None = None,
) -> SourceCommitEvidence:
    """在调用方现有事务/一致性快照中采集同一份源水位证据。"""
    normalized_feature_date = _canonical_date(feature_date)
    evidence: list[SourceTableEvidence] = []
    for table_name in FACTOR_SOURCE_TABLES:
        rows = connection.execute(
            text(
                f"""
                SELECT rdate,
                       COUNT(*) AS row_count,
                       MAX(create_time) AS latest_create_time
                FROM {table_name}
                WHERE rdate <= :feature_date
                GROUP BY rdate
                """
            ),
            {"feature_date": normalized_feature_date},
        ).mappings().all()
        row_count, latest_create_time = _aggregate_factor_rows(
            rows,
            feature_date=normalized_feature_date,
            count_field="row_count",
            timestamp_field="latest_create_time",
        )
        evidence.append(
            SourceTableEvidence(
                table_name=table_name,
                row_count=row_count,
                latest_create_time=latest_create_time,
            )
        )
    if metadata is None:
        metadata_record = connection.execute(
            text(
                f"""
                SELECT COUNT(*) AS row_count,
                       MAX(create_time) AS latest_create_time,
                       MAX(update_time) AS latest_update_time
                FROM {METADATA_SOURCE_TABLE}
                """
            )
        ).mappings().one()
    else:
        required = {"create_time", "update_time"}
        missing = required - set(metadata.columns)
        if missing:
            raise ValueError(
                "preloaded factor metadata is missing source evidence "
                f"columns: {sorted(missing)}"
            )
        metadata_record = {
            "row_count": len(metadata),
            "latest_create_time": _dataframe_max(metadata["create_time"]),
            "latest_update_time": _dataframe_max(metadata["update_time"]),
        }
    evidence.append(
        SourceTableEvidence(
            table_name=METADATA_SOURCE_TABLE,
            row_count=int(metadata_record["row_count"]),
            latest_create_time=_naive_timestamp_text(
                metadata_record["latest_create_time"]
            ),
            latest_update_time=_naive_timestamp_text(
                metadata_record["latest_update_time"]
            ),
        )
    )
    for table_name in CALENDAR_SOURCE_TABLES:
        row = connection.execute(
            text(
                f"""
                SELECT COUNT(*) AS row_count,
                       MAX(rdate) AS latest_business_key
                FROM {table_name}
                WHERE rdate <= :feature_date
                """
            ),
            {"feature_date": normalized_feature_date},
        ).mappings().one()
        evidence.append(
            SourceTableEvidence(
                table_name=table_name,
                row_count=int(row["row_count"]),
                latest_create_time=None,
                latest_business_key=(
                    str(row["latest_business_key"])
                    if row["latest_business_key"] is not None
                    else None
                ),
            )
        )
    ordered = tuple(sorted(evidence, key=lambda item: item.table_name))
    evidence_record = SourceCommitEvidence(
        feature_date=normalized_feature_date,
        source_commit_token="",
        tables=ordered,
    )
    return SourceCommitEvidence(
        feature_date=evidence_record.feature_date,
        source_commit_token=source_commit_evidence_sha256(
            evidence_record
        ),
        tables=evidence_record.tables,
    )


def _dataframe_max(series: pd.Series) -> object | None:
    values = series.dropna()
    if values.empty:
        return None
    return values.max()


def source_commit_evidence_payload(
    evidence: SourceCommitEvidence,
) -> dict[str, object]:
    """返回可重算源水位 token 的规范、非敏感审计载荷。"""
    if not isinstance(evidence, SourceCommitEvidence):
        raise TypeError("evidence must be SourceCommitEvidence")
    normalized_feature_date = _canonical_date(evidence.feature_date)
    ordered = tuple(
        sorted(evidence.tables, key=lambda item: item.table_name)
    )
    return {
        "evidence_version": "native-source-watermark-v1",
        "feature_date": normalized_feature_date,
        "tables": [asdict(item) for item in ordered],
    }


def source_commit_evidence_sha256(
    evidence: SourceCommitEvidence,
) -> str:
    """重算 ``SourceCommitEvidence`` 的稳定水位摘要。"""
    return hashlib.sha256(
        json.dumps(
            source_commit_evidence_payload(evidence),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def assert_source_commit_evidence_at_cutoff(
    evidence: SourceCommitEvidence,
    *,
    cutoff_at: datetime,
) -> None:
    """拒绝一致性快照中任何已知水位晚于调用方给定边界。"""
    if not isinstance(evidence, SourceCommitEvidence):
        raise TypeError("evidence must be SourceCommitEvidence")
    if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
        raise ValueError("cutoff_at must be timezone-aware")
    localized_cutoff = cutoff_at.astimezone(SHANGHAI)
    late: list[str] = []
    for table in evidence.tables:
        for field, value in (
            ("latest_create_time", table.latest_create_time),
            ("latest_update_time", table.latest_update_time),
        ):
            observed = _source_timestamp(value)
            if observed is not None and observed > localized_cutoff:
                late.append(
                    f"{table.table_name}.{field}="
                    f"{observed.isoformat(timespec='microseconds')}"
                )
    if late:
        raise RuntimeError(
            "source snapshot contains writes after the supplied cutoff: "
            + ", ".join(sorted(late))
        )


def _aggregate_factor_rows(
    rows: Iterable[dict[str, object]],
    *,
    feature_date: str,
    count_field: str,
    timestamp_field: str,
) -> tuple[int, str | None]:
    """聚合 Native 实际消费域 ``rdate <= feature_date`` 的水位。"""
    cutoff = date.fromisoformat(feature_date)
    row_count = 0
    latest: datetime | None = None
    for row in rows:
        if _source_date(row.get("rdate")) > cutoff:
            continue
        count = int(row[count_field])
        if count < 0:
            raise RuntimeError(f"{count_field} must be non-negative")
        row_count += count
        observed = _source_timestamp(row.get(timestamp_field))
        if observed is not None and (latest is None or observed > latest):
            latest = observed
    return (
        row_count,
        (
            latest.replace(tzinfo=None).isoformat(timespec="microseconds")
            if latest is not None
            else None
        ),
    )


def _canonical_date(value: str) -> str:
    normalized = date.fromisoformat(str(value)).isoformat()
    if normalized != value:
        raise ValueError("feature_date must use canonical YYYY-MM-DD")
    return normalized


def _source_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    normalized = str(value or "").strip().split(" ", 1)[0].replace("/", "-")
    parts = normalized.split("-")
    if len(parts) != 3:
        raise RuntimeError(f"source rdate is invalid: {value!r}")
    try:
        return date(*(int(part) for part in parts))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"source rdate is invalid: {value!r}") from exc


def _source_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace(" ", "T"))
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _naive_timestamp_text(value: object) -> str | None:
    parsed = _source_timestamp(value)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=None).isoformat(timespec="microseconds")

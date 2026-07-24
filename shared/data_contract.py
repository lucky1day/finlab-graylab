"""06:30 Native 源数据封账证据与晚写只读审计。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


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
    """一个源表在 generation cutoff 上的只读水位证据。"""

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


@dataclass(frozen=True)
class LateSourceWrite:
    """06:30 后写入当日 cutoff 输入域的一类契约违约。"""

    table_name: str
    late_row_count: int
    latest_write_at: str
    feature_date: str
    cutoff_at: str


def capture_source_commit_evidence(
    engine: Engine,
    *,
    feature_date: str,
) -> SourceCommitEvidence:
    """采集 cutoff 水位并生成确定性 SHA-256 token。

    该 token 是源表水位证据，不替代 generation 自身逐文件内容哈希。
    """
    with engine.connect() as connection:
        return capture_source_commit_evidence_from_connection(
            connection,
            feature_date=feature_date,
        )


def capture_source_commit_evidence_from_connection(
    connection: Connection,
    *,
    feature_date: str,
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
                GROUP BY rdate
                """
            )
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
    metadata = connection.execute(
        text(
            f"""
            SELECT COUNT(*) AS row_count,
                   MAX(create_time) AS latest_create_time,
                   MAX(update_time) AS latest_update_time
            FROM {METADATA_SOURCE_TABLE}
            """
        )
    ).mappings().one()
    evidence.append(
        SourceTableEvidence(
            table_name=METADATA_SOURCE_TABLE,
            row_count=int(metadata["row_count"]),
            latest_create_time=_naive_timestamp_text(
                metadata["latest_create_time"]
            ),
            latest_update_time=_naive_timestamp_text(
                metadata["latest_update_time"]
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
    payload = {
        "evidence_version": "native-source-watermark-v1",
        "feature_date": normalized_feature_date,
        "tables": [asdict(item) for item in ordered],
    }
    token = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return SourceCommitEvidence(
        feature_date=normalized_feature_date,
        source_commit_token=token,
        tables=ordered,
    )


def assert_source_commit_evidence_at_cutoff(
    evidence: SourceCommitEvidence,
    *,
    cutoff_at: datetime,
) -> None:
    """拒绝一致性快照中任何已知水位晚于正式 06:30 cutoff。"""
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
            "source snapshot contains writes after the contract cutoff: "
            + ", ".join(sorted(late))
        )


def detect_late_source_writes(
    engine: Engine,
    *,
    feature_date: str,
    cutoff_at: datetime,
) -> tuple[LateSourceWrite, ...]:
    """检测 06:30 后针对冻结 feature cutoff 的可审计源表写入。"""
    if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
        raise ValueError("cutoff_at must be timezone-aware")
    normalized_feature_date = _canonical_date(feature_date)
    localized_cutoff = cutoff_at.astimezone(SHANGHAI)
    source_cutoff = localized_cutoff.replace(tzinfo=None)
    findings: list[LateSourceWrite] = []
    with engine.connect() as connection:
        for table_name in FACTOR_SOURCE_TABLES:
            rows = connection.execute(
                text(
                    f"""
                    SELECT rdate,
                           COUNT(*) AS late_row_count,
                           MAX(create_time) AS latest_write_at
                    FROM {table_name}
                    WHERE create_time > :cutoff_at
                    GROUP BY rdate
                    """
                ),
                {"cutoff_at": source_cutoff},
            ).mappings().all()
            row_count, latest_write = _aggregate_factor_rows(
                rows,
                feature_date=normalized_feature_date,
                count_field="late_row_count",
                timestamp_field="latest_write_at",
            )
            _append_finding(
                findings,
                table_name=table_name,
                row_count=row_count,
                latest_write=latest_write,
                feature_date=normalized_feature_date,
                cutoff_at=localized_cutoff,
            )
        metadata = connection.execute(
            text(
                f"""
                SELECT COUNT(*) AS late_row_count,
                       MAX(
                           CASE
                               WHEN update_time IS NOT NULL
                                AND (
                                    create_time IS NULL
                                    OR update_time > create_time
                                )
                               THEN update_time
                               ELSE create_time
                           END
                       ) AS latest_write_at
                FROM {METADATA_SOURCE_TABLE}
                WHERE (
                    CASE
                        WHEN update_time IS NOT NULL
                         AND (
                             create_time IS NULL
                             OR update_time > create_time
                         )
                        THEN update_time
                        ELSE create_time
                    END
                ) > :cutoff_at
                """
            ),
            {"cutoff_at": source_cutoff},
        ).mappings().one()
        _append_finding(
            findings,
            table_name=METADATA_SOURCE_TABLE,
            row_count=int(metadata["late_row_count"]),
            latest_write=metadata["latest_write_at"],
            feature_date=normalized_feature_date,
            cutoff_at=localized_cutoff,
        )
    return tuple(sorted(findings, key=lambda item: item.table_name))


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


def _append_finding(
    findings: list[LateSourceWrite],
    *,
    table_name: str,
    row_count: int,
    latest_write: object,
    feature_date: str,
    cutoff_at: datetime,
) -> None:
    if row_count <= 0:
        return
    parsed = _source_timestamp(latest_write)
    if parsed is None:
        raise RuntimeError(
            f"late source rows have no latest timestamp: {table_name}"
        )
    findings.append(
        LateSourceWrite(
            table_name=table_name,
            late_row_count=row_count,
            latest_write_at=parsed.isoformat(timespec="seconds"),
            feature_date=feature_date,
            cutoff_at=cutoff_at.isoformat(timespec="seconds"),
        )
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

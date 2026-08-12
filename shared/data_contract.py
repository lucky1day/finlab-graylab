"""Native 源快照水位证据与 06:30 后写入的只读诊断。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from shared.calendar_service import is_trading_day_row

SHANGHAI = ZoneInfo("Asia/Shanghai")
NATIVE_READINESS_DAILY_ANCHORS = (
    "TB1YWI0C",
    "TB3YWI0C",
    "TB5YWI0C",
    "TB7YWI0C",
    "TB0YWI0C",
)
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


@dataclass(frozen=True)
class LateSourceWrite:
    """06:30 后写入当日 cutoff 输入域的一类契约违约。"""

    table_name: str
    late_row_count: int
    latest_write_at: str
    feature_date: str
    cutoff_at: str


@dataclass(frozen=True)
class NativeInputReadiness:
    """Native 日批可冻结输入的最小业务就绪证据。"""

    feature_date: str
    ready: bool
    missing_requirements: tuple[str, ...]


def inspect_native_input_readiness(
    engine: Engine,
    *,
    feature_date: str,
) -> NativeInputReadiness:
    """检查 T-1 锚点、日历和三频历史是否足以开始冻结快照。

    单因子缺值不是阻断条件。这里只要求日频目标锚点、两张日历、factor
    metadata 以及至少一条可用的周/月历史；算法真正消费的全部内容仍由
    随后的 Repeatable Read generation 一次性冻结。
    """
    normalized_feature_date = _canonical_date(feature_date)
    target_parameters = {
        f"target_{index}": target
        for index, target in enumerate(NATIVE_READINESS_DAILY_ANCHORS)
    }
    target_placeholders = ", ".join(
        f":target_{index}"
        for index in range(len(NATIVE_READINESS_DAILY_ANCHORS))
    )
    with engine.connect() as connection:
        trade_flag = connection.execute(
            text(
                """
                SELECT trade_flag
                FROM t_trade_calendar
                WHERE rdate = :feature_date
                LIMIT 1
                """
            ),
            {"feature_date": normalized_feature_date},
        ).scalar()
        wind_week = connection.execute(
            text(
                """
                SELECT week_id
                FROM api_wind_date
                WHERE rdate = :feature_date
                LIMIT 1
                """
            ),
            {"feature_date": normalized_feature_date},
        ).scalar()
        metadata_ready = (
            connection.execute(
                text(
                    f"""
                    SELECT 1
                    FROM {METADATA_SOURCE_TABLE}
                    LIMIT 1
                    """
                )
            ).scalar()
            is not None
        )
        daily_rows = connection.execute(
            text(
                f"""
                SELECT indicators_code
                FROM api_wind_daily
                WHERE rdate = :feature_date
                  AND indicators_code IN ({target_placeholders})
                  AND indicators_value IS NOT NULL
                UNION
                SELECT indicators_code
                FROM api_wind_derivative_daily
                WHERE rdate = :feature_date
                  AND indicators_code IN ({target_placeholders})
                  AND indicators_value IS NOT NULL
                """
            ),
            {
                "feature_date": normalized_feature_date,
                **target_parameters,
            },
        ).scalars().all()
        weekly_ready = _has_period_history(
            connection,
            raw_table="api_wind_weekly",
            derivative_table="api_wind_derivative_weekly",
            feature_date=normalized_feature_date,
        )
        monthly_ready = _has_period_history(
            connection,
            raw_table="api_wind_monthly",
            derivative_table="api_wind_derivative_monthly",
            feature_date=normalized_feature_date,
        )

    missing: list[str] = []
    if wind_week is None or not str(wind_week).strip():
        missing.append("calendar:api_wind_date")
    if not is_trading_day_row(normalized_feature_date, trade_flag):
        missing.append("calendar:t_trade_calendar")
    present_targets = {
        str(value).strip()
        for value in daily_rows
        if str(value or "").strip()
    }
    missing.extend(
        f"daily_target:{target}"
        for target in NATIVE_READINESS_DAILY_ANCHORS
        if target not in present_targets
    )
    if not monthly_ready:
        missing.append("history:monthly")
    if not weekly_ready:
        missing.append("history:weekly")
    if not metadata_ready:
        missing.append("metadata:active_factors")
    ordered = tuple(sorted(missing))
    return NativeInputReadiness(
        feature_date=normalized_feature_date,
        ready=not ordered,
        missing_requirements=ordered,
    )


def _has_period_history(
    connection: Connection,
    *,
    raw_table: str,
    derivative_table: str,
    feature_date: str,
) -> bool:
    """用索引友好的存在性查询检查 raw/derivative 历史。"""
    parameters = {"feature_date": feature_date}
    for table_name in (raw_table, derivative_table):
        row = connection.execute(
            text(
                f"""
                SELECT 1
                FROM {table_name}
                WHERE rdate <= :feature_date
                LIMIT 1
                """
            ),
            parameters,
        ).scalar()
        if row is not None:
            return True
    return False


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

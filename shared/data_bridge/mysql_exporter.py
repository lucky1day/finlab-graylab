"""从本机 MySQL 一致性快照构造 DataBridge 三频 round。"""

from __future__ import annotations

import shutil
import time
from datetime import datetime
from typing import Mapping
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from shared.data_bridge.refresh import (
    DataBridgeRefreshConfig,
    DataBridgeRefreshError,
    DownloadRound,
    LOCAL_MYSQL_PROVENANCE_VERSION,
    LOCAL_MYSQL_SOURCE_MODE,
)
from shared.data_bridge.validation import (
    validate_dataset,
    write_validated_dataset,
)
from shared.data_contract import (
    CALENDAR_SOURCE_TABLES,
    FACTOR_SOURCE_TABLES,
    METADATA_SOURCE_TABLE,
    assert_source_commit_evidence_at_cutoff,
    capture_source_commit_evidence_from_connection,
    source_commit_evidence_payload,
    source_commit_evidence_sha256,
)
from shared.data_service import (
    build_daily_output_from_db,
    build_monthly_output_from_db,
    build_weekly_output_from_db,
)


LOCAL_MYSQL_REQUIRED_TABLES = tuple(
    sorted(
        set(FACTOR_SOURCE_TABLES)
        | {METADATA_SOURCE_TABLE}
        | set(CALENDAR_SOURCE_TABLES)
    )
)


class MySqlDataBridgeRoundBuilder:
    """在每轮单一 MySQL 只读一致性快照中生成全部三频输出。"""

    def __init__(self, *, engine, config: DataBridgeRefreshConfig) -> None:
        self.engine = engine
        self.config = config

    def build(
        self,
        round_id: str,
        *,
        expected_daily_date: str,
        previous_keys: Mapping[str, set[str] | frozenset[str]] | None,
        end_date: str | None = None,
        continuity_cutoffs: Mapping[str, object] | None = None,
    ) -> DownloadRound:
        """生成一次完整候选；所有源读取严格截止于 feature date。"""
        if end_date is not None and end_date != expected_daily_date:
            raise DataBridgeRefreshError(
                "local MySQL DataBridge end_date must equal feature cutoff"
            )
        started = time.monotonic()
        snapshot_started_at: datetime | None = None
        try:
            with self.engine.connect() as connection:
                connection.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
                connection.exec_driver_sql(
                    "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
                )
                try:
                    snapshot_started_at = datetime.now(
                        ZoneInfo("Asia/Shanghai")
                    )
                    _assert_required_source_tables(connection)
                    frames = {
                        "daily_output.csv": build_daily_output_from_db(
                            start_date=self.config.daily_start_date,
                            end_date=expected_daily_date,
                            engine=connection,
                        ),
                        "weekly_output.csv": build_weekly_output_from_db(
                            as_of_date=expected_daily_date,
                            engine=connection,
                        ),
                        "monthly_output.csv": build_monthly_output_from_db(
                            start_date=self.config.daily_start_date,
                            end_date=expected_daily_date,
                            engine=connection,
                            include_databridge_additions=True,
                        ),
                    }
                    evidence = capture_source_commit_evidence_from_connection(
                        connection,
                        feature_date=expected_daily_date,
                    )
                    assert_source_commit_evidence_at_cutoff(
                        evidence,
                        cutoff_at=snapshot_started_at,
                    )
                finally:
                    connection.rollback()
        except DataBridgeRefreshError:
            raise
        except (OSError, SQLAlchemyError):
            raise
        except Exception as exc:
            raise DataBridgeRefreshError(
                "local MySQL DataBridge export failed"
            ) from exc

        dataset = validate_dataset(
            frames,
            schema_path=self.config.schema_path,
            expected_daily_date=expected_daily_date,
            previous_keys=previous_keys,
            continuity_cutoffs=continuity_cutoffs,
        )
        elapsed = time.monotonic() - started
        if elapsed > self.config.round_timeout_sec:
            raise DataBridgeRefreshError(
                "local MySQL DataBridge full round exceeded "
                f"{self.config.round_timeout_sec}s: {elapsed:.1f}s"
            )
        staging_root = self.config.runtime_root / "staging"
        staging_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination = staging_root / round_id
        if destination.exists():
            shutil.rmtree(destination)
        write_validated_dataset(dataset, destination)
        return DownloadRound(
            round_id=round_id,
            directory=destination,
            dataset=dataset,
            digest=dataset.business_digest,
            source_mode=LOCAL_MYSQL_SOURCE_MODE,
            source_provenance=_source_provenance(
                expected_daily_date,
                snapshot_started_at=_require_snapshot_started_at(
                    snapshot_started_at
                ),
                evidence=evidence,
            ),
        )


def _assert_required_source_tables(connection) -> None:
    """逐表探测本地 exporter 及水位证据依赖，缺失即拒绝。"""
    for table_name in LOCAL_MYSQL_REQUIRED_TABLES:
        try:
            connection.exec_driver_sql(
                f"SELECT 1 FROM `{table_name}` LIMIT 1"
            )
        except Exception as exc:
            raise DataBridgeRefreshError(
                "local MySQL DataBridge required source table is unavailable: "
                f"{table_name}"
            ) from exc


def _require_snapshot_started_at(value: datetime | None) -> datetime:
    if value is None:
        raise DataBridgeRefreshError(
            "local MySQL DataBridge snapshot did not start"
        )
    return value


def _source_provenance(
    feature_date: str,
    *,
    snapshot_started_at: datetime,
    evidence,
) -> dict[str, object]:
    token = source_commit_evidence_sha256(evidence)
    if token != evidence.source_commit_token:
        raise DataBridgeRefreshError(
            "local MySQL source evidence token is inconsistent"
        )
    return {
        "provenance_version": LOCAL_MYSQL_PROVENANCE_VERSION,
        "feature_date": feature_date,
        "source_rdate_cutoff": feature_date,
        "snapshot_started_at": snapshot_started_at.isoformat(
            timespec="seconds"
        ),
        "source_commit_token": token,
        "source_evidence_sha256": token,
        "source_evidence": source_commit_evidence_payload(evidence),
    }

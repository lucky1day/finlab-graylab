from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import Iterable, Mapping, Sequence

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from scheduler.persistence.connections import dialect_name
from shared.actual_facts import (
    ActualSourceSnapshot,
    build_daily_actual_records_from_rows,
    normalize_date,
    read_actual_source_snapshot,
)
from shared.models import (
    ActualRecord,
    MonthlyActualRecord,
    PeriodAverageActualRecord,
    WeeklyActualRecord,
)
from shared.tenor_mapping import normalize_tenor


@dataclass(frozen=True)
class ActualWriteStats:
    """一次 Actual 写入相对事务开始时既有事实的业务统计。"""

    attempted: int = 0
    inserted: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0


def actual_comparison_value(column: str, value: object) -> object:
    """规范化数据库驱动差异，避免把等价业务值误报为变更。"""
    if column == "extra":
        if value is None:
            return {}
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            loaded = json.loads(value)
            if isinstance(loaded, Mapping):
                return dict(loaded)
        raise ValueError("Actual extra must be a JSON object")
    if column.endswith("_date"):
        return str(value)[:10]
    if column.endswith("_yield"):
        return float(value)
    return value


def classify_actual_rows_conn(
    conn: Connection,
    *,
    table: str,
    rows: Sequence[Mapping[str, object]],
    key_columns: tuple[str, ...],
    business_columns: tuple[str, ...],
) -> tuple[ActualWriteStats, list[dict[str, object]]]:
    """锁定并比较精确业务键，只返回仍需 INSERT/UPDATE 的行。

    MySQL 先做无锁观测，再只锁定已经存在的精确键。观测时不存在的键使用
    ``INSERT .. ON DUPLICATE KEY`` 原子认领；若并发事务先完成插入，本事务会
    等待唯一键并把该行重新归类为 changed/unchanged。这样统计不依赖服务器的
    默认事务隔离级别，也不会把两个并发调用都报告为 inserted。
    """
    if not rows:
        return ActualWriteStats(), []
    query_keys = [tuple(row[column] for column in key_columns) for row in rows]
    keys = [
        tuple(
            actual_comparison_value(column, value)
            for column, value in zip(key_columns, key)
        )
        for key in query_keys
    ]
    if len(set(keys)) != len(keys):
        raise ValueError(f"duplicate Actual business key in one batch: table={table}")

    selected_columns = (*key_columns, *business_columns)

    def select_rows(
        selected_keys: Sequence[tuple[object, ...]],
        *,
        lock: bool,
    ) -> dict[tuple[object, ...], Mapping[str, object]]:
        selected: dict[tuple[object, ...], Mapping[str, object]] = {}
        chunk_size = 400
        lock_clause = " FOR UPDATE" if lock else ""
        for offset in range(0, len(selected_keys), chunk_size):
            chunk = selected_keys[offset : offset + chunk_size]
            predicates: list[str] = []
            params: dict[str, object] = {}
            for row_index, key in enumerate(chunk):
                parts = []
                for column_index, (column, value) in enumerate(
                    zip(key_columns, key)
                ):
                    name = f"key_{row_index}_{column_index}"
                    parts.append(f"{column} = :{name}")
                    params[name] = value
                predicates.append("(" + " AND ".join(parts) + ")")
            statement = text(
                f"SELECT {', '.join(selected_columns)} FROM {table} "
                f"WHERE {' OR '.join(predicates)}{lock_clause}"
            )
            for stored in conn.execute(statement, params).mappings().all():
                key = tuple(
                    actual_comparison_value(column, stored[column])
                    for column in key_columns
                )
                selected[key] = stored
        return selected

    if dialect_name(conn) == "sqlite":
        existing = select_rows(query_keys, lock=False)
        inserted_keys: set[tuple[object, ...]] = set()
    else:
        observed = select_rows(query_keys, lock=False)
        observed_query_keys = [
            query_key
            for query_key, key in zip(query_keys, keys)
            if key in observed
        ]
        # 精确唯一键锁按稳定顺序获取，避免同一批键仅因输入顺序不同
        # 而反转。
        existing = select_rows(sorted(observed_query_keys), lock=True)
        inserted_keys = set()

        insert_columns = (*key_columns, *business_columns)
        insert_values = [
            "CAST(:extra AS JSON)" if column == "extra" else f":{column}"
            for column in insert_columns
        ]
        claim_statement = text(
            f"INSERT INTO {table} ({', '.join(insert_columns)}) "
            f"VALUES ({', '.join(insert_values)}) "
            "ON DUPLICATE KEY UPDATE id = id + LAST_INSERT_ID(0)"
        )
        missing_rows = sorted(
            (
                (key, raw)
                for raw, key in zip(rows, keys)
                if key not in existing
            ),
            key=lambda item: item[0],
        )
        duplicate_query_keys: list[tuple[object, ...]] = []
        for key, raw in missing_rows:
            conn.execute(claim_statement, dict(raw))
            claimed_id = int(
                conn.execute(text("SELECT LAST_INSERT_ID()")).scalar_one()
            )
            if claimed_id > 0:
                inserted_keys.add(key)
            else:
                duplicate_query_keys.append(
                    tuple(raw[column] for column in key_columns)
                )
        if duplicate_query_keys:
            # 冲突 INSERT 已持有目标记录锁；锁定读取取得等待后的
            # 最新提交值。
            existing.update(select_rows(duplicate_query_keys, lock=True))

    inserted = 0
    changed = 0
    unchanged = 0
    write_rows: list[dict[str, object]] = []
    for raw, key in zip(rows, keys):
        row = dict(raw)
        if key in inserted_keys:
            inserted += 1
            continue
        stored = existing.get(key)
        if stored is None:
            # SQLite 没有并发认领分支，仍由原有 UPSERT 完成首次插入。
            inserted += 1
            write_rows.append(row)
            continue
        differs = any(
            actual_comparison_value(column, row[column])
            != actual_comparison_value(column, stored[column])
            for column in business_columns
        )
        if differs:
            changed += 1
            write_rows.append(row)
        else:
            unchanged += 1
    return (
        ActualWriteStats(
            attempted=len(rows),
            inserted=inserted,
            changed=changed,
            unchanged=unchanged,
        ),
        write_rows,
    )


def upsert_actuals(engine: Engine, records: Iterable[ActualRecord]) -> int:
    """兼容入口：UPSERT 日频 Actual，并返回本批处理条数。"""
    rows = [asdict(record) for record in records]
    if not rows:
        return 0
    with engine.begin() as conn:
        return _upsert_actuals_conn(conn, rows)


def upsert_actuals_detailed(
    engine: Engine,
    records: Iterable[ActualRecord],
) -> ActualWriteStats:
    """UPSERT 日频 Actual，并返回真实新增、变化与未变化统计。"""
    rows = [asdict(record) for record in records]
    if not rows:
        return ActualWriteStats()
    with engine.begin() as conn:
        return _upsert_actuals_detailed_conn(conn, rows)


def _upsert_actuals_conn(
    conn: Connection,
    records: Iterable[ActualRecord | Mapping[str, object]],
) -> int:
    """兼容入口：在调用方事务中 UPSERT，并返回本批处理条数。"""
    rows = [
        asdict(record) if isinstance(record, ActualRecord) else dict(record)
        for record in records
    ]
    return _upsert_actuals_detailed_conn(conn, rows).attempted


def _upsert_actuals_detailed_conn(
    conn: Connection,
    records: Iterable[ActualRecord | Mapping[str, object]],
) -> ActualWriteStats:
    """在调用方事务中仅写入新增或业务值发生变化的日频 Actual。"""
    rows = [
        asdict(record) if isinstance(record, ActualRecord) else dict(record)
        for record in records
    ]
    if not rows:
        return ActualWriteStats()
    stats, write_rows = classify_actual_rows_conn(
        conn,
        table="t_scheme_actuals",
        rows=rows,
        key_columns=("tenor", "trade_date"),
        business_columns=("close_yield", "direction_1d", "direction_5d"),
    )
    if not write_rows:
        return stats
    duplicate = (
        """
        ON CONFLICT (tenor, trade_date) DO UPDATE SET
            close_yield = excluded.close_yield,
            direction_1d = excluded.direction_1d,
            direction_5d = excluded.direction_5d,
            updated_at = CURRENT_TIMESTAMP
        """
        if dialect_name(conn) == "sqlite"
        else """
        ON DUPLICATE KEY UPDATE
            close_yield = VALUES(close_yield),
            direction_1d = VALUES(direction_1d),
            direction_5d = VALUES(direction_5d),
            updated_at = CURRENT_TIMESTAMP
        """
    )
    sql = text(
        f"""
        INSERT INTO t_scheme_actuals
            (tenor, trade_date, close_yield, direction_1d, direction_5d)
        VALUES
            (:tenor, :trade_date, :close_yield, :direction_1d, :direction_5d)
        {duplicate}
        """
    )
    conn.execute(sql, write_rows)
    return stats


@dataclass(frozen=True)
class ActualTailRepairPlan:
    """经只读预览生成的日频 Actual 尾部修复计划。"""

    tenors: tuple[str, ...]
    source_digest: str
    source_watermarks: tuple[tuple[str, str], ...]
    start_date: str | None
    end_date: str | None
    business_keys: tuple[tuple[str, str], ...]


def _actual_keys_after_source_watermark_conn(
    conn: Connection,
    source_watermarks: Mapping[str, str],
    end_date: str | None = None,
) -> tuple[tuple[str, str], ...]:
    rows = [
        {
            "tenor": str(tenor),
            "source_max_date": str(source_max_date),
            "end_date": end_date,
        }
        for tenor, source_max_date in source_watermarks.items()
        if source_max_date
    ]
    if not rows:
        return ()
    end_filter = "AND trade_date <= :end_date" if end_date else ""
    sql = text(
        f"""
        SELECT tenor, trade_date
        FROM t_scheme_actuals
        WHERE tenor = :tenor
          AND trade_date > :source_max_date
          {end_filter}
        ORDER BY tenor, trade_date
        """
    )
    keys: list[tuple[str, str]] = []
    for row in rows:
        keys.extend(
            (str(item["tenor"]), str(item["trade_date"])[:10])
            for item in conn.execute(sql, row).mappings().all()
        )
    return tuple(sorted(keys))


def plan_actuals_tail_repair(
    engine: Engine,
    *,
    tenors: Iterable[str],
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> ActualTailRepairPlan:
    """只读生成绑定源快照身份与精确业务键的尾部修复计划。"""
    normalized_tenors = tuple(sorted({normalize_tenor(tenor) for tenor in tenors}))
    if not normalized_tenors:
        raise ValueError("daily actual tail repair tenor scope cannot be empty")
    normalized_start = normalize_date(start_date)
    normalized_end = normalize_date(end_date)
    with engine.connect() as conn:
        snapshot = read_actual_source_snapshot(
            conn,
            tenors=normalized_tenors,
            end_date=normalized_end,
        )
        normalized_watermarks = tuple(sorted(snapshot.watermarks.items()))
        business_keys = _actual_keys_after_source_watermark_conn(
            conn,
            dict(normalized_watermarks),
            normalized_end,
        )
    return ActualTailRepairPlan(
        tenors=normalized_tenors,
        source_digest=snapshot.source_digest,
        source_watermarks=normalized_watermarks,
        start_date=normalized_start,
        end_date=normalized_end,
        business_keys=business_keys,
    )


def _delete_actual_keys_conn(
    conn: Connection,
    business_keys: Iterable[tuple[str, str]],
) -> int:
    rows = [
        {"tenor": str(tenor), "trade_date": str(trade_date)}
        for tenor, trade_date in business_keys
    ]
    if not rows:
        return 0
    result = conn.execute(
        text(
            """
            DELETE FROM t_scheme_actuals
            WHERE tenor = :tenor AND trade_date = :trade_date
            """
        ),
        rows,
    )
    return int(result.rowcount or 0)


def _require_current_actual_tail_plan(
    conn: Connection,
    plan: ActualTailRepairPlan,
) -> ActualSourceSnapshot:
    snapshot = read_actual_source_snapshot(
        conn,
        tenors=plan.tenors,
        end_date=plan.end_date,
    )
    if snapshot.source_digest != plan.source_digest:
        raise RuntimeError("daily actual tail repair source snapshot changed")
    current_watermarks = tuple(sorted(snapshot.watermarks.items()))
    if current_watermarks != plan.source_watermarks:
        raise RuntimeError("daily actual tail repair source watermarks changed")
    current_keys = _actual_keys_after_source_watermark_conn(
        conn,
        dict(plan.source_watermarks),
        plan.end_date,
    )
    if current_keys != plan.business_keys:
        raise RuntimeError(
            "daily actual tail repair plan is stale: "
            f"expected={plan.business_keys!r}, current={current_keys!r}"
        )
    return snapshot


def delete_actuals_after_source_watermark(
    engine: Engine,
    plan: ActualTailRepairPlan,
) -> int:
    """按已预览且仍匹配的精确业务键执行独立尾部删除。"""
    with engine.begin() as conn:
        _require_current_actual_tail_plan(conn, plan)
        deleted = _delete_actual_keys_conn(conn, plan.business_keys)
        if deleted != len(plan.business_keys):
            raise RuntimeError(
                "daily actual tail repair delete count changed: "
                f"expected={len(plan.business_keys)}, actual={deleted}"
            )
        return deleted


def repair_actuals_after_source_watermark(
    engine: Engine,
    plan: ActualTailRepairPlan,
) -> tuple[int, int]:
    """兼容入口：执行日频 Actual 修复并返回处理数与删除数。"""
    stats = repair_actuals_after_source_watermark_detailed(engine, plan)
    return stats.attempted, stats.deleted


def repair_actuals_after_source_watermark_detailed(
    engine: Engine,
    plan: ActualTailRepairPlan,
) -> ActualWriteStats:
    """原子执行日频 Actual 修复，并返回包含精确删除数的统计。"""
    with engine.begin() as conn:
        snapshot = _require_current_actual_tail_plan(conn, plan)
        records = build_daily_actual_records_from_rows(
            snapshot.rows,
            start_date=plan.start_date,
        )
        stats = _upsert_actuals_detailed_conn(conn, records)
        deleted = _delete_actual_keys_conn(conn, plan.business_keys)
        if deleted != len(plan.business_keys):
            raise RuntimeError(
                "daily actual tail repair delete count changed: "
                f"expected={len(plan.business_keys)}, actual={deleted}"
            )
    return replace(stats, deleted=deleted)


def upsert_weekly_actuals(engine: Engine, records: Iterable[WeeklyActualRecord]) -> int:
    """兼容入口：UPSERT 周度 Actual，并返回本批处理条数。"""
    return upsert_weekly_actuals_detailed(engine, records).attempted


def upsert_weekly_actuals_detailed(
    engine: Engine,
    records: Iterable[WeeklyActualRecord],
) -> ActualWriteStats:
    """UPSERT 周度 Actual，并返回真实新增、变化与未变化统计。"""
    rows = []
    for record in records:
        row = asdict(record)
        row["extra"] = json.dumps(record.extra or {}, ensure_ascii=False)
        rows.append(row)
    if not rows:
        return ActualWriteStats()
    sqlite = engine.dialect.name == "sqlite"
    json_value = ":extra" if sqlite else "CAST(:extra AS JSON)"
    duplicate = """
        ON CONFLICT (tenor, predict_date, target_rule) DO UPDATE SET
            feature_week_id = excluded.feature_week_id,
            target_week_id = excluded.target_week_id,
            feature_date = excluded.feature_date,
            target_date = excluded.target_date,
            feature_yield = excluded.feature_yield,
            target_yield = excluded.target_yield,
            direction_weekly = excluded.direction_weekly,
            price_signal = excluded.price_signal,
            extra = excluded.extra,
            updated_at = CURRENT_TIMESTAMP
    """ if sqlite else """
        ON DUPLICATE KEY UPDATE
            feature_week_id = VALUES(feature_week_id),
            target_week_id = VALUES(target_week_id),
            feature_date = VALUES(feature_date),
            target_date = VALUES(target_date),
            feature_yield = VALUES(feature_yield),
            target_yield = VALUES(target_yield),
            direction_weekly = VALUES(direction_weekly),
            price_signal = VALUES(price_signal),
            extra = VALUES(extra),
            updated_at = CURRENT_TIMESTAMP
    """
    sql = text(
        f"""
        INSERT INTO t_scheme_weekly_actuals
            (tenor, feature_week_id, target_week_id, predict_date, feature_date, target_date,
             feature_yield, target_yield, direction_weekly, price_signal, target_rule, extra)
        VALUES
            (:tenor, :feature_week_id, :target_week_id, :predict_date, :feature_date, :target_date,
             :feature_yield, :target_yield, :direction_weekly, :price_signal, :target_rule, {json_value})
        {duplicate}
        """
    )
    with engine.begin() as conn:
        _assert_weekly_actuals_target_rule_unique_key(conn)
        stats, write_rows = classify_actual_rows_conn(
            conn,
            table="t_scheme_weekly_actuals",
            rows=rows,
            key_columns=("tenor", "predict_date", "target_rule"),
            business_columns=(
                "feature_week_id", "target_week_id", "feature_date", "target_date",
                "feature_yield", "target_yield", "direction_weekly", "price_signal", "extra",
            ),
        )
        if write_rows:
            conn.execute(sql, write_rows)
    return stats


def upsert_monthly_actuals(engine: Engine, records: Iterable[MonthlyActualRecord]) -> int:
    """兼容入口：UPSERT 月度 Actual，并返回本批处理条数。"""
    return upsert_monthly_actuals_detailed(engine, records).attempted


def upsert_monthly_actuals_detailed(
    engine: Engine,
    records: Iterable[MonthlyActualRecord],
) -> ActualWriteStats:
    """UPSERT 月度 Actual，并返回真实新增、变化与未变化统计。"""
    rows = []
    for record in records:
        row = asdict(record)
        row["extra"] = json.dumps(record.extra or {}, ensure_ascii=False)
        rows.append(row)
    if not rows:
        return ActualWriteStats()

    if engine.dialect.name == "sqlite":
        sql = text(
            """
            INSERT INTO t_scheme_monthly_actuals
                (tenor, feature_month_id, target_month_id, predict_date, feature_date, target_date,
                 feature_yield, target_yield, direction_monthly, price_signal, target_rule, extra)
            VALUES
                (:tenor, :feature_month_id, :target_month_id, :predict_date, :feature_date, :target_date,
                 :feature_yield, :target_yield, :direction_monthly, :price_signal, :target_rule, :extra)
            ON CONFLICT (tenor, predict_date, target_rule) DO UPDATE SET
                feature_month_id = excluded.feature_month_id,
                target_month_id = excluded.target_month_id,
                feature_date = excluded.feature_date,
                target_date = excluded.target_date,
                feature_yield = excluded.feature_yield,
                target_yield = excluded.target_yield,
                direction_monthly = excluded.direction_monthly,
                price_signal = excluded.price_signal,
                target_rule = excluded.target_rule,
                extra = excluded.extra,
                updated_at = CURRENT_TIMESTAMP
            """
        )
    else:
        sql = text(
            """
            INSERT INTO t_scheme_monthly_actuals
                (tenor, feature_month_id, target_month_id, predict_date, feature_date, target_date,
                 feature_yield, target_yield, direction_monthly, price_signal, target_rule, extra)
            VALUES
                (:tenor, :feature_month_id, :target_month_id, :predict_date, :feature_date, :target_date,
                 :feature_yield, :target_yield, :direction_monthly, :price_signal, :target_rule, CAST(:extra AS JSON))
            ON DUPLICATE KEY UPDATE
                feature_month_id = VALUES(feature_month_id),
                target_month_id = VALUES(target_month_id),
                feature_date = VALUES(feature_date),
                target_date = VALUES(target_date),
                feature_yield = VALUES(feature_yield),
                target_yield = VALUES(target_yield),
                direction_monthly = VALUES(direction_monthly),
                price_signal = VALUES(price_signal),
                target_rule = VALUES(target_rule),
                extra = VALUES(extra),
                updated_at = CURRENT_TIMESTAMP
            """
        )
    with engine.begin() as conn:
        stats, write_rows = classify_actual_rows_conn(
            conn,
            table="t_scheme_monthly_actuals",
            rows=rows,
            key_columns=("tenor", "predict_date", "target_rule"),
            business_columns=(
                "feature_month_id", "target_month_id", "feature_date", "target_date",
                "feature_yield", "target_yield", "direction_monthly", "price_signal", "extra",
            ),
        )
        if write_rows:
            conn.execute(sql, write_rows)
    return stats


def upsert_period_average_actuals(
    engine: Engine,
    records: Iterable[PeriodAverageActualRecord],
) -> int:
    """兼容入口：UPSERT 周期均值 Actual，并返回本批处理条数。"""
    return upsert_period_average_actuals_detailed(engine, records).attempted


def upsert_period_average_actuals_detailed(
    engine: Engine,
    records: Iterable[PeriodAverageActualRecord],
) -> ActualWriteStats:
    """UPSERT 周期均值 Actual，并返回真实新增、变化与未变化统计。"""
    rows = []
    for record in records:
        row = asdict(record)
        row["extra"] = json.dumps(record.extra or {}, ensure_ascii=False)
        rows.append(row)
    if not rows:
        return ActualWriteStats()

    json_value = ":extra" if engine.dialect.name == "sqlite" else "CAST(:extra AS JSON)"
    duplicate = """
        ON CONFLICT (tenor, predict_date, target_rule) DO UPDATE SET
            feature_date = excluded.feature_date,
            target_date = excluded.target_date,
            feature_yield = excluded.feature_yield,
            target_yield = excluded.target_yield,
            actual_direction = excluded.actual_direction,
            price_signal = excluded.price_signal,
            extra = excluded.extra,
            updated_at = CURRENT_TIMESTAMP
    """ if engine.dialect.name == "sqlite" else """
        ON DUPLICATE KEY UPDATE
            feature_date = VALUES(feature_date),
            target_date = VALUES(target_date),
            feature_yield = VALUES(feature_yield),
            target_yield = VALUES(target_yield),
            actual_direction = VALUES(actual_direction),
            price_signal = VALUES(price_signal),
            extra = VALUES(extra),
            updated_at = CURRENT_TIMESTAMP
    """
    sql = text(
        f"""
        INSERT INTO t_scheme_period_average_actuals
            (tenor, predict_date, feature_date, target_date,
             feature_yield, target_yield, actual_direction,
             price_signal, target_rule, extra)
        VALUES
            (:tenor, :predict_date, :feature_date, :target_date,
             :feature_yield, :target_yield, :actual_direction,
             :price_signal, :target_rule, {json_value})
        {duplicate}
        """
    )
    with engine.begin() as conn:
        stats, write_rows = classify_actual_rows_conn(
            conn,
            table="t_scheme_period_average_actuals",
            rows=rows,
            key_columns=("tenor", "predict_date", "target_rule"),
            business_columns=(
                "feature_date", "target_date", "feature_yield", "target_yield",
                "actual_direction", "price_signal", "extra",
            ),
        )
        if write_rows:
            conn.execute(sql, write_rows)
    return stats


def _assert_weekly_actuals_target_rule_unique_key(conn: Connection) -> None:
    """确认周度 actual 唯一键包含 target_rule，避免 point/average 互相覆盖。"""
    expected = ("tenor", "predict_date", "target_rule")
    legacy = ("tenor", "predict_date")
    indexes = inspect(conn).get_indexes("t_scheme_weekly_actuals")
    unique_columns = [
        tuple(index.get("column_names") or ())
        for index in indexes
        if bool(index.get("unique"))
    ]
    if expected not in unique_columns:
        raise RuntimeError(
            "t_scheme_weekly_actuals missing unique key "
            "uk_weekly_actual_predict_rule(tenor,predict_date,target_rule); "
            "run migrations/014_weekly_average_actuals.sql before weekly actual writes"
        )
    if legacy in unique_columns:
        raise RuntimeError(
            "t_scheme_weekly_actuals still has legacy unique key on (tenor,predict_date); "
            "run migrations/014_weekly_average_actuals.sql before weekly actual writes"
        )

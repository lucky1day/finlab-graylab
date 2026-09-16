from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from backend.db import current_http_request_context
from backend.factor_lab_dashboard_semantics import (
    BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE,
    FACTOR_LAB_HISTORY_START_DATE,
    DashboardDataError,
    live_actual_selector,
)
from shared.legacy_prediction_migration import (
    load_legacy_corrected_exact_evidence,
    load_legacy_prediction_migrations,
)
from shared.prediction_history_replacement import (
    PredictionHistoryProjection,
    PredictionHistoryReplacementError,
    resolve_prediction_history_projection,
    validate_prediction_history_source_runs,
)


logger = logging.getLogger(__name__)
MAX_REGISTRY_SOURCE_ROWS = 1_000
MAX_TARGET_SOURCE_ROWS = 1_000
MAX_PRODUCT_PREDICTION_SOURCE_ROWS = 100_000
MAX_ACTUAL_SOURCE_ROWS = 80_000
SUMMARY_PREDICTION_FETCH_ROWS = 2_000
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_ACTUAL_SOURCE_KIND_ORDER = (
    "daily_1d",
    "daily_5d",
    "weekly",
    "monthly",
    "period_average",
)
_ACTUAL_SOURCE_FRAGMENTS = {
    "daily_1d": """
        SELECT 'daily_1d' AS actual_kind,
               tenor AS target_tenor,
               trade_date AS target_date,
               'target_date_yield_vs_feature_date_yield' AS target_rule,
               direction_1d AS actual_direction
        FROM t_scheme_actuals
        WHERE tenor IN :daily_1d_tenors
          AND trade_date >= :history_start_date
    """,
    "daily_5d": """
        SELECT 'daily_5d' AS actual_kind,
               tenor AS target_tenor,
               trade_date AS target_date,
               'target_date_yield_vs_feature_date_yield' AS target_rule,
               direction_5d AS actual_direction
        FROM t_scheme_actuals
        WHERE tenor IN :daily_5d_tenors
          AND trade_date >= :history_start_date
    """,
    "weekly": """
        SELECT 'weekly' AS actual_kind,
               tenor AS target_tenor,
               target_date,
               target_rule,
               direction_weekly AS actual_direction
        FROM t_scheme_weekly_actuals
        WHERE (tenor, target_rule) IN :weekly_scopes
          AND target_date >= :history_start_date
    """,
    "monthly": """
        SELECT 'monthly' AS actual_kind,
               tenor AS target_tenor,
               target_date,
               target_rule,
               direction_monthly AS actual_direction
        FROM t_scheme_monthly_actuals
        WHERE (tenor, target_rule) IN :monthly_scopes
          AND target_date >= :history_start_date
    """,
    "period_average": """
        SELECT 'period_average' AS actual_kind,
               tenor AS target_tenor,
               target_date,
               target_rule,
               actual_direction
        FROM t_scheme_period_average_actuals
        WHERE (tenor, target_rule) IN :period_average_scopes
          AND target_date >= :history_start_date
    """,
}
_ACTUAL_SOURCE_SCOPE_PARAMS = {
    "daily_1d": "daily_1d_tenors",
    "daily_5d": "daily_5d_tenors",
    "weekly": "weekly_scopes",
    "monthly": "monthly_scopes",
    "period_average": "period_average_scopes",
}
_ACTUAL_SOURCE_DATE_FILTERS = {
    "daily_1d": (
        " AND trade_date >= :target_date_from"
        " AND trade_date < :target_date_before"
    ),
    "daily_5d": (
        " AND trade_date >= :target_date_from"
        " AND trade_date < :target_date_before"
    ),
    "weekly": (
        " AND target_date >= :target_date_from"
        " AND target_date < :target_date_before"
    ),
    "monthly": (
        " AND target_date >= :target_date_from"
        " AND target_date < :target_date_before"
    ),
    "period_average": (
        " AND target_date >= :target_date_from"
        " AND target_date < :target_date_before"
    ),
}
_ACTUAL_SOURCE_TARGET_DATE_COLUMNS = {
    "daily_1d": "trade_date",
    "daily_5d": "trade_date",
    "weekly": "target_date",
    "monthly": "target_date",
    "period_average": "target_date",
}


@dataclass(slots=True)
class SummaryPredictionReadStats:
    """Summary 流式预测读取的有界诊断统计。"""

    source_rows: int = 0
    fetch_seconds: float = 0.0
    cleanup_seconds: float = 0.0
    exit_reason: str = "not_started"


@dataclass(frozen=True, slots=True)
class DashboardHistoryReplacementPlan:
    """当前数据库中已完整验证、可用于 Dashboard 的历史替代投影。"""

    projections: tuple[PredictionHistoryProjection, ...] = ()

    @property
    def excluded_product_ids(self) -> frozenset[int]:
        return frozenset(
            row_id
            for projection in self.projections
            for row_id in projection.excluded_product_ids
        )

    @property
    def source_rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            row
            for projection in self.projections
            for row in projection.source_rows
        )


def resolve_dashboard_history_replacements(
    connection: Connection,
    registry_rows: list[Mapping[str, Any]],
) -> DashboardHistoryReplacementPlan:
    """仅在 archived/retired 来源和完整事实摘要均命中时启用替代。"""
    active_scopes = {
        (
            str(row["base_scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
        for row in registry_rows
    }
    entries = load_legacy_prediction_migrations(PROJECT_ROOT)
    corrected_by_identity = {
        (item.source_scheme_id, item.designated_exact): item
        for item in load_legacy_corrected_exact_evidence(PROJECT_ROOT)
    }
    projections: list[PredictionHistoryProjection] = []
    for entry in sorted(
        entries,
        key=lambda item: (
            item.prediction_scheme_id,
            item.target_tenor,
            item.horizon,
        ),
    ):
        if (
            entry.prediction_scheme_id,
            entry.target_tenor,
            entry.horizon,
        ) not in active_scopes:
            continue
        corrected = corrected_by_identity.get(
            (entry.designated_source_scheme_id, entry.designated_exact)
        )
        if corrected is None:
            raise DashboardDataError(
                "historical replacement corrected evidence is missing"
            )
        source_identity = connection.execute(
            text(
                "SELECT r.base_scheme_id, r.status AS registry_status, "
                "v.runtime_type, v.status AS version_status, v.code_hash, "
                "v.config_hash, v.manifest_hash "
                "FROM t_scheme_registry r "
                "JOIN t_scheme_versions v "
                "ON v.scheme_id = r.base_scheme_id "
                "AND v.scheme_version = :source_exact "
                "WHERE r.scheme_id = :source_registry_scheme_id"
            ),
            {
                "source_registry_scheme_id": corrected.source_registry_scheme_id,
                "source_exact": entry.designated_exact,
            },
        ).mappings().one_or_none()
        if source_identity is None:
            orphan_count = connection.execute(
                text(
                    "SELECT "
                    "(SELECT COUNT(*) FROM t_scheme_versions "
                    " WHERE scheme_id = :source_scheme_id "
                    " AND scheme_version = :source_exact) + "
                    "(SELECT COUNT(*) FROM t_scheme_predictions "
                    " WHERE scheme_id = :source_scheme_id "
                    " AND scheme_version = :source_exact)"
                ),
                {
                    "source_scheme_id": entry.designated_source_scheme_id,
                    "source_exact": entry.designated_exact,
                },
            ).scalar_one()
            if int(orphan_count or 0):
                raise DashboardDataError(
                    "historical replacement source identity is incomplete"
                )
            continue
        if dict(source_identity) != {
            "base_scheme_id": entry.designated_source_scheme_id,
            "registry_status": "archived",
            "runtime_type": "blackbox_v2",
            "version_status": "retired",
            "code_hash": entry.designated_code_hash,
            "config_hash": entry.designated_config_hash,
            "manifest_hash": entry.designated_manifest_hash,
        }:
            raise DashboardDataError(
                "historical replacement source identity changed"
            )
        product_rows = _read_replacement_scope_rows(
            connection,
            scheme_id=entry.prediction_scheme_id,
            target_tenor=entry.target_tenor,
            horizon=entry.horizon,
            scheme_version=None,
        )
        source_rows = _read_replacement_scope_rows(
            connection,
            scheme_id=entry.designated_source_scheme_id,
            target_tenor=entry.target_tenor,
            horizon=entry.horizon,
            scheme_version=entry.designated_exact,
        )
        try:
            projection = resolve_prediction_history_projection(
                product_rows,
                source_rows,
                entry=entry,
                corrected=corrected,
            )
        except PredictionHistoryReplacementError as exc:
            raise DashboardDataError(
                f"historical replacement evidence drift: {exc}"
            ) from exc
        if projection is not None:
            (
                source_live_runs,
                source_backtest_runs,
                source_backtest_predictions,
            ) = _read_replacement_source_runs(connection, projection)
            try:
                validate_prediction_history_source_runs(
                    projection,
                    source_live_runs,
                    source_backtest_runs,
                    source_backtest_predictions,
                    entry=entry,
                    corrected=corrected,
                )
            except PredictionHistoryReplacementError as exc:
                raise DashboardDataError(
                    f"historical replacement run lineage drift: {exc}"
                ) from exc
            projections.append(projection)
    return DashboardHistoryReplacementPlan(tuple(projections))


def _read_replacement_scope_rows(
    connection: Connection,
    *,
    scheme_id: str,
    target_tenor: str,
    horizon: int,
    scheme_version: str | None,
) -> list[Mapping[str, Any]]:
    version_filter = (
        " AND scheme_version = :scheme_version"
        if scheme_version is not None
        else ""
    )
    params: dict[str, Any] = {
        "scheme_id": scheme_id,
        "target_tenor": target_tenor,
        "horizon": horizon,
    }
    if scheme_version is not None:
        params["scheme_version"] = scheme_version
    return list(
        connection.execute(
            text(
                "SELECT id, run_id, backtest_run_id, scheme_version, scheme_id, "
                "target_tenor, horizon, predict_date, feature_date, target_date, "
                "predicted_direction, backtest_actual_direction "
                "FROM t_scheme_predictions "
                "WHERE scheme_id = :scheme_id "
                "AND target_tenor = :target_tenor AND horizon = :horizon"
                + version_filter
                + " ORDER BY target_date, predict_date, id"
            ),
            params,
        ).mappings()
    )


def _read_replacement_source_runs(
    connection: Connection,
    projection: PredictionHistoryProjection,
) -> tuple[
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
]:
    """读取替代事实精确引用的 live/backtest run。"""
    live_runs: list[Mapping[str, Any]] = []
    if projection.source_live_run_ids:
        statement = text(
            "SELECT run_id, scheme_id, scheme_version, runtime_type, status, "
            "prediction_phase, predict_date, data_snapshot_id, records_written "
            "FROM t_scheme_runs WHERE run_id IN :run_ids ORDER BY run_id"
        ).bindparams(bindparam("run_ids", expanding=True))
        live_runs = list(
            connection.execute(
                statement,
                {"run_ids": sorted(projection.source_live_run_ids)},
            ).mappings()
        )
    backtest_runs: list[Mapping[str, Any]] = []
    if projection.source_backtest_run_ids:
        statement = text(
            "SELECT id, benchmark_id, scheme_id, data_source, status, "
            "code_hash, config_hash, input_artifact_hash, run_mode, summary "
            "FROM t_backtest_runs "
            "WHERE id IN :run_ids ORDER BY id"
        ).bindparams(bindparam("run_ids", expanding=True))
        backtest_runs = list(
            connection.execute(
                statement,
                {"run_ids": sorted(projection.source_backtest_run_ids)},
            ).mappings()
        )
    backtest_predictions: list[Mapping[str, Any]] = []
    if projection.source_backtest_run_ids:
        statement = text(
            "SELECT run_id, scheme_id, target_tenor, horizon, predict_date, "
            "feature_date, target_date, predicted_direction, label "
            "FROM t_backtest_predictions WHERE run_id IN :run_ids "
            "ORDER BY target_tenor, horizon, target_date, predict_date, id"
        ).bindparams(bindparam("run_ids", expanding=True))
        backtest_predictions = list(
            connection.execute(
                statement,
                {"run_ids": sorted(projection.source_backtest_run_ids)},
            ).mappings()
        )
    return live_runs, backtest_runs, backtest_predictions


def read_bounded_source_rows(
    connection: Connection,
    statement: Any,
    params: Mapping[str, Any],
    *,
    dataset: str,
    cap: int,
) -> list[Mapping[str, Any]]:
    """读取至多 ``cap`` 行；超限时失败而不是截断事实。"""
    execution_params = dict(params)
    execution_params["dashboard_source_limit"] = cap + 1
    request_context = current_http_request_context()
    if request_context is not None:
        request_context.failure_stage = "dashboard_query"
        request_context.require_remaining("dashboard_query")
        db_started_at = request_context.clock()
    else:
        db_started_at = None
    try:
        rows = list(
            connection.execute(statement, execution_params).mappings().all()
        )
    finally:
        if request_context is not None and db_started_at is not None:
            request_context.db_seconds += max(
                0.0,
                request_context.clock() - db_started_at,
            )
    if len(rows) > cap:
        raise DashboardDataError(
            "dashboard source row limit exceeded: "
            f"dataset={dataset} limit={cap}"
        )
    return rows


def read_active_registry(
    connection: Connection,
    *,
    registry_scheme_id: str | None = None,
    cap: int = MAX_REGISTRY_SOURCE_ROWS,
) -> list[Mapping[str, Any]]:
    """读取 active Registry 目标；可收敛到一个 composite scheme。"""
    scheme_filter = (
        " AND scheme_id = :registry_scheme_id"
        if registry_scheme_id is not None
        else ""
    )
    statement = text(
        f"""
        SELECT scheme_id, base_scheme_id, runtime_type, name, owner, description,
               horizon, task_type, frequency, target_tenor, status, deployed_at
        FROM t_scheme_registry
        WHERE status = :active_status{scheme_filter}
        ORDER BY target_tenor, task_type, scheme_id
        LIMIT :dashboard_source_limit
        """
    )
    params = {"active_status": "active"}
    if registry_scheme_id is not None:
        params["registry_scheme_id"] = registry_scheme_id
    return read_bounded_source_rows(
        connection,
        statement,
        params,
        dataset="active_registry",
        cap=cap,
    )


def read_selected_backtest_runs(
    connection: Connection,
    registry_rows: list[Mapping[str, Any]],
    *,
    cap: int = MAX_REGISTRY_SOURCE_ROWS,
) -> list[Mapping[str, Any]]:
    """在数据库侧为每个 base scheme 选择唯一最新成功回测。"""
    base_scheme_ids = sorted(
        {str(row["base_scheme_id"]) for row in registry_rows}
    )
    if not base_scheme_ids:
        return []
    data_sources = sorted(set(BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE.values()))
    statement = text(
        """
        SELECT id, benchmark_id, scheme_id, data_source, start_date, end_date,
               status, created_at, updated_at
        FROM (
            SELECT id, benchmark_id, scheme_id, data_source, start_date,
                   end_date, status, created_at, updated_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY scheme_id
                       ORDER BY updated_at DESC, id DESC
                   ) AS dashboard_selection_rank
            FROM t_backtest_runs
            WHERE status = :success_status
              AND scheme_id IN :base_scheme_ids
              AND data_source IN :data_sources
        ) AS ranked_backtest_runs
        WHERE dashboard_selection_rank = 1
        ORDER BY scheme_id
        LIMIT :dashboard_source_limit
        """
    ).bindparams(
        bindparam("base_scheme_ids", expanding=True),
        bindparam("data_sources", expanding=True),
    )
    return read_bounded_source_rows(
        connection,
        statement,
        {
            "success_status": "success",
            "base_scheme_ids": base_scheme_ids,
            "data_sources": data_sources,
        },
        dataset="selected_backtest_runs",
        cap=cap,
    )


def read_active_targets(
    connection: Connection,
    *,
    cap: int = MAX_TARGET_SOURCE_ROWS,
) -> list[Mapping[str, Any]]:
    """读取 active 展示目标。"""
    statement = text(
        """
        SELECT target_code, display_name, asset_class, target_type,
               sort_order, status, extra
        FROM t_target_registry
        WHERE status = :active_status
        ORDER BY sort_order, target_code
        LIMIT :dashboard_source_limit
        """
    )
    return read_bounded_source_rows(
        connection,
        statement,
        {"active_status": "active"},
        dataset="active_targets",
        cap=cap,
    )


def read_product_predictions(
    connection: Connection,
    registry_rows: list[Mapping[str, Any]],
    *,
    target_date_range: tuple[str, str] | None = None,
    replacement_plan: DashboardHistoryReplacementPlan | None = None,
    cap: int = MAX_PRODUCT_PREDICTION_SOURCE_ROWS,
) -> list[Mapping[str, Any]]:
    """读取 Detail 所需的有界产品事实。"""
    scopes = _prediction_scopes(registry_rows)
    params, scope_placeholders = _prediction_scope_params(scopes)
    where_clause = (
        "(scheme_id, target_tenor, horizon) IN ("
        + ", ".join(scope_placeholders)
        + ")"
        if scope_placeholders
        else "1 = 0"
    )
    date_filter = (
        " AND target_date >= :target_date_from"
        " AND target_date < :target_date_before"
        if target_date_range is not None
        else ""
    )
    if target_date_range is not None:
        params["target_date_from"], params["target_date_before"] = (
            target_date_range
        )
    excluded_ids = (
        replacement_plan.excluded_product_ids
        if replacement_plan is not None
        else frozenset()
    )
    excluded_filter = ""
    expanding_params = []
    if excluded_ids:
        excluded_filter = " AND id NOT IN :excluded_product_ids"
        params["excluded_product_ids"] = sorted(excluded_ids)
        expanding_params.append(bindparam("excluded_product_ids", expanding=True))
    statement = text(
        f"""
        SELECT id, scheme_id, target_tenor, horizon, predict_date,
               feature_date, target_date, predicted_direction,
               backtest_actual_direction
        FROM t_scheme_predictions
        WHERE {where_clause}{date_filter}{excluded_filter}
        ORDER BY target_date, predict_date, id
        LIMIT :dashboard_source_limit
        """
    )
    if expanding_params:
        statement = statement.bindparams(*expanding_params)
    rows = read_bounded_source_rows(
        connection,
        statement,
        params,
        dataset="product_predictions",
        cap=cap,
    )
    projected_rows = _replacement_rows_in_range(
        replacement_plan,
        registry_rows=registry_rows,
        target_date_range=target_date_range,
    )
    if len(rows) + len(projected_rows) > cap:
        raise DashboardDataError(
            "dashboard source row limit exceeded: "
            f"dataset=product_predictions limit={cap}"
        )
    rows.extend(projected_rows)
    rows.sort(
        key=lambda row: (
            str(row["target_date"]),
            str(row["predict_date"]),
            int(row["id"]),
        )
    )
    return rows


def iter_summary_product_predictions(
    connection: Connection,
    registry_rows: list[Mapping[str, Any]],
    *,
    stats: SummaryPredictionReadStats,
    replacement_plan: DashboardHistoryReplacementPlan | None = None,
    fetch_rows: int = SUMMARY_PREDICTION_FETCH_ROWS,
) -> Iterator[Mapping[str, Any]]:
    """按业务键分组顺序流式读取 Summary 的完整预测历史。"""
    scopes = _prediction_scopes(registry_rows)
    if not scopes:
        return
    params, scope_placeholders = _prediction_scope_params(scopes)
    excluded_ids = (
        replacement_plan.excluded_product_ids
        if replacement_plan is not None
        else frozenset()
    )
    excluded_filter = ""
    if excluded_ids:
        excluded_filter = " AND id NOT IN :excluded_product_ids"
        params["excluded_product_ids"] = sorted(excluded_ids)
    binary_order = (
        "BINARY scheme_id, BINARY target_tenor"
        if connection.dialect.name == "mysql"
        else "scheme_id COLLATE BINARY, target_tenor COLLATE BINARY"
    )
    statement = text(
        f"""
        SELECT id, scheme_id, target_tenor, horizon, predict_date,
               feature_date, target_date, predicted_direction,
               backtest_actual_direction
        FROM t_scheme_predictions
        WHERE (scheme_id, target_tenor, horizon) IN (
            {", ".join(scope_placeholders)}
        ){excluded_filter}
        ORDER BY {binary_order}, horizon, target_date,
                 predict_date, id
        """
    )
    if excluded_ids:
        statement = statement.bindparams(
            bindparam("excluded_product_ids", expanding=True)
        )
    request_context = current_http_request_context()
    if request_context is not None:
        request_context.failure_stage = "dashboard_query"
        request_context.require_remaining("dashboard_query")
        context_started_at = request_context.clock()
    else:
        context_started_at = None
    fetch_started_at = time.perf_counter()
    try:
        result = connection.execution_options(
            stream_results=True,
            yield_per=fetch_rows,
        ).execute(statement, params)
    finally:
        elapsed = time.perf_counter() - fetch_started_at
        stats.fetch_seconds += elapsed
        if request_context is not None and context_started_at is not None:
            request_context.db_seconds += max(
                0.0,
                request_context.clock() - context_started_at,
            )

    exhausted = False
    primary_error: BaseException | None = None
    projected_rows = iter(
        sorted(
            _replacement_rows_in_range(
                replacement_plan,
                registry_rows=registry_rows,
                target_date_range=None,
            ),
            key=_summary_prediction_sort_key,
        )
    )
    projected = next(projected_rows, None)
    try:
        partitions = result.mappings().partitions(fetch_rows)
        while True:
            if request_context is not None:
                request_context.failure_stage = "dashboard_query"
                request_context.require_remaining("dashboard_query")
                context_started_at = request_context.clock()
            else:
                context_started_at = None
            fetch_started_at = time.perf_counter()
            try:
                partition = next(partitions)
            except StopIteration:
                exhausted = True
                stats.exit_reason = "exhausted"
                break
            finally:
                elapsed = time.perf_counter() - fetch_started_at
                stats.fetch_seconds += elapsed
                if request_context is not None and context_started_at is not None:
                    request_context.db_seconds += max(
                        0.0,
                        request_context.clock() - context_started_at,
                    )
            stats.source_rows += len(partition)
            for row in partition:
                while (
                    projected is not None
                    and _summary_prediction_sort_key(projected)
                    <= _summary_prediction_sort_key(row)
                ):
                    stats.source_rows += 1
                    yield projected
                    projected = next(projected_rows, None)
                yield row
        while projected is not None:
            stats.source_rows += 1
            yield projected
            projected = next(projected_rows, None)
    except GeneratorExit as exc:
        primary_error = exc
        stats.exit_reason = "generator_closed"
        raise
    except BaseException as exc:
        primary_error = exc
        stats.exit_reason = f"error:{type(exc).__name__}"
        raise
    finally:
        cleanup_started_at = time.perf_counter()
        if not exhausted and connection.dialect.name == "mysql":
            try:
                _detach_pymysql_unbuffered_result(connection, result)
                connection.invalidate()
            except Exception as cleanup_error:
                logger.warning(
                    "dashboard_stream_invalidate_failed error=%s",
                    type(cleanup_error).__name__,
                )
        try:
            result.close()
        except Exception as cleanup_error:
            logger.warning(
                "dashboard_stream_result_close_failed exit_reason=%s error=%s",
                stats.exit_reason,
                type(cleanup_error).__name__,
            )
            if primary_error is None and exhausted:
                raise
        finally:
            stats.cleanup_seconds += max(
                0.0,
                time.perf_counter() - cleanup_started_at,
            )


def _replacement_rows_in_range(
    replacement_plan: DashboardHistoryReplacementPlan | None,
    *,
    registry_rows: list[Mapping[str, Any]],
    target_date_range: tuple[str, str] | None,
) -> list[dict[str, Any]]:
    if replacement_plan is None:
        return []
    scopes = _prediction_scopes(registry_rows)
    result: list[dict[str, Any]] = []
    for row in replacement_plan.source_rows:
        scope = (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
        target_date = str(row["target_date"])
        if scope not in scopes:
            continue
        if target_date_range is not None and not (
            target_date_range[0] <= target_date < target_date_range[1]
        ):
            continue
        result.append(dict(row))
    return result


def _summary_prediction_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row["scheme_id"]),
        str(row["target_tenor"]),
        int(row["horizon"]),
        str(row["target_date"]),
        str(row["predict_date"]),
        int(row["id"]),
    )


def _detach_pymysql_unbuffered_result(
    connection: Connection,
    result: Any,
) -> None:
    """在物理断开前解除 PyMySQL SSCursor 的排空回调。

    提前退出时不能调用 SSCursor.close()，因为它会同步读完剩余结果；先解除
    driver 对未缓冲结果的引用，再由 ``invalidate()`` 物理关闭 socket。该逻辑
    只作用于项目固定使用的 mysql+pymysql 驱动。
    """
    if connection.dialect.driver != "pymysql":
        return
    cursor = getattr(result, "cursor", None)
    cursor_result = getattr(cursor, "_result", None)
    dbapi_connection = getattr(
        getattr(connection, "connection", None),
        "driver_connection",
        None,
    )
    if cursor is not None:
        cursor._result = None
        cursor.connection = None
    if (
        dbapi_connection is not None
        and getattr(dbapi_connection, "_result", None) is cursor_result
    ):
        dbapi_connection._result = None


def read_live_actuals(
    connection: Connection,
    registry_rows: list[Mapping[str, Any]],
    *,
    target_date_range: tuple[str, str] | None = None,
    target_dates: set[str] | None = None,
    cap: int = MAX_ACTUAL_SOURCE_ROWS,
) -> list[Mapping[str, Any]]:
    """按 active Actual selector 读取关联事实。"""
    if target_dates is not None and not target_dates:
        return []
    active_scopes = sorted(
        {
            (
                str(row["target_tenor"]),
                *live_actual_selector(row.get("task_type")),
            )
            for row in registry_rows
        }
    )
    if not active_scopes:
        return []
    scopes_by_kind: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for target_tenor, actual_kind, target_rule in active_scopes:
        scopes_by_kind[actual_kind].append((target_tenor, target_rule))

    fragments: list[str] = []
    expanding_params = []
    params: dict[str, Any] = {
        "history_start_date": FACTOR_LAB_HISTORY_START_DATE,
    }
    for actual_kind in _ACTUAL_SOURCE_KIND_ORDER:
        scopes = scopes_by_kind.get(actual_kind)
        if not scopes:
            continue
        fragment = _ACTUAL_SOURCE_FRAGMENTS[actual_kind]
        if target_date_range is not None:
            fragment += _ACTUAL_SOURCE_DATE_FILTERS[actual_kind]
        if target_dates is not None:
            fragment += (
                " AND "
                + _ACTUAL_SOURCE_TARGET_DATE_COLUMNS[actual_kind]
                + " IN :actual_target_dates"
            )
        fragments.append(fragment)
        param_name = _ACTUAL_SOURCE_SCOPE_PARAMS[actual_kind]
        expanding_params.append(bindparam(param_name, expanding=True))
        params[param_name] = (
            sorted({target_tenor for target_tenor, _rule in scopes})
            if actual_kind in {"daily_1d", "daily_5d"}
            else scopes
        )

    if target_dates is not None:
        expanding_params.append(bindparam("actual_target_dates", expanding=True))
        params["actual_target_dates"] = sorted(target_dates)
    statement = text(
        "\nUNION ALL\n".join(fragments)
        + "\nLIMIT :dashboard_source_limit"
    ).bindparams(*expanding_params)
    if target_date_range is not None:
        params["target_date_from"], params["target_date_before"] = (
            target_date_range
        )
    return read_bounded_source_rows(
        connection,
        statement,
        params,
        dataset="live_actuals",
        cap=cap,
    )


def _prediction_scopes(
    registry_rows: list[Mapping[str, Any]],
) -> list[tuple[str, str, int]]:
    return sorted(
        {
            (
                str(row["base_scheme_id"]),
                str(row["target_tenor"]),
                int(row["horizon"]),
            )
            for row in registry_rows
        }
    )


def _prediction_scope_params(
    scopes: list[tuple[str, str, int]],
) -> tuple[dict[str, Any], list[str]]:
    params: dict[str, Any] = {}
    placeholders: list[str] = []
    for index, (base_scheme_id, target_tenor, horizon) in enumerate(scopes):
        placeholders.append(
            f"(:base_scheme_id_{index}, :target_tenor_{index}, :horizon_{index})"
        )
        params[f"base_scheme_id_{index}"] = base_scheme_id
        params[f"target_tenor_{index}"] = target_tenor
        params[f"horizon_{index}"] = horizon
    return params, placeholders

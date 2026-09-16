from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

from shared.legacy_prediction_migration import (
    LegacyCorrectedExactEvidence,
    LegacyPredictionMigration,
    load_legacy_corrected_exact_evidence,
    load_legacy_prediction_migrations,
)
from shared.prediction_history_replacement import (
    PredictionHistoryProjection,
    resolve_prediction_history_projection,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PLAN_SCHEMA = "replaced-prediction-history-cleanup-v1"


@dataclass(frozen=True, slots=True)
class ReplacedHistoryCleanupStats:
    """一次精确历史归并事务实际删除的行数。"""

    predictions_deleted: int
    live_runs_deleted: int
    backtest_runs_deleted: int
    versions_deleted: int


def plan_replaced_prediction_history_cleanup(
    engine: Engine,
    *,
    prediction_scheme_id: str,
) -> dict[str, Any]:
    """生成包含恢复原始行与不可变摘要的只读删除计划。"""
    _require_mysql(engine)
    entry, corrected = _load_entry(prediction_scheme_id)
    with engine.connect() as connection:
        projection, _product_rows, source_rows = _resolve_scope(
            connection,
            entry=entry,
            corrected=corrected,
            for_update=False,
        )
        if projection is None:
            raise ValueError("designated replacement source is absent")
        backup = _backup_rows(
            connection,
            projection=projection,
        )
        identity = connection.execute(
            text("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")
        ).mappings().one()
    scope = {
        "prediction_scheme_id": entry.prediction_scheme_id,
        "source_scheme_id": entry.designated_source_scheme_id,
        "source_exact": entry.designated_exact,
        "target_tenor": entry.target_tenor,
        "horizon": entry.horizon,
        "expected_product_delete_count": (
            entry.expected_fact_count + entry.expected_live_fact_count
        ),
        "product_ids": sorted(projection.deletable_product_ids),
        "live_run_ids": sorted(projection.deletable_live_run_ids),
        "backtest_run_ids": sorted(projection.deletable_backtest_run_ids),
        "source_row_ids": sorted(int(row["id"]) for row in source_rows),
    }
    plan = {
        "schema_version": _PLAN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database_identity_sha256": _digest(
            {
                "database_name": identity["database_name"],
                "server_uuid": identity["server_uuid"],
            }
        ),
        "scope": scope,
        "backup": backup,
    }
    plan["scope_digest"] = _digest({"scope": scope, "backup": backup})
    return plan


def apply_replaced_prediction_history_cleanup(
    engine: Engine,
    plan: Mapping[str, Any],
) -> ReplacedHistoryCleanupStats:
    """锁定并重验计划中的精确行，然后原子删除已被替代的历史。"""
    _require_mysql(engine)
    _validate_plan_shape(plan)
    scope = plan["scope"]
    entry, corrected = _load_entry(str(scope["prediction_scheme_id"]))
    with engine.begin() as connection:
        identity = connection.execute(
            text("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")
        ).mappings().one()
        if _digest(
            {
                "database_name": identity["database_name"],
                "server_uuid": identity["server_uuid"],
            }
        ) != plan["database_identity_sha256"]:
            raise ValueError("cleanup plan database identity does not match")
        projection, product_rows, source_rows = _resolve_scope(
            connection,
            entry=entry,
            corrected=corrected,
            for_update=True,
        )
        if projection is None:
            raise ValueError("designated replacement source is absent")
        current_scope = {
            **{key: scope[key] for key in (
                "prediction_scheme_id",
                "source_scheme_id",
                "source_exact",
                "target_tenor",
                "horizon",
                "expected_product_delete_count",
            )},
            "product_ids": sorted(projection.deletable_product_ids),
            "live_run_ids": sorted(projection.deletable_live_run_ids),
            "backtest_run_ids": sorted(projection.deletable_backtest_run_ids),
            "source_row_ids": sorted(int(row["id"]) for row in source_rows),
        }
        current_backup = _backup_rows(
            connection,
            projection=projection,
            for_update=True,
        )
        if (
            current_scope != dict(scope)
            or current_backup != plan["backup"]
            or _digest({"scope": current_scope, "backup": current_backup})
            != plan["scope_digest"]
        ):
            raise ValueError("cleanup plan no longer matches locked database rows")
        expected = entry.expected_fact_count + entry.expected_live_fact_count
        if len(projection.deletable_product_ids) != expected:
            raise ValueError("cleanup plan does not contain the approved fact count")

        predictions_deleted = _delete_ids(
            connection,
            table="t_scheme_predictions",
            column="id",
            ids=projection.deletable_product_ids,
        )
        backtest_runs_deleted = _delete_ids(
            connection,
            table="t_backtest_runs",
            column="id",
            ids=projection.deletable_backtest_run_ids,
        )
        if predictions_deleted != expected:
            raise ValueError("cleanup deleted an unexpected prediction count")
        if backtest_runs_deleted != len(
            projection.deletable_backtest_run_ids
        ):
            raise ValueError("cleanup deleted an unexpected backtest count")
        live_runs_deleted = _delete_unreferenced_live_runs(
            connection,
            projection.deletable_live_run_ids,
        )
        versions_deleted = _delete_unreferenced_retired_versions(
            connection,
            product_rows,
            scheme_id=entry.prediction_scheme_id,
        )
        return ReplacedHistoryCleanupStats(
            predictions_deleted=predictions_deleted,
            live_runs_deleted=live_runs_deleted,
            backtest_runs_deleted=backtest_runs_deleted,
            versions_deleted=versions_deleted,
        )


def _load_entry(
    prediction_scheme_id: str,
) -> tuple[LegacyPredictionMigration, LegacyCorrectedExactEvidence]:
    matches = [
        item
        for item in load_legacy_prediction_migrations(PROJECT_ROOT)
        if item.prediction_scheme_id == prediction_scheme_id
    ]
    if len(matches) != 1:
        raise ValueError("cleanup requires one exact replacement entry")
    entry = matches[0]
    corrected = [
        item
        for item in load_legacy_corrected_exact_evidence(PROJECT_ROOT)
        if item.source_scheme_id == entry.designated_source_scheme_id
        and item.designated_exact == entry.designated_exact
    ]
    if len(corrected) != 1:
        raise ValueError("cleanup corrected evidence is missing or ambiguous")
    return entry, corrected[0]


def _resolve_scope(
    connection: Connection,
    *,
    entry: LegacyPredictionMigration,
    corrected: LegacyCorrectedExactEvidence,
    for_update: bool,
) -> tuple[
    PredictionHistoryProjection | None,
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
]:
    lock = " FOR UPDATE" if for_update and connection.dialect.name != "sqlite" else ""
    source_identity = connection.execute(
        text(
            "SELECT r.base_scheme_id, r.status AS registry_status, "
            "v.runtime_type, v.status AS version_status, v.code_hash, "
            "v.config_hash, v.manifest_hash "
            "FROM t_scheme_registry r JOIN t_scheme_versions v "
            "ON v.scheme_id = r.base_scheme_id "
            "AND v.scheme_version = :source_exact "
            "WHERE r.scheme_id = :source_registry_scheme_id" + lock
        ),
        {
            "source_registry_scheme_id": corrected.source_registry_scheme_id,
            "source_exact": entry.designated_exact,
        },
    ).mappings().one_or_none()
    if source_identity is None:
        return None, [], []
    expected_identity = {
        "base_scheme_id": entry.designated_source_scheme_id,
        "registry_status": "archived",
        "runtime_type": "blackbox_v2",
        "version_status": "retired",
        "code_hash": entry.designated_code_hash,
        "config_hash": entry.designated_config_hash,
        "manifest_hash": entry.designated_manifest_hash,
    }
    if dict(source_identity) != expected_identity:
        raise ValueError("cleanup replacement identity changed")
    product_rows = _scope_rows(
        connection,
        scheme_id=entry.prediction_scheme_id,
        target_tenor=entry.target_tenor,
        horizon=entry.horizon,
        scheme_version=None,
        lock=lock,
    )
    source_rows = _scope_rows(
        connection,
        scheme_id=entry.designated_source_scheme_id,
        target_tenor=entry.target_tenor,
        horizon=entry.horizon,
        scheme_version=entry.designated_exact,
        lock=lock,
    )
    projection = resolve_prediction_history_projection(
        product_rows,
        source_rows,
        entry=entry,
        corrected=corrected,
    )
    return projection, product_rows, source_rows


def _scope_rows(
    connection: Connection,
    *,
    scheme_id: str,
    target_tenor: str,
    horizon: int,
    scheme_version: str | None,
    lock: str,
) -> list[Mapping[str, Any]]:
    version_filter = ""
    params: dict[str, Any] = {
        "scheme_id": scheme_id,
        "target_tenor": target_tenor,
        "horizon": horizon,
    }
    if scheme_version is not None:
        version_filter = " AND scheme_version = :scheme_version"
        params["scheme_version"] = scheme_version
    return list(
        connection.execute(
            text(
                "SELECT id, run_id, backtest_run_id, scheme_version, scheme_id, "
                "target_tenor, horizon, predict_date, feature_date, target_date, "
                "predicted_direction, backtest_actual_direction "
                "FROM t_scheme_predictions WHERE scheme_id = :scheme_id "
                "AND target_tenor = :target_tenor AND horizon = :horizon"
                + version_filter
                + " ORDER BY target_date, predict_date, id"
                + lock
            ),
            params,
        ).mappings()
    )


def _backup_rows(
    connection: Connection,
    *,
    projection: PredictionHistoryProjection,
    for_update: bool = False,
) -> dict[str, Any]:
    lock = " FOR UPDATE" if for_update and connection.dialect.name != "sqlite" else ""
    product_ids = projection.deletable_product_ids
    product_backup = _select_ids(
        connection,
        table="t_scheme_predictions",
        column="id",
        ids=product_ids,
        lock=lock,
    )
    live_runs = _select_ids(
        connection,
        table="t_scheme_runs",
        column="run_id",
        ids=projection.deletable_live_run_ids,
        lock=lock,
    )
    backtest_runs = _select_ids(
        connection,
        table="t_backtest_runs",
        column="id",
        ids=projection.deletable_backtest_run_ids,
        lock=lock,
    )
    backtest_predictions = _select_ids(
        connection,
        table="t_backtest_predictions",
        column="run_id",
        ids=projection.deletable_backtest_run_ids,
        lock=lock,
    )
    version_identities = sorted(
        {
            (str(row["scheme_id"]), str(row.get("scheme_version") or ""))
            for row in product_backup
            if row.get("scheme_version")
        }
    )
    versions: list[dict[str, Any]] = []
    for scheme_id, scheme_version in version_identities:
        rows = connection.execute(
            text(
                "SELECT * FROM t_scheme_versions WHERE scheme_id = :scheme_id "
                "AND scheme_version = :scheme_version" + lock
            ),
            {"scheme_id": scheme_id, "scheme_version": scheme_version},
        ).mappings()
        versions.extend(dict(row) for row in rows)
    return _json_safe(
        {
            "product_predictions": product_backup,
            "live_runs": live_runs,
            "backtest_runs": backtest_runs,
            "backtest_predictions": backtest_predictions,
            "versions": versions,
        }
    )


def _select_ids(
    connection: Connection,
    *,
    table: str,
    column: str,
    ids: frozenset[int],
    lock: str,
) -> list[dict[str, Any]]:
    if not ids:
        return []
    statement = text(
        f"SELECT * FROM {table} WHERE {column} IN :row_ids "
        f"ORDER BY {column}" + lock
    ).bindparams(bindparam("row_ids", expanding=True))
    return [
        dict(row)
        for row in connection.execute(
            statement,
            {"row_ids": sorted(ids)},
        ).mappings()
    ]


def _delete_ids(
    connection: Connection,
    *,
    table: str,
    column: str,
    ids: frozenset[int],
) -> int:
    if not ids:
        return 0
    statement = text(
        f"DELETE FROM {table} WHERE {column} IN :row_ids"
    ).bindparams(bindparam("row_ids", expanding=True))
    return int(
        connection.execute(statement, {"row_ids": sorted(ids)}).rowcount or 0
    )


def _delete_unreferenced_live_runs(
    connection: Connection,
    run_ids: frozenset[int],
) -> int:
    deleted = 0
    for run_id in sorted(run_ids):
        references = connection.execute(
            text(
                "SELECT "
                "(SELECT COUNT(*) FROM t_scheme_predictions WHERE run_id=:run_id) + "
                "(SELECT COUNT(*) FROM t_schedule_items WHERE current_run_id=:run_id) + "
                "(SELECT COUNT(*) FROM t_schedule_item_targets "
                " WHERE accepted_run_id=:run_id)"
            ),
            {"run_id": run_id},
        ).scalar_one()
        if int(references or 0) == 0:
            deleted += int(
                connection.execute(
                    text("DELETE FROM t_scheme_runs WHERE run_id=:run_id"),
                    {"run_id": run_id},
                ).rowcount
                or 0
            )
    return deleted


def _delete_unreferenced_retired_versions(
    connection: Connection,
    product_rows: list[Mapping[str, Any]],
    *,
    scheme_id: str,
) -> int:
    versions = sorted(
        {
            str(row.get("scheme_version") or "")
            for row in product_rows
            if row.get("scheme_version")
        }
    )
    deleted = 0
    for version in versions:
        references = connection.execute(
            text(
                "SELECT "
                "(SELECT COUNT(*) FROM t_scheme_predictions "
                " WHERE scheme_id=:scheme_id AND scheme_version=:version) + "
                "(SELECT COUNT(*) FROM t_scheme_runs "
                " WHERE scheme_id=:scheme_id AND scheme_version=:version)"
            ),
            {"scheme_id": scheme_id, "version": version},
        ).scalar_one()
        if int(references or 0) == 0:
            deleted += int(
                connection.execute(
                    text(
                        "DELETE FROM t_scheme_versions WHERE scheme_id=:scheme_id "
                        "AND scheme_version=:version AND status='retired'"
                    ),
                    {"scheme_id": scheme_id, "version": version},
                ).rowcount
                or 0
            )
    return deleted


def _validate_plan_shape(plan: Mapping[str, Any]) -> None:
    scope = plan.get("scope")
    backup = plan.get("backup")
    if (
        set(plan)
        != {
            "schema_version",
            "created_at",
            "database_identity_sha256",
            "scope",
            "backup",
            "scope_digest",
        }
        or plan.get("schema_version") != _PLAN_SCHEMA
        or not isinstance(scope, Mapping)
        or set(scope)
        != {
            "prediction_scheme_id",
            "source_scheme_id",
            "source_exact",
            "target_tenor",
            "horizon",
            "expected_product_delete_count",
            "product_ids",
            "live_run_ids",
            "backtest_run_ids",
            "source_row_ids",
        }
        or not isinstance(backup, Mapping)
        or set(backup)
        != {
            "product_predictions",
            "live_runs",
            "backtest_runs",
            "backtest_predictions",
            "versions",
        }
        or not all(isinstance(value, list) for value in backup.values())
        or not isinstance(plan.get("created_at"), str)
        or not _is_sha256(plan.get("database_identity_sha256"))
        or not _is_sha256(plan.get("scope_digest"))
        or not all(
            isinstance(scope.get(key), str) and bool(scope[key])
            for key in (
                "prediction_scheme_id",
                "source_scheme_id",
                "source_exact",
                "target_tenor",
            )
        )
        or not isinstance(scope.get("horizon"), int)
        or isinstance(scope.get("horizon"), bool)
        or int(scope["horizon"]) <= 0
        or not isinstance(scope.get("expected_product_delete_count"), int)
        or isinstance(scope.get("expected_product_delete_count"), bool)
        or int(scope["expected_product_delete_count"]) <= 0
        or scope.get("expected_product_delete_count")
        != len(scope.get("product_ids", []))
        or len(backup.get("product_predictions", []))
        != scope.get("expected_product_delete_count")
        or any(
            not _valid_id_list(scope.get(key))
            for key in (
                "product_ids",
                "live_run_ids",
                "backtest_run_ids",
                "source_row_ids",
            )
        )
        or _digest({"scope": dict(scope), "backup": dict(backup)})
        != plan.get("scope_digest")
    ):
        raise ValueError("cleanup plan schema is invalid")


def _valid_id_list(value: Any) -> bool:
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in value
    ):
        return False
    return value == sorted(set(value))


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    )


def _require_mysql(engine: Engine) -> None:
    if engine.dialect.name != "mysql":
        raise ValueError("replaced history cleanup requires MySQL")


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _digest(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

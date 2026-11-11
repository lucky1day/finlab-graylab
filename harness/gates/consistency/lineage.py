from __future__ import annotations

from typing import Any, Mapping

from shared.historical_reference_compatibility import HistoricalBacktestReference
from shared.legacy_prediction_migration import (
    LegacyCorrectedExactEvidence,
    LegacyPredictionMigration,
)
from shared.legacy_backtest_lineage import LegacyBacktestLineage
from shared.prediction_context import LIVE_PREDICTION_PHASES
from harness.gates.consistency.common import (
    DatabaseSnapshot,
    _is_sha256,
    _is_snapshot_id,
    _json_mapping,
    _product_business_key,
    _required_text,
)
from harness.gates.consistency.legacy import (
    _legacy_backtest_lineage_evidence,
    _legacy_migration_evidence,
    _resolve_legacy_backtest_lineage_matches,
    _resolve_legacy_migration_matches,
)
from harness.gates.consistency.completeness import _validated_live_only_baselines


def validate_snapshot_lineage(
    snapshot: DatabaseSnapshot,
    *,
    historical_references: frozenset[HistoricalBacktestReference],
    legacy_migrations: frozenset[
        LegacyPredictionMigration
    ] = frozenset(),
    corrected_exact_evidence: frozenset[
        LegacyCorrectedExactEvidence
    ] = frozenset(),
    legacy_backtest_lineages: frozenset[
        LegacyBacktestLineage
    ] = frozenset(),
) -> tuple[list[str], dict[str, Any]]:
    """核对产品事实引用的 exact、run、版本与输入工件血缘。"""
    errors: list[str] = []
    authority_gaps: set[str] = set()
    legacy_matches = _resolve_legacy_migration_matches(
        snapshot,
        legacy_migrations,
        corrected_exact_evidence,
    )
    legacy_lineage_matches = _resolve_legacy_backtest_lineage_matches(
        snapshot,
        legacy_backtest_lineages,
    )
    registry_scopes = {
        (str(row["base_scheme_id"]), str(row["target_tenor"]), int(row["horizon"]))
        for row in snapshot.registry_rows
    }
    referenced_backtest_scopes = {
        (str(row["scheme_id"]), str(row["target_tenor"]), int(row["horizon"]))
        for row in snapshot.prediction_rows
        if row.get("backtest_run_id") is not None
    }
    live_only_keys, _ = _validated_live_only_baselines(
        snapshot, legacy_migrations, registry_scopes - referenced_backtest_scopes
    )
    versions = {
        (str(row["scheme_id"]), str(row["scheme_version"])): row
        for row in snapshot.version_rows
    }
    artifacts = {
        str(row["artifact_id"]): row for row in snapshot.input_artifact_rows
    }
    live_runs = {int(row["reference_id"]): row for row in snapshot.live_runs}
    backtest_runs = {
        int(row["reference_id"]): row for row in snapshot.backtest_runs
    }
    registry = {
        (str(row["base_scheme_id"]), str(row["target_tenor"]), int(row["horizon"])): row
        for row in snapshot.registry_rows
    }
    compatibility = {
        (item.prediction_scheme_id, item.backtest_source_scheme_id)
        for item in historical_references
    }
    checked_artifacts: set[str] = set()
    checked_versions: set[tuple[str, str]] = set()
    legacy_fact_count = 0

    def validate_version(
        identity: tuple[str, str],
        version: Mapping[str, Any] | None,
    ) -> None:
        if version is None:
            errors.append(
                "prediction exact has no version row: "
                f"scheme_id={identity[0]} exact={identity[1]}"
            )
            return
        if identity in checked_versions:
            return
        checked_versions.add(identity)
        runtime_type = str(version.get("runtime_type") or "")
        if runtime_type not in {"native_adapter", "blackbox_v2"}:
            errors.append(
                f"version runtime is invalid: scheme_id={identity[0]} "
                f"exact={identity[1]}"
            )
        for field in ("code_hash", "config_hash", "manifest_hash"):
            value = version.get(field)
            if value in {None, ""}:
                if not (
                    field == "manifest_hash"
                    and identity in legacy_live_only_versions
                    and runtime_type == "native_adapter"
                    and version.get("status") == "retired"
                ):
                    authority_gaps.add(
                        f"version lineage is missing: scheme_id={identity[0]} "
                        f"exact={identity[1]} field={field}"
                    )
            elif not _is_sha256(value):
                errors.append(
                    f"version lineage hash invalid: scheme_id={identity[0]} "
                    f"exact={identity[1]} field={field}"
                )
        if runtime_type == "blackbox_v2":
            snapshot_id = version.get("data_snapshot_id")
            if snapshot_id in {None, ""}:
                authority_gaps.add(
                    f"version data snapshot is missing: scheme_id={identity[0]} "
                    f"exact={identity[1]}"
                )
            elif not _is_snapshot_id(snapshot_id):
                errors.append(
                    f"version data snapshot is invalid: scheme_id={identity[0]} "
                    f"exact={identity[1]}"
                )

    candidate_live_only_versions = {
        (str(row["scheme_id"]), str(row["scheme_version"]))
        for row in snapshot.prediction_rows
        if _product_business_key(row) in live_only_keys.get(
            (str(row["scheme_id"]), str(row["target_tenor"]), int(row["horizon"])),
            frozenset(),
        )
    }
    legacy_live_only_versions = {
        identity
        for identity in candidate_live_only_versions
        if all(
            _product_business_key(row) in live_only_keys.get(
                (str(row["scheme_id"]), str(row["target_tenor"]), int(row["horizon"])),
                frozenset(),
            )
            for row in snapshot.prediction_rows
            if (str(row["scheme_id"]),
                str(row["scheme_version"])) == identity
        )
    }
    for row in snapshot.prediction_rows:
        scope = (str(row["scheme_id"]), str(row["target_tenor"]), int(row["horizon"]))
        registry_row = registry.get(scope)
        if registry_row is None:
            continue
        backtest_run_id = row.get("backtest_run_id")
        legacy_match = (
            legacy_matches.get(int(backtest_run_id))
            if backtest_run_id is not None
            else None
        )
        if (
            legacy_match is not None
            and not row.get("scheme_version")
            and scope
            == (
                legacy_match.entry.prediction_scheme_id,
                legacy_match.entry.target_tenor,
                legacy_match.entry.horizon,
            )
        ):
            legacy_fact_count += 1
            continue
        exact = _required_text(row.get("scheme_version"), field="prediction exact")
        lineage_scheme_id = scope[0]
        version_identity = (lineage_scheme_id, exact)
        version = versions.get(version_identity)
        validate_version(version_identity, version)
        if row.get("run_id") is not None:
            run = live_runs.get(int(row["run_id"]))
            if run is None:
                continue
            if str(run.get("scheme_id") or "") != lineage_scheme_id:
                errors.append(f"live run scheme drift: run_id={row['run_id']}")
            if run.get("scheme_version") != exact:
                errors.append(f"live run exact drift: run_id={row['run_id']}")
            version_runtime = version.get("runtime_type") if version else None
            if run.get("runtime_type") != version_runtime:
                errors.append(f"live run runtime drift: run_id={row['run_id']}")
            if run.get("status") != "success":
                errors.append(f"live run is not successful: run_id={row['run_id']}")
            if run.get("prediction_phase") not in LIVE_PREDICTION_PHASES:
                errors.append(f"live run phase is invalid: run_id={row['run_id']}")
            if version_runtime == "blackbox_v2":
                snapshot_id = run.get("data_snapshot_id")
                if snapshot_id in {None, ""}:
                    authority_gaps.add(
                        f"live run data snapshot is missing: run_id={row['run_id']}"
                    )
                elif not _is_snapshot_id(snapshot_id):
                    errors.append(
                        f"live run data snapshot drift: run_id={row['run_id']}"
                    )
            else:
                artifact_id = str(run.get("input_artifact_id") or "")
                artifact = artifacts.get(artifact_id)
                if not artifact_id or artifact is None:
                    if not (
                        not artifact_id
                        and version_runtime == "native_adapter"
                        and version is not None
                        and version.get("status") == "retired"
                        and _product_business_key(row) in live_only_keys.get(
                            scope, frozenset()
                        )
                    ):
                        authority_gaps.add(
                            f"live run input artifact is missing: run_id={row['run_id']}"
                        )
                elif artifact_id not in checked_artifacts:
                    checked_artifacts.add(artifact_id)
                    if (
                        artifact.get("scheme_id") != scope[0]
                        or artifact.get("scheme_version") != exact
                        or not _is_sha256(artifact.get("content_hash"))
                        or not _is_sha256(artifact.get("schema_hash"))
                    ):
                        errors.append(
                            "live input artifact lineage drift: "
                            f"artifact_id={artifact_id}"
                        )
        elif row.get("backtest_run_id") is not None:
            run = backtest_runs.get(int(row["backtest_run_id"]))
            if run is None:
                continue
            legacy_lineage = legacy_lineage_matches.get(
                int(row["backtest_run_id"])
            )
            source_scheme_id = str(run.get("scheme_id") or "")
            summary = _json_mapping(run.get("summary"), field="backtest summary")
            source_exact = str(summary.get("scheme_version") or "")
            if source_scheme_id == scope[0]:
                if source_exact != exact:
                    errors.append(
                        f"backtest exact drift: run_id={row['backtest_run_id']}"
                    )
            elif (scope[0], source_scheme_id) not in compatibility:
                errors.append(
                    f"unapproved backtest lineage source: prediction={scope[0]} "
                    f"source={source_scheme_id}"
                )
            source_identity = (source_scheme_id, source_exact)
            source_version = versions.get(source_identity)
            validate_version(source_identity, source_version)
            if (
                source_scheme_id != scope[0]
                and source_version is not None
                and source_version.get("status") != "retired"
            ):
                errors.append(
                    "historical backtest source exact is not retired: "
                    f"scheme_id={source_scheme_id} exact={source_exact}"
                )
            for field in ("code_hash", "config_hash"):
                value = run.get(field)
                if value in {None, ""}:
                    if legacy_lineage is None:
                        authority_gaps.add(
                            "backtest lineage is missing: "
                            f"run_id={row['backtest_run_id']} field={field}"
                        )
                elif not _is_sha256(value):
                    errors.append(
                        f"backtest lineage hash invalid: run_id={row['backtest_run_id']} "
                        f"field={field}"
                    )
                elif source_version is not None and value != source_version.get(field):
                    errors.append(
                        f"backtest lineage drift: run_id={row['backtest_run_id']} "
                        f"field={field}"
                    )
            manifest_hash = summary.get("manifest_hash")
            if manifest_hash in {None, ""}:
                if legacy_lineage is None:
                    authority_gaps.add(
                        "backtest lineage is missing: "
                        f"run_id={row['backtest_run_id']} field=manifest_hash"
                    )
            elif not _is_sha256(manifest_hash):
                errors.append(
                    f"backtest lineage hash invalid: run_id={row['backtest_run_id']} "
                    "field=manifest_hash"
                )
            elif (
                source_version is not None
                and manifest_hash != source_version.get("manifest_hash")
            ):
                errors.append(
                    f"backtest manifest drift: run_id={row['backtest_run_id']}"
                )
            input_identity = run.get("input_artifact_hash")
            if input_identity in {None, ""}:
                if legacy_lineage is None:
                    authority_gaps.add(
                        f"backtest input identity is missing: "
                        f"run_id={row['backtest_run_id']}"
                    )
            elif _is_snapshot_id(input_identity):
                if (
                    summary.get("data_snapshot_id") != input_identity
                    or (
                        source_version is not None
                        and source_version.get("runtime_type") == "blackbox_v2"
                        and source_version.get("data_snapshot_id") != input_identity
                    )
                ):
                    errors.append(
                        f"backtest input snapshot drift: "
                        f"run_id={row['backtest_run_id']}"
                    )
            elif not _is_sha256(input_identity):
                errors.append(
                    f"backtest input identity is invalid: "
                    f"run_id={row['backtest_run_id']}"
                )
    return errors, {
        "version_count": len(versions),
        "live_run_count": len(live_runs),
        "backtest_run_count": len(backtest_runs),
        "input_artifact_count": len(artifacts),
        "authority_gaps": sorted(authority_gaps),
        "legacy_migration_fact_count": legacy_fact_count,
        "legacy_migrations": _legacy_migration_evidence(legacy_matches),
        "legacy_backtest_lineages": _legacy_backtest_lineage_evidence(
            legacy_lineage_matches
        ),
    }

from __future__ import annotations

from dataclasses import replace

from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.repository import register_blackbox_draft_identity


def create_draft_identity(
    engine,
    cfg: SchemeConfig,
    *,
    environment_fingerprint: str,
    data_snapshot_id: str,
    expected_harness_run_id: str,
):
    """由 shadow-register 在 insert-only 语义下创建 draft+paused 身份。"""
    pinned = _reload_pinned_canonical(cfg)
    enriched = replace(
        pinned,
        environment_fingerprint=str(environment_fingerprint),
        data_snapshot_id=str(data_snapshot_id),
    )
    return register_blackbox_draft_identity(
        engine,
        enriched,
        expected_harness_run_id=expected_harness_run_id,
    )


def _reload_pinned_canonical(cfg: SchemeConfig) -> SchemeConfig:
    current = load_scheme_config(cfg.path / "config.yaml")
    initial_identity = _canonical_identity(cfg)
    current_identity = _canonical_identity(current)
    mismatches = [
        f"{field}: initial={initial_identity[field]!r}, "
        f"current={current_identity[field]!r}"
        for field in initial_identity
        if initial_identity[field] != current_identity[field]
    ]
    if mismatches:
        raise ValueError(
            "Blackbox canonical delivery drift before draft registration: "
            + "; ".join(mismatches)
        )
    return current


def _canonical_identity(cfg: SchemeConfig) -> dict[str, object]:
    return {
        "scheme_id": cfg.scheme_id,
        "scheme_version": cfg.scheme_version,
        "code_hash": cfg.code_hash,
        "config_hash": cfg.config_hash,
        "manifest_hash": cfg.manifest_hash,
        "runtime_type": cfg.runtime_type,
        "status": cfg.status,
        "version_status": cfg.version_status,
        "name": cfg.name,
        "description": cfg.description,
        "algorithm_version": cfg.algorithm_version,
        "contract_version": cfg.contract_version,
        "runtime_profile": cfg.runtime_profile,
        "data_schema_version": cfg.data_schema_version,
        "input_source": cfg.input_source,
        "platform_inputs": tuple(cfg.platform_inputs),
        "horizon": cfg.horizon,
        "task_type": cfg.task_type,
        "tenors": tuple(cfg.tenors),
        "frequency": cfg.frequency,
        "target_rule": cfg.target_rule,
        "schedule_cron": cfg.schedule.cron,
        "schedule_timezone": cfg.schedule.timezone,
    }

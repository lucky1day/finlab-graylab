from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from scheduler.discovery import SchemeConfig


DEFERRED_NATIVE_SCHEME_IDS = frozenset(
    {
        "daily_1y_xgb_1y13_0629",
        "daily_5y_lgbm_5y10_0629",
        "daily_10y_lgbm_10y04_0629",
        "monthly_1y_rf_top30_0629",
        "monthly_5y_knn_top20_0629",
        "monthly_10y_rf_top5_0629",
        "weekly_avg_1y_lgbm_0529",
        "weekly_avg_5y_lgbm_0529",
        "weekly_avg_10y_lgbm_0529",
    }
)


def _candidate_configs() -> list[SchemeConfig]:
    from scheduler.discovery import discover_schemes

    project_root = Path(__file__).resolve().parents[1]
    return discover_schemes(project_root / "schemes", strict=True)


def test_c56_defers_the_exact_nine_native_schemes() -> None:
    from shared.source_runtime_database import SOURCE_RUNTIME_SCHEME_IDS

    configs = {config.scheme_id: config for config in _candidate_configs()}

    assert DEFERRED_NATIVE_SCHEME_IDS == SOURCE_RUNTIME_SCHEME_IDS
    assert len(configs) == 65
    assert {
        scheme_id
        for scheme_id, config in configs.items()
        if config.status == "paused"
    } == DEFERRED_NATIVE_SCHEME_IDS
    assert all(
        configs[scheme_id].runtime_type == "native_adapter"
        for scheme_id in DEFERRED_NATIVE_SCHEME_IDS
    )


def test_c56_active_execution_scope_is_56() -> None:
    configs = _candidate_configs()
    active = [config for config in configs if config.status == "active"]

    assert len(active) == 56
    assert sum(len(config.tenors) for config in configs) == 69
    assert sum(len(config.tenors) for config in active) == 60
    assert Counter(config.runtime_type for config in active) == {
        "native_adapter": 17,
        "blackbox_v2": 39,
    }
    assert Counter(config.frequency for config in active) == {
        "daily": 39,
        "weekly": 12,
        "monthly": 5,
    }


def test_c56_keeps_deferred_scheme_implementations_in_release() -> None:
    configs = {config.scheme_id: config for config in _candidate_configs()}

    for scheme_id in DEFERRED_NATIVE_SCHEME_IDS:
        scheme_dir = configs[scheme_id].path
        assert scheme_dir.is_dir()
        assert (scheme_dir / "predict.py").is_file()


def test_c56_preserves_native_policy_and_all_composite_owners() -> None:
    from harness.contracts.onboarding_policy import load_onboarding_policy
    from shared.scheme_owner_registry import load_scheme_owners

    project_root = Path(__file__).resolve().parents[1]
    configs = _candidate_configs()
    native_ids = {
        config.scheme_id
        for config in configs
        if config.runtime_type == "native_adapter"
    }
    composite_ids = {
        f"{config.scheme_id}__h{config.horizon}__{tenor}"
        for config in configs
        for tenor in config.tenors
    }
    policy = load_onboarding_policy(project_root)
    owners = load_scheme_owners(project_root)

    assert set(policy.legacy_native_scheme_ids) == native_ids
    assert len(native_ids) == 26
    assert set(owners) == composite_ids
    assert len(composite_ids) == 69

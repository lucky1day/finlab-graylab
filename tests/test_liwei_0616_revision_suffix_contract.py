from __future__ import annotations

import importlib

import pandas as pd
import pytest

from shared.liwei_0616_cache_contract import CACHE_MUTATION_POLICY_ENV


@pytest.mark.parametrize(
    "module_name",
    (
        "schemes.liwei_0616_10y01_full_oos_k3_div_k10.inference",
        "schemes.liwei_0616_10y01_cons_say_k3_div_k10.inference",
        "schemes.liwei_0616_10y02_cons_say_k3_div_k5.inference",
        "schemes.liwei_0616_5y01_full_oos_k3_div_k10.inference",
        "schemes.liwei_0616_cons_sda_k3_div_k10.inference",
        "schemes.liwei_0616_5y_auc_static_all_k3_div_k10.inference",
        "schemes.liwei_0616_5y_auc_yearly_all_k3_div_k10.inference",
        "schemes.liwei_0616_5y_ic_yearly_all_k3_div_k10.inference",
        "schemes.liwei_0616_7y01_cons_say_k3_div_k10.inference",
        "schemes.liwei_0616_7y03_cons_all_k3_div_k8.inference",
    ),
)
def test_incremental_cache_declares_bounded_daily_dependency(
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CACHE_MUTATION_POLICY_ENV, raising=False)
    module = importlib.import_module(module_name)
    captured: dict[str, object] = {}

    def capture_prepare(**kwargs):
        captured.update(kwargs)
        return {}, {"status": "captured"}

    monkeypatch.setattr(module, "prepare_phase_a_caches", capture_prepare)
    monkeypatch.setattr(
        module,
        "build_auxiliary_dependency_projection",
        lambda **_kwargs: None,
    )

    module._prepare_incremental_phase_a_caches(
        daily_df=pd.DataFrame(),
        weekly_df=pd.DataFrame(),
        monthly_df=pd.DataFrame(),
        date_to_week=None,
        test_ranges=(("2026-08-18", "2026-08-18"),),
        n_workers=1,
        cache_root=None,
    )

    spec = captured["spec"]
    assert spec.daily_dependency_lookback_rows == max(
        module.HORIZON,
        module.PURGE_GAP,
    )
    assert (
        spec.daily_dependency_proof
        == "liwei_0616_daily_revision_suffix_v1"
    )

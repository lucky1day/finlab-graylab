from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from scheduler.scheme_runner import run_scheme


# 每个 liwei_0616 cache family 取一个代表 adapter。预热按 family 而不是按 tenor
# 组织：同一 tenor 下可以有多个互不共享 Phase-A cache 的 family，漏掉任何一个都会
# 让该 family 只能在日频窗口内冷重建。
# 该清单必须覆盖 `schemes/*/inference.py` 中出现的全部 CACHE_FAMILY，
# 由 tests.test_prewarm_liwei_0616_phase_a_cache 静态守护。
PREWARM_SCHEMES = (
    "liwei_0616_cons_sda_k3_div_k10",           # liwei_0616_5y_v31
    "liwei_0616_5y_auc_static_all_k3_div_k10",  # liwei_0616_5y_allk10_auc_static_v1
    "liwei_0616_5y_auc_yearly_all_k3_div_k10",  # liwei_0616_5y_allk10_auc_yearly_v1
    "liwei_0616_5y_ic_yearly_all_k3_div_k10",   # liwei_0616_5y_allk10_ic_yearly_v1
    "liwei_0616_7y01_cons_say_k3_div_k10",      # liwei_0616_7y01_v31
    "liwei_0616_7y03_cons_all_k3_div_k8",       # liwei_0616_7y03_v31
    "liwei_0616_10y02_cons_say_k3_div_k5",      # liwei_0616_10y_v61
)


def prewarm(
    predict_date: str,
    *,
    run_scheme_fn: Callable[[str, str], list[dict[str, Any]]] = run_scheme,
) -> list[dict[str, Any]]:
    """逐 cache family 运行代表 adapter 预热 baseline cache，不写业务数据库。"""
    results: list[dict[str, Any]] = []
    for scheme_id in PREWARM_SCHEMES:
        records = run_scheme_fn(scheme_id, predict_date)
        if not records:
            raise RuntimeError(f"scheme {scheme_id} returned no prediction records")
        extra = records[0].get("extra") or {}
        cache_audit = extra.get("phase_a_cache")
        if not isinstance(cache_audit, dict) or not cache_audit:
            raise RuntimeError(f"scheme {scheme_id} missing phase_a_cache audit")
        results.append({"scheme_id": scheme_id, **cache_audit})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Prewarm liwei_0616 shared Phase A caches without DB writes.")
    parser.add_argument("--predict-date", required=True, help="信号发出日，格式 YYYY-MM-DD")
    args = parser.parse_args()
    print(json.dumps(prewarm(args.predict_date), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

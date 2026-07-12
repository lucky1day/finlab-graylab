from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from scheduler.scheme_runner import run_scheme


PREWARM_SCHEMES = (
    "liwei_0616_cons_sda_k3_div_k10",
    "liwei_0616_7y01_cons_say_k3_div_k10",
    "liwei_0616_10y02_cons_say_k3_div_k5",
)


def prewarm(
    predict_date: str,
    *,
    run_scheme_fn: Callable[[str, str], list[dict[str, Any]]] = run_scheme,
) -> list[dict[str, Any]]:
    """运行三个代表 adapter 预热 5Y/7Y/10Y baseline cache，不写业务数据库。"""
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

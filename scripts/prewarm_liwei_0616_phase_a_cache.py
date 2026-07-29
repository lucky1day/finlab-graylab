from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from scheduler.scheme_runner import run_scheme
from shared.liwei_0616_cache_contract import (
    APPROVED_PHASE_A_CACHE_PUBLISHERS,
)


# 预热必须逐 cache family 且用该 family 的 **publisher** 运行。
#
# 两条约束缺一不可：
#   1. 按 family 而不是按 tenor —— 同一 tenor 下可以有多个互不共享 Phase-A cache
#      的 family，漏掉任何一个都会让它只能在日频窗口内冷重建；
#   2. 代表必须是 publisher —— 只有 publisher 能建并原子切换 current；消费者走
#      `_validated_consumer_hit`，在 publisher 尚未刷新时直接抛
#      `CACHE_PUBLISHER_REQUIRED` 硬失败，永远预热不出缓存。
#
# 因此清单直接由 `APPROVED_PHASE_A_CACHE_PUBLISHERS` 派生，杜绝手工维护漂移；
# 顺序按 family 名固定，保证可复现。由
# tests.test_prewarm_liwei_0616_phase_a_cache 静态守护。
PREWARM_SCHEMES: tuple[str, ...] = tuple(
    publisher
    for _family, (_tenor, publisher) in sorted(
        APPROVED_PHASE_A_CACHE_PUBLISHERS.items()
    )
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

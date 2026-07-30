"""每日 gray_live 信号自动生成器（最简调度）。

复用已验证的 gray_live 执行链，为全部 active 日频方案（17 Native + 8 Blackbox V2 = 25）
每交易日各出一次信号，统一写 gray_live。不走 ledger/epoch/migration-018 重装甲路径。

设计要点：
- legacy 模式下 gray_live 无需 native generation authority / trusted qualification；
  Liwei 缓存 publisher 自行增量 suffix，consumer 直接 hit。
- 唯一硬约束：Liwei 家族 publisher 必须先于 consumer 执行，否则 consumer 会因
  缓存覆盖不足 fail-closed（CACHE_PUBLISHER_REQUIRED）。
- 落库沿用 repository.insert_run_predictions（UPSERT，天然幂等，可安全重跑）。
- 本模块只做日常出信号，不复制历史补缺（H2 operator）的 authority/plan/monkeypatch。

并发调度（单进程内、错峰替代方案）：
- 重方案（实际重训、各占 ~10 workers）用线程池并发，`--max-heavy` 限制同时运行数
  （默认 3；32 核 / 10 workers ≈ 3，避免 CPU 抢占）。重方案优先提交、最先启动。
- consumer 依赖的 publisher 完成且 success 后才提交；publisher 失败则 consumer 跳过。
- 轻方案（consumer 命中缓存、非 Liwei 快模型、V2 读 DataBridge）不计入重方案并发预算，
  在重方案调度的同时以较高并发跑完。
- execute_scheme 各自 spawn forecast_env 子进程，主控用线程编排即可。
"""
from __future__ import annotations

import argparse
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date

from scheduler.calendar import is_trading_day
from scheduler.discovery import discover_schemes
from scheduler.executor import DEFAULT_ALGO_ENV, execute_scheme
from scheduler.repository import create_engine_from_env

logger = logging.getLogger(__name__)

PREDICTION_PHASE = "gray_live"

# 单机默认资源约束：32 逻辑核，重方案各 ~10 workers，安全同时 3 个。
DEFAULT_MAX_HEAVY = 3
DEFAULT_LIGHT_CONCURRENCY = 6

# Liwei consumer -> 其依赖的 publisher（publisher 必须先成功扩缓存）。
# 其余 Liwei 方案 publisher==consumer（自给自足），无跨方案依赖。
LIWEI_CONSUMER_DEPENDENCIES: dict[str, str] = {
    "liwei_0616_10y01_cons_say_k3_div_k10": "liwei_0616_10y01_full_oos_k3_div_k10",
    "liwei_0616_10y02_cons_say_k3_div_k5": "liwei_0616_10y01_full_oos_k3_div_k10",
    "liwei_0616_cons_sda_k3_div_k10": "liwei_0616_5y01_full_oos_k3_div_k10",
}

# 重方案（实际重训、约占 10 workers）。其余日频方案视为轻方案。
# Liwei consumer 命中缓存不训练，属轻方案。
HEAVY_SCHEMES: frozenset[str] = frozenset({
    "daily_5y_2_v28",
    "daily_7y_1_v28",
    "liwei_0616_10y01_full_oos_k3_div_k10",
    "liwei_0616_5y01_full_oos_k3_div_k10",
    "liwei_0616_5y_auc_static_all_k3_div_k10",
    "liwei_0616_5y_auc_yearly_all_k3_div_k10",
    "liwei_0616_5y_ic_yearly_all_k3_div_k10",
    "liwei_0616_7y01_cons_say_k3_div_k10",
    "liwei_0616_7y03_cons_all_k3_div_k8",
})


def _is_heavy(scheme_id: str) -> bool:
    return scheme_id in HEAVY_SCHEMES


@dataclass
class RunnerSummary:
    """一次每日 gray_live 运行的汇总结果。"""

    predict_date: str
    is_trading_day: bool
    total: int = 0
    success: int = 0
    partial: int = 0
    failed: int = 0
    skipped: int = 0
    records_written: int = 0
    details: list[tuple[str, str, int, str | None]] | None = None

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = []
        # 并发写入 summary 的保护锁。
        self._lock = threading.Lock()


def _daily_active_schemes():
    """返回全部 status=active 且 frequency=daily 的方案配置（按 scheme_id 排序）。"""
    schemes = [
        cfg
        for cfg in discover_schemes()
        if cfg.status == "active" and cfg.frequency == "daily"
    ]
    schemes.sort(key=lambda cfg: cfg.scheme_id)
    return schemes


def _tally(summary: RunnerSummary, scheme_id: str, status: str, records: int, error) -> None:
    """线程安全地把单方案结果计入汇总。"""
    with summary._lock:
        summary.total += 1
        if status == "success":
            summary.success += 1
        elif status == "partial":
            summary.partial += 1
        elif status == "skipped":
            summary.skipped += 1
        else:
            summary.failed += 1
        summary.records_written += int(records or 0)
        summary.details.append((scheme_id, status, int(records or 0), error))


def _run_one(cfg, predict_date: str, algo_env: str, summary: RunnerSummary) -> str:
    """执行单个方案 gray_live；捕获异常不拖垮整批，返回最终 status 字符串。"""
    try:
        result = execute_scheme(
            cfg,
            predict_date,
            algo_env=algo_env,
            prediction_phase=PREDICTION_PHASE,
        )
    except Exception as exc:  # noqa: BLE001 — 单方案失败隔离，整批继续
        logger.exception("scheme %s raised during gray_live run", cfg.scheme_id)
        _tally(summary, cfg.scheme_id, "failed", 0, str(exc))
        return "failed"
    _tally(summary, cfg.scheme_id, result.status, result.records_written, result.error_msg)
    logger.info(
        "[gray_live] %s -> %s (records=%s)",
        cfg.scheme_id,
        result.status,
        result.records_written,
    )
    return result.status


def run(
    predict_date: str,
    *,
    algo_env: str = DEFAULT_ALGO_ENV,
    only: set[str] | None = None,
    max_heavy: int = DEFAULT_MAX_HEAVY,
    light_concurrency: int = DEFAULT_LIGHT_CONCURRENCY,
) -> RunnerSummary:
    """对 predict_date 执行全部 active 日频方案的 gray_live 出信号（并发）。

    非交易日直接返回。重方案（HEAVY_SCHEMES）限并发 max_heavy 且优先启动；
    轻方案以 light_concurrency 并发跑；Liwei consumer 仅在其 publisher success 后提交。
    only 非空时仅执行指定 scheme_id 子集（验证用）。
    """
    engine = create_engine_from_env()
    try:
        trading = is_trading_day(engine, predict_date)
    finally:
        engine.dispose()

    summary = RunnerSummary(predict_date=predict_date, is_trading_day=trading)
    if not trading:
        logger.info("%s 非交易日，跳过 gray_live 出信号", predict_date)
        return summary

    schemes = _daily_active_schemes()
    if only:
        schemes = [cfg for cfg in schemes if cfg.scheme_id in only]
        missing = only - {cfg.scheme_id for cfg in schemes}
        if missing:
            logger.warning("--only 指定了非 active-daily 方案，已忽略: %s", sorted(missing))

    by_id = {cfg.scheme_id: cfg for cfg in schemes}
    # publisher 完成状态（scheme_id -> status），供 consumer 依赖判定。
    pub_status: dict[str, str] = {}
    pub_lock = threading.Lock()
    pub_done = threading.Condition(pub_lock)

    heavy = [c for c in schemes if _is_heavy(c.scheme_id)]
    light = [c for c in schemes if not _is_heavy(c.scheme_id)]

    def heavy_task(cfg) -> None:
        status = _run_one(cfg, predict_date, algo_env, summary)
        # 若该重方案是某 consumer 的 publisher，登记状态并唤醒等待者。
        with pub_done:
            pub_status[cfg.scheme_id] = status
            pub_done.notify_all()

    def light_task(cfg) -> None:
        publisher = LIWEI_CONSUMER_DEPENDENCIES.get(cfg.scheme_id)
        if publisher is not None:
            # consumer：等待其 publisher 完成（仅当 publisher 也在本次运行集合内）。
            if publisher in by_id:
                with pub_done:
                    while publisher not in pub_status:
                        pub_done.wait(timeout=5)
                    pub_ok = pub_status.get(publisher) == "success"
            else:
                pub_ok = False
            if not pub_ok:
                reason = (
                    f"skipped: publisher {publisher} status="
                    f"{pub_status.get(publisher, 'absent')}"
                )
                logger.warning("[gray_live] %s %s", cfg.scheme_id, reason)
                _tally(summary, cfg.scheme_id, "skipped", 0, reason)
                return
        _run_one(cfg, predict_date, algo_env, summary)

    # 重方案与轻方案分池并发，同时进行；重方案受 max_heavy 限流避免 CPU 抢占。
    with ThreadPoolExecutor(max_workers=max(1, max_heavy)) as heavy_pool, \
            ThreadPoolExecutor(max_workers=max(1, light_concurrency)) as light_pool:
        heavy_futures = [heavy_pool.submit(heavy_task, c) for c in heavy]
        light_futures = [light_pool.submit(light_task, c) for c in light]
        for fut in heavy_futures + light_futures:
            fut.result()

    return summary


def _print_summary(summary: RunnerSummary) -> None:
    """打印人类可读的运行汇总。"""
    print(
        f"predict_date={summary.predict_date} trading_day={summary.is_trading_day} "
        f"total={summary.total} success={summary.success} partial={summary.partial} "
        f"failed={summary.failed} skipped={summary.skipped} "
        f"records_written={summary.records_written}"
    )
    for scheme_id, status, records, error in summary.details or []:
        line = f"  {scheme_id}: {status} (records={records})"
        if error:
            line += f" — {error}"
        print(line)


def main() -> int:
    """CLI 入口：默认对 today 出信号，支持 --predict-date 覆盖。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="每日 gray_live 出信号：全部 active 日频方案各出一次信号。"
    )
    parser.add_argument(
        "--predict-date",
        default=date.today().isoformat(),
        help="信号发出日 YYYY-MM-DD（默认今天）",
    )
    parser.add_argument("--algo-env", default=DEFAULT_ALGO_ENV)
    parser.add_argument(
        "--only",
        default=None,
        help="逗号分隔的 scheme_id 子集，仅执行这些方案（验证用）",
    )
    parser.add_argument(
        "--max-heavy",
        type=int,
        default=DEFAULT_MAX_HEAVY,
        help=f"重方案最大并发数（默认 {DEFAULT_MAX_HEAVY}；32核/10workers≈3）",
    )
    parser.add_argument(
        "--light-concurrency",
        type=int,
        default=DEFAULT_LIGHT_CONCURRENCY,
        help=f"轻方案并发数（默认 {DEFAULT_LIGHT_CONCURRENCY}）",
    )
    args = parser.parse_args()

    only = (
        {s.strip() for s in args.only.split(",") if s.strip()}
        if args.only
        else None
    )
    summary = run(
        args.predict_date,
        algo_env=args.algo_env,
        only=only,
        max_heavy=args.max_heavy,
        light_concurrency=args.light_concurrency,
    )
    _print_summary(summary)

    if not summary.is_trading_day:
        return 0
    # 有失败即以非零退出，便于 launchd/日志识别不完整。
    return 0 if (summary.failed == 0 and summary.skipped == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())

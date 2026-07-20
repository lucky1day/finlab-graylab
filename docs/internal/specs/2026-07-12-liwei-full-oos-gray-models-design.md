# Liwei Full-OOS 灰度新模型设计

> **文档状态：HISTORICAL。** 本文是内部设计记录，不是当前操作 SOP；当前入口见 [文档中心](../../README.md)。

**目标**: 在不覆盖现有 `10y01` 和 `5y1` 的前提下，将两个原始连续 full-OOS 方案作为独立新模型接入 Bond Factor Lab，保持工作日 07:03 业务调度，并在因子实验室中与旧模型并排展示。

## 范围

新增两个 base scheme：

- `liwei_0616_10y01_full_oos_k3_div_k10`
- `liwei_0616_5y01_full_oos_k3_div_k10`

现有以下方案不修改、不停用、不覆盖历史数据：

- `liwei_0616_10y01_cons_say_k3_div_k10`
- `liwei_0616_cons_sda_k3_div_k10`

新方案复用已验证的 source core、baseline 配置、共识阈值和 streak-break 参数，只改变方案身份与 source-original 测试序列。前端不新增专用页面，继续通过现有 Factor Lab registry、historical backtest 和 live metrics 接口展示。

## 算法语义

两个新模型统一使用连续 full-OOS 测试序列：

```text
test_ranges = (("2024-01-01", source_end),)
```

日频 live 时：

```text
predict_date = scheduler 运行日
feature_date = predict_date 前一交易日
source_end = feature_date
current_start = feature_date
current_end = feature_date
target_date = feature_date 后第 5 个交易日
```

历史回测按 `target_date` 月份分组。每组使用该目标月最后一个目标交易日作为 `source_end`，完整运行 `2024-01-01..source_end`，再抽取该组所需 feature dates。准确率月份归属始终使用 `target_date`，不使用 feature 月份。

10Y 新模型沿用 `STD + ACCWT + V55_7Y` 的 `k=3` 共识和 `DIV_K=10` fallback。5Y 新模型沿用 `STD + DIV + ACCWT` 的 `k=3` 共识和 `DIV_K=10` fallback。

## 代码结构

每个新方案建立独立 scheme 目录，包含：

- `config.yaml`: 新 identity、07:03 cron、3600 秒 timeout、backtest runner 和展示文案。
- `predict.py`: 平台日期转换、输入 artifact、PredictionRecord 和 full-OOS 审计字段。
- `inference.py`: full-OOS window、增量 Phase A cache 和 source core 调用。
- `benchmarks/`: source-original/current 对比证据与样本。

每个新方案在接入时拥有一份与已验证 source core 字节一致的本地副本。平台 StaticGate 禁止独立 scheme 跨方案导入 core，并要求 scheme 的 code hash 和生命周期自包含；因此复制 core 是独立注册的必要边界。复制后的 core 不做行为修改，只有新 adapter 负责选择连续 full-OOS 序列。

新建独立 backtest runner，负责写入新 scheme identity，并采用与 10Y02 已验证实现一致的 target-month full-OOS batching。共享 helper 仅在能保持两个 tenor 返回结构差异清晰时抽取；否则保留小型 adapter，避免改动旧模型。

## 调度设计

两个新模型业务 cron 均保持：

```yaml
schedule:
  cron: "3 7 * * 1-5"
  timezone: "Asia/Shanghai"
  timeout_sec: 3600
```

生产 scheduler 继续使用：

- `BOND_SCHEDULER_STAGGER_MINUTES=2`
- `BOND_SCHEDULER_PREDICTION_MAX_CONCURRENCY=1`
- `BOND_SCHEDULER_STARTUP_CATCHUP=1`

不提高并发，不建立第二套 scheduler。新 10Y full-OOS 与现有 10Y02 使用相同 10Y Phase A cache family；按稳定 scheme_id 顺序执行时，先运行的新 10Y 模型可以填充缓存，后续 10Y02 复用。新 5Y full-OOS 使用现有 5Y cache family增量扩展。首次冷启动可能接近 40 分钟，因此两个新方案均显式设置 3600 秒执行预算。

07:03 是业务基准时间，实际进程启动时间由错峰和全局单进程队列决定。该物理延后不改变 `predict_date`、`feature_date` 或 `target_date`。

## 灰度实验室展示

新模型按现有 onboarding 生命周期接入：

1. 以 `paused` 配置完成 Static、Unit、Dry-run、Compare、Backtest 和 API readiness 验证。
2. source-original 与 current historical backtest 对齐后，授权持久化 `framework_db_aligned` latest backtest。
3. 通过 ActivationGate 将新 scheme version 和 composite registry row 激活。
4. 对切换日之前的预测不做事后覆盖；如需要首条灰度样本，使用显式 `prediction_phase=gray_live` 的 LiveGate 写入一个已授权交易日。
5. 后续 07:03 scheduler 产生 `scheduled_live` 行。

Factor Lab 依靠 active composite registry row 显示候选模型。历史区域来自 `/api/backtests/factor-lab`，灰度/实盘区域来自 `/api/metrics/{composite_scheme_id}`。因此无需修改前端代码，但必须完成 backtest persist、activation、registry 同步和至少一条 live/gray evidence 验收。

## 版本与审计

新模型使用独立 model version：

- `liwei_0616_10y01_full_oos_v1`
- `liwei_0616_5y01_full_oos_v1`

PredictionRecord `extra` 至少记录：

- `model_scope=source_original_full_oos`
- `model_test_ranges=[["2024-01-01", source_end]]`
- `model_source_end`
- `model_current_start` / `model_current_end`
- Phase A cache status、watermark 和 fingerprint
- 输入 artifact 路径与 hash

新旧 scheme_id、scheme_version、model_version 和 DB run 独立，不能复用旧方案的 registry row 或覆盖旧 prediction。

## 失败处理

- full-OOS 超过 3600 秒时，由 executor 终止整个进程组并记录 failed run，不缩短测试序列。
- 输入源未更新到上一交易日时，live 日期校验 fail-closed，不复用旧 feature_date。
- cache identity、输入前缀或 payload 无效时，cache 自动隔离并冷重建，不复用不可信结果。
- activation 前任何 protected table 写入必须通过 harness authorization；普通 dry-run 和 compare 保持只读。
- scheduler 重启后的 startup catch-up 只补当天无终态 run 的方案，不重复覆盖已有运行。

## 验证标准

- 两个新配置可被 strict discovery 加载，旧方案配置与预测不变。
- window 单测确认唯一 test range 为 `2024-01-01..source_end`。
- live adapter 单测确认工作日 T+5 日期语义和 3600 秒 timeout。
- source comparison 在选定样本上方向、label、vote score 和 baseline score 对齐。
- historical backtest 每月按 target date 分组，汇总可由明细复算。
- active registry 中出现两个新 composite scheme，cron/timezone 与配置一致。
- `/api/backtests/factor-lab` 和 `/api/metrics/...` 可读取新方案，前端候选排行可见。
- scheduler 重启后日志出现两个新方案的注册记录，首次实际耗时小于 3600 秒。

# 2026-07-29 日频信号补齐与缓存性能交接

**文档状态**：`CURRENT`

**冻结时间**：2026-07-29 11:13 CST

**用途**：记录 2026-07-28 日频信号补齐的实际进度、未完成项、生产服务状态和 Liwei 缓存性能证据，供后续接手者继续处理。本文只记录事实，不构成新的调度或算法规范。

## 1. Git 与执行边界

- 仓库：`/Users/macstudio0/bond-factor-lab`
- 分支：`codex/audit-bugfixes-20260613`
- HEAD：`d10b9eb`
- 工作区在本文修改前为 clean。
- 未合并 `master`、未推送、未部署。
- 未修改、停止或迁移 BondProjectPro。
- 本轮生产补齐统一使用 `gray_live`；没有倒签 `scheduled_live`。
- 2026-07-29 信号未执行；禁止用 2026-07-28 DataBridge current 作为 2026-07-29 的旧数据 fallback。

与本次缓存和 DataBridge 诊断直接相关的开发提交：

| Commit | 内容 |
|---|---|
| `65f5c97` | 使用 Liwei effective cache inputs |
| `bd67e0f` | 保留 Liwei cache qualification identity |
| `ba0e848` | 记录 Liwei legacy cache fallback |
| `a4a9940` | 增加 daily policy v2 replay preflight |
| `bf2b914` | 记录 Liwei cache replay 结果 |
| `a894549` | 将 DataBridge 连续性校验限制在 DB 权威 cutoff 内 |
| `1255802` | 将 DataBridge 连续性权威绑定到 exact current |
| `d10b9eb` | 加固 DataBridge current 恢复权威 |

DataBridge 三个提交的六文件针对性回归为 `284 passed、61 subtests passed`。之后发现但未进入本轮关键路径的并发恢复加固已停止，未提交修改已全部撤销。

## 2. 当前生产服务状态

截至冻结时间：

| 服务/进程 | 状态 |
|---|---|
| BFL backend（同时提供静态前端和 API） | 已加载；`http://127.0.0.1:8100/` 返回 200 |
| BFL scheduler | 未加载 |
| BFL v2-preflight | 未加载 |
| 临时 Liwei operator launchd jobs | 已删除 |
| `scheduler.executor` / `scheduler.scheme_runner` | 无残留进程 |
| DataBridge refresh | 无运行中进程 |

因此当前只能确认前端可用，**不能**确认生产 scheduler 已恢复或日频自动调度已正常运行。缓存性能和 DataBridge 前置条件未解决前，不应把 scheduler 简单拉起后表述为“生产调度正常”。

## 3. 2026-07-28 日频结果

权威口径为 active Registry 的 29 个日频 target。按
`base_scheme_id + target_tenor + horizon + predict_date=2026-07-28`
只读核验：

- 应有：29
- 已有：12
- 缺失：17

### 3.1 已有 12 个 target

此前已存在：

- `daily_10y_lgbm_10y04_0629 / 10Y`
- `daily_1y_xgb_1y13_0629 / 1Y`
- `daily_5y_lgbm_5y10_0629 / 5Y`
- `daily_5y_2_v28 / 5Y`
- `daily_7y_1_v28 / 7Y`
- `liwei_0616_10y01_cons_say_k3_div_k10 / 10Y`

本轮已经补齐：

- run `1437`：`t1_daily / 5Y、10Y`，`gray_live`
- run `1442`：`t5_daily / 3Y、5Y、7Y、10Y`，`gray_live`
  - 状态：`success`
  - 写入：4/4
  - 算法执行耗时：约 22.10 秒

### 3.2 剩余 8 个 Blackbox V2

- `one_y_t5_liq_excess_a_v1 / 1Y`
- `one_y_t5_liq_excess_a_w252_l7_v1 / 1Y`
- `one_y_t5_liq_excess_a_w350_l7_v1 / 1Y`
- `one_y_t5_liq_excess_b_w252_l7_v1 / 1Y`
- `ten_y_t5_maj3_k3_ic_static_v1 / 10Y`
- `ten_y_t5_maj4_k3_ic_static_v1 / 10Y`
- `ten_y_t5_maj4_k3_ic_yearly_v1 / 10Y`
- `ten_y_t5_say_k5_sharpe_static_v1 / 10Y`

当前 DataBridge current 与 `predict_date=2026-07-28` 匹配，可在重新核验 exact version、latest passed all-stage run、授权 token 和目标业务键仍缺失后，通过现有受控 Blackbox live Gate 写 `gray_live`。不要直接调用 repository，也不要写 `scheduled_live`。

冻结时查到的 latest passed all-stage run：

| Scheme | Version | Harness run |
|---|---|---|
| `one_y_t5_liq_excess_a_v1` | `8d583560c9f1` | `hr_20260721T053643Z_64d115035d10` |
| `one_y_t5_liq_excess_a_w252_l7_v1` | `103c93bbc913` | `hr_20260721T053723Z_68f0af6321c4` |
| `one_y_t5_liq_excess_a_w350_l7_v1` | `86b458c568a5` | `hr_20260721T053800Z_51fd9b61c1cd` |
| `one_y_t5_liq_excess_b_w252_l7_v1` | `ba00891cd179` | `hr_20260721T053840Z_2d65d6136c61` |
| `ten_y_t5_maj3_k3_ic_static_v1` | `c54b90bcafa7` | `hr_20260728T101142Z_1681f3911d77` |
| `ten_y_t5_maj4_k3_ic_static_v1` | `6bdabf86b4a6` | `hr_20260728T101249Z_052552fedbbd` |
| `ten_y_t5_maj4_k3_ic_yearly_v1` | `af04567a19c3` | `hr_20260728T101329Z_9bfc9584ea2c` |
| `ten_y_t5_say_k5_sharpe_static_v1` | `e8137af4b655` | `hr_20260726T134918Z_b71762de0a2f` |

这些身份必须在实际写入前重新读取，不得把本文快照当成长期授权。

### 3.3 剩余 9 个 Liwei Native

- `liwei_0616_10y01_full_oos_k3_div_k10 / 10Y`
- `liwei_0616_10y02_cons_say_k3_div_k5 / 10Y`
- `liwei_0616_5y01_full_oos_k3_div_k10 / 5Y`
- `liwei_0616_5y_auc_static_all_k3_div_k10 / 5Y`
- `liwei_0616_5y_auc_yearly_all_k3_div_k10 / 5Y`
- `liwei_0616_5y_ic_yearly_all_k3_div_k10 / 5Y`
- `liwei_0616_7y01_cons_say_k3_div_k10 / 7Y`
- `liwei_0616_7y03_cons_all_k3_div_k8 / 7Y`
- `liwei_0616_cons_sda_k3_div_k10 / 5Y`

## 4. Liwei 缓存性能现场证据

生产 cache root：

`/Users/macstudio0/bond-factor-lab/backtest_artifacts/runtime_cache/liwei_0616`

冻结时存在 7 个 current cache family，总占用此前核验约 397 MiB：

- `liwei_0616_10y_v61 / 10y`
- `liwei_0616_5y_v31 / 5y`
- `liwei_0616_5y_allk10_auc_static_v1 / 5y`
- `liwei_0616_5y_allk10_auc_yearly_v1 / 5y`
- `liwei_0616_5y_allk10_ic_yearly_v1 / 5y`
- `liwei_0616_7y01_v31 / 7y`
- `liwei_0616_7y03_v31 / 7y`

本轮分别运行：

- `liwei_0616_10y01_full_oos_k3_div_k10`
- `liwei_0616_5y01_full_oos_k3_div_k10`

实际观察：

- 每个方案都启动了 10 个 `multiprocessing.spawn` 训练 worker；
- 两个方案并行时合计约 20 个持续满核 worker；
- 主进程 RSS 约 1.3–1.5 GiB/方案，worker 另有独立 RSS；
- 持续运行约 12 分 38 秒仍未返回 PredictionRecord；
- cache current pointer 在终止前没有切换；
- 接手请求后已终止两个精确进程组，未产生 prediction。

结论：

1. 当前 legacy 实际执行没有证明命中“几分钟级直接复用”路径。
2. 训练 worker 被创建，说明 `prepare_phase_a_caches()` 进入了 `train_missing`，不是纯 cache hit。
3. 仅凭现有 cache 文件存在，不能宣称生产预测已使用缓存。
4. 当前证据尚不足以断言唯一根因。优先检查：
   - 当前输入与 cache manifest `input_state` 的 `input_change/build_reason`；
   - 是否因日/周/月输入 revision 或 append 被判为 full/suffix build；
   - legacy 入口没有 Ledger 的 trusted cache qualification 时，是否触发运行期 CompareGate 或重新资格化；
   - publisher/consumer 是否正确共用同一个 cache family current。

建议先在 no-persist/隔离输出中打印或采集一次 build decision，再改代码；不要再次用生产写入任务做盲测。

## 5. 本轮失败 run 与原子性

本轮产生但没有 prediction 的失败 run：

- `1443、1444`：交互会话被中断后，孤儿进程组被终止；
- `1445–1452`：临时 launchd operator 缺少工作目录/PATH，均在约 0.02 秒内 fail-closed；
- `1453、1454`：正确启动并进入训练，接手请求后终止进程组。

上述 run 全部为 `failed`、`records_written=0`。当前没有 running run、算法子进程或部分 prediction。失败 run 保留作为审计证据，不应在未完成独立确认前删除。

## 6. DataBridge 当前状态

合法 current：

- generation：`full-20260728-152206-d52ecec59422`
- refresh date：`2026-07-28`
- published at：`2026-07-28 15:22:06 +08:00`
- daily max：`2026-07-27`
- weekly max：`202630`
- monthly max：`202701`
- stability rounds：2

2026-07-29 刷新没有发布：

1. 第一轮下载后因旧 current 的未来周键 `202630` 在新源中消失而被连续性校验拒绝；原子 current 保持不变。
2. 修正 cutoff 语义后再次尝试，DataBridge API 返回 6 张必需行情表不可用。
3. 只读检查确认远端 DataBridge 访问的 natapp MySQL 地址为 connection refused。
4. 按用户指令，未重启或修复 natapp，未继续 2026-07-29 DataBridge。

最后失败 attempt：

- refresh date：`2026-07-29`
- finished at：`2026-07-29 10:35:46 +08:00`
- duration：约 10.17 秒
- current 未变化

## 7. 建议接手顺序

1. 保持 backend 在线，不启动批量补数。
2. 在 no-persist/隔离目录复现一个 Liwei publisher，记录 cache `input_change`、`build_mode`、`build_reason` 和实际 missing ranges。
3. 只修使 legacy/当前生产入口无法直接复用已验证 cache 的最小根因；不要改算法窗口、特征、模型或 fallback。
4. 验证同 cache family：
   - publisher 首次必要增量；
   - consumer 必须复用；
   - 第二次相同输入必须确定且在几分钟内完成。
5. 重新读取 2026-07-28 精确缺口，只运行仍缺失的 9 个 Liwei target。
6. 使用当前合法 2026-07-28 DataBridge 和受控 Gate 补 8 个 V2 `gray_live`。
7. 再次验收 29/29、无 duplicate/partial/running/orphan。
8. 只有在 DataBridge 前置条件和分钟级缓存路径明确后，再恢复 BFL scheduler/v2-preflight；恢复后必须检查无 crash-loop。
9. 2026-07-29 保持独立事项，不使用本文的 2026-07-28 generation fallback。

## 8. 明确未完成项

- 2026-07-28 仍缺 17/29。
- 2026-07-29 日频未运行。
- BFL scheduler 和 v2-preflight 尚未恢复。
- Liwei 单方案几分钟级目标未通过现场验证。
- 2026-07-29 DataBridge 未发布。
- 日频 25 execution / 29 target 自动生产 MVP 未完成。

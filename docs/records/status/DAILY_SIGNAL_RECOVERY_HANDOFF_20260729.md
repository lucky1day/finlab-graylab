# 2026-07-29 日频信号补齐与缓存性能交接

**文档状态**：`CURRENT`

**冻结时间**：2026-07-29 11:13 CST

**性能诊断更新时间**：2026-07-29 11:41 CST

**用途**：记录 2026-07-28 日频信号补齐的实际进度、未完成项、生产服务状态和 Liwei 缓存性能证据，供后续接手者继续处理。本文只记录事实，不构成新的调度或算法规范。

## 1. Git 与执行边界

- 仓库：`/Users/macstudio0/bond-factor-lab`
- 分支：`codex/audit-bugfixes-20260613`
- 本次性能诊断修改前 HEAD：`62ca846`
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

### 4.1 性能诊断结论

经过算法计算量、DataBridge/输入链路、执行时序和磁盘 cache manifest
四路只读交叉核验，当前根因已经可以收敛：

1. Liwei 的“一条最终信号”不是一次模型计算。方案先构建从 2010 年开始的完整
   日/周/月特征和 source-compatible OOS Phase-A 状态，再运行 Phase-B 信号、
   滚动准确率及 Phase-C ensemble，最后才选择当前 `feature_date` 的一行。
2. 本轮两个 publisher 没有命中正常 warm hit/append，而是进入了
   `build_mode=full`。直接触发条件是当前代码的 cache spec fingerprint 与磁盘
   current manifest 不一致，即 `spec_changed -> full`。
3. 即使忽略 fingerprint 差异，生产 7 个 cache family 的 current input state
   仍是 schema 2，缺少新代码要求的 `effective_auxiliary` projection；新代码会将
   该状态判为 `missing_from_parent -> full`。因此旧生产 current 不能无证明地直接
   升格为 schema 3 增量缓存。
4. 两次运行都在完整 generation 校验和原子切换 `current.json` 之前被终止，
   所以没有保留可供下一次复用的新 current。再次运行仍会看到旧 schema 2/旧
   fingerprint，并从 full rebuild 重新开始。

因此，本次十几分钟未返回结果的直接原因不是“每天生成一条信号本身很慢”，而是
**生产旧缓存尚未完成新 schema 的一次性安全 bootstrap，日增量执行退化成了历史
Phase-A 冷重建；冷重建又在发布 current 前被中断，导致重试从零开始。**

### 4.2 一条输出背后的算法计算量

以 `liwei_0616_10y01_full_oos_k3_div_k10` 为例：

- 参数 grid 为 256 个基础配置加 9 个 slow-path 配置，共 265 个；
- 需要 STD、ACCWT、V55_7Y 和 fallback DIV 四条 baseline；
- 各 baseline 合计 11 个 seed；
- 每个需要训练的日期最多约为 `265 * 11 = 2,915` 次 LightGBM fit；
- 若冷重建约 619 个历史日期，训练量级接近 180 万次 fit。

5Y full OOS 每个日期约 1,590 次 fit，其他 ALLK10/7Y family 也均是每个日期
数千次 fit 的量级。算法执行不是只训练最后一天的一棵模型。

即使 Phase-A warm hit，运行仍会重新完成全历史 normalize、周/月对齐、标签、
特征和固定 IC screening，并运行 Phase-B/Phase-C 后才取最后一行，所以正常
cache hit 也不应被预期为 O(1) 或一两秒完成。改变这些序列、窗口、selector 或
ensemble 属于源算法保真约束下的 L2 算法改动，不得用调参或删减历史计算来贴合
耗时目标。

### 4.3 Cache 决策证据

已对本轮实际启动的两个 publisher 做只读 fingerprint 对账：

| Cache family | 当前代码/策略 fingerprint | 磁盘 current fingerprint | 当前代码决策 |
|---|---|---|---|
| 10Y publisher | `4e8d2201...e354` | `36e6750...` | `spec_changed -> full` |
| 5Y publisher | `dd8d3507...f19` | `e57f3015...` | `spec_changed -> full` |

现有 generation 创建时间早于本轮 cache identity/projection 代码。近期提交
`bd67e0f` 移除了 `_spec_fingerprint` 中的 `publisher_consumer_id`，会改变
fingerprint；这足以解释当前代码与这两个磁盘 current 不一致，但现有证据不把
它外推为所有历史 fingerprint 差异的唯一来源。

另一个独立问题是旧 10Y shared current 曾被窄 consumer 从约 618 个日期缩到
42 个日期，导致 wide publisher 下一次所谓 append 实际需补约 578 个历史日期。
新代码已有 publisher-only 写入和 coverage monotonicity 保护，但生产 current
仍未完成按新规则的安全迁移。

### 4.4 中断、原子性与重复冷启动

cache generation 只有在所有 baseline 构建、校验和清理完成后才原子切换
`current.json`。该行为保证失败不会污染已发布缓存，但当前没有跨运行的
family/baseline 训练 checkpoint。

本轮实际循环为：

```text
旧 schema 2 / 旧 fingerprint current
  -> 判定 full
  -> 历史冷训练
  -> 在 current 切换前人工终止
  -> 旧 current 保持不变
  -> 下一次再次判定 full
```

run `1453、1454` 各运行约 798 秒后被人工终止；数据库里的
`duration_sec=0` 是审计收尾结果，实际壁钟时间应按
`started_at -> finished_at` 计算。Native executor 只有在完整
`module.run()` 返回后才写 prediction，所以中断前 `records_written=0`
符合当前原子执行设计。

### 4.5 历史耗时对照

只读运行记录和已有隔离验证给出的量级如下：

| 类型 | 已观察耗时 | 说明 |
|---|---:|---|
| `t1_daily` | 约 22.57 秒 | 成功，2 个 target |
| `t5_daily` | 约 22.10 秒 | 成功，4 个 target |
| Blackbox V2 gray 包装执行 | 通常约 10–13 秒/方案 | 算法 live 本体多为约 1–2 秒 |
| Liwei consumer/cache hit | 约 72–75 秒 | 仍含完整输入和 Phase-B/Phase-C |
| Liwei 5Y 历史冷构建 | 约 21–29 分钟 | 历史成功 run |
| Liwei 10Y full publisher | 约 36–38 分钟 | 历史成功 run |
| 全 family forced-cold 隔离 rehearsal | 约 95.8 分钟 | 功能完成但未达到容量/截止目标 |
| run `1453、1454` | 各约 13 分 18 秒 | 人工终止，不能当成完整耗时 |

这说明结果条数不是主要耗时变量。剩余 8 个 V2 也没有现场慢执行证据；其积压的
直接原因是尚未通过受控补数继续执行，且 scheduler/v2-preflight 当前未加载，
不是 V2 单方案性能。

### 4.6 次要性能与运维问题

- Native 每次仍会重读约 98 MiB frozen generation，并重建、完整写入、读回和
  哈希约 24.84 MiB 日/周/月 CSV；“增量”仅指 Liwei Phase-A missing dates，
  不代表输入按一行增量传递。
- 2026-07-28 scheduled artifact 已累计 130 个文件、约 1.128 GiB，说明多次
  恢复/重试重复落了完整历史输入；该数字不是单次运行输入量。
- Blackbox 也会逐方案完整解析/校验约 24 MiB DataBridge 三频文件并创建 runtime
  copy，但现有 V2 耗时证明它不是本次十几分钟级主瓶颈。
- run `1445–1452` 的约 0.02 秒失败是临时 launchd operator 找不到 `conda`，
  属于工作目录/PATH 配置问题，不是算法性能问题。
- 当前缺少 input artifact、projection、manifest decision、逐 baseline training、
  Phase-B、Phase-C 和 serialization 的结构化阶段计时。中断 run 无法精确分摊
  每一阶段耗时，这是需要补齐的可观测性缺口。
- 2026-07-28 DataBridge current 与本次补数日期匹配；2026-07-29 DataBridge
  刷新失败是独立事项，不是本次 2026-07-28 Liwei 冷构建的原因。

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

1. 保持 backend 在线，不启动 scheduler，也不再并行盲跑生产 Liwei publisher。
2. 对全部 7 个 cache family 做纯只读决策矩阵，记录：
   - 当前代码/策略 fingerprint；
   - current manifest fingerprint、input-state schema、watermark 和日期覆盖数；
   - `input_change`、`build_mode`、`build_reason`；
   - publisher/consumer 身份和 qualification 状态。
3. 在隔离 candidate cache root 中按 family 单独运行 publisher，完成必要的 schema 3
   安全 bootstrap；记录输入、projection、各 baseline、Phase-B/Phase-C 和序列化
   阶段耗时，不写 prediction。
4. 对同一输入立即运行第二次，必须证明：
   - `build_mode=hit`；
   - 不创建训练 worker；
   - consumer 只读且不切换 current；
   - 输出确定，内部字段和 source-compatible scope 继续通过 CompareGate。
5. 再使用下一交易日输入验证真正的 append/suffix，确认只补预期日期且 coverage
   不缩减。不能把 forced-cold rehearsal 成功表述成 warm-incremental 已验收。
6. 只有 candidate generation 完成 qualification/admission 后，才按受控方式接入
   生产 current；不得无证明地直接改 current pointer。
7. 重新读取 2026-07-28 精确缺口，只运行仍缺失的 9 个 Liwei target。
8. 使用当前合法 2026-07-28 DataBridge 和受控 Gate 补 8 个 V2 `gray_live`；
   V2 与 Liwei cache bootstrap 可分别管理，但都要在写入前重新核验 exact
   version、授权和目标业务键。
9. 再次验收 29/29、无 duplicate/partial/running/orphan。
10. 只有在 DataBridge 前置条件和 warm cache 路径完成现场验收后，再恢复 BFL
    scheduler/v2-preflight；恢复后必须检查无 crash-loop。
11. 2026-07-29 保持独立事项，不使用本文的 2026-07-28 generation fallback。

## 8. 明确未完成项

- 2026-07-28 仍缺 17/29。
- 2026-07-29 日频未运行。
- BFL scheduler 和 v2-preflight 尚未恢复。
- Liwei 慢执行的直接原因已经定位为旧生产 cache 向新 schema 迁移时触发 full
  rebuild，并在 current 发布前被中断；生产 schema 3 bootstrap 尚未完成。
- Liwei warm hit、下一交易日 append/suffix 和单方案几分钟级目标仍未通过生产
  现场验证。
- 当前 cache 构建缺少跨运行 checkpoint 和完整分阶段计时。
- 2026-07-29 DataBridge 未发布。
- 日频 25 execution / 29 target 自动生产 MVP 未完成。

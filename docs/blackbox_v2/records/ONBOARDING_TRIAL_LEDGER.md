# Bond Factor Lab Blackbox V2 接入规划与试验记录

**文档状态**：`HISTORICAL`

**目标读者**：平台入库、审计和复盘人员

**最后核验日期**：2026-07-20

**记录时区**：除明确标注 UTC 外，本文时间均为 `Asia/Shanghai`。
**文档性质**：追加式平台规划和试验台账，不是上游交付契约，也不是平台操作 SOP。
**维护规则**：只追加、不覆盖；每条结论必须带执行时间和时区。

权威文档：

- [平台 Blackbox V2 入库 SOP](../../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- [上游 Blackbox V2 交付 SOP](../../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- [双运行时架构与实现边界](../../architecture/BLACKBOX_V2_PLATFORM.md)
- [Blackbox V2 文档管理](../README.md)

本文只记录具体方案、generation、snapshot、Harness run、数据库核验和待整改事项。通用契约变更必须修改仓库权威 SOP；外发副本由仓库文件生成，不在仓库外独立维护。本文中的历史实测值不得反向改变通用契约。

## 1. 管理目标

平台继续维护一套 Registry、版本、Harness、日期语义、`PredictionRecord`、结果表、API 和前端链路。`runtime_type` 只选择交付检查、输入准备和算法执行驱动：

| 运行类型 | 上游交付 | 输入 | 执行方式 |
|---|---|---|---|
| `native_adapter` | 完整方案目录 | `legacy_db` | import adapter |
| `blackbox_v2` | 一个 `.py` 和一个 `.json` | `data_bridge_current` | sandbox CLI 子进程 |

Blackbox 方案使用独立 trial ID，不替换、不暂停、不修改现有原生方案。通用入库默认终态仍为 `shadow + paused`；具体方案只有在生产路径认证通过并取得专项授权后，才可进入 `active + gray_live`。专项授权不自动扩展到其他方案。

## 2. 平台准备状态

### 2.1 截至 2026-07-19 13:23 的能力快照

| 项目 | 状态 |
|---|---|
| 双运行时 | `native_adapter` 与 `blackbox_v2` 已显式发现和分派 |
| Blackbox profile | `blackbox-v2-v1` |
| conda 环境 | `forecast_env_blackbox_v1` |
| 环境指纹 | `720ad40ab77cd6c7156ff35a80cf3604ac3a6153425ed235a4e3158b0631f8bd` |
| 数据 Schema | `data-bridge-v1`：日 774、周 575、月 123 列 |
| 运行限制 | 8 线程、64 GiB、predict 3600 秒、backtest 14400 秒、单批 100 条、Output 50 MiB、日志 5 MiB |
| 隔离 | sandbox 禁止网络和数据目录写入；运行目录可写 |
| DataBridge | 每日 current、稳定性校验、原子发布和启动补刷已接通 |
| 输入链路 | current 到一次性只读 Snapshot、七字段 Request 已接通 |
| Harness | 七个自动 Gate 已接通 |
| Shadow | exact version/run 授权及 paused Registry 已接通 |

数据库迁移 `migrations/016_dual_runtime.sql` 已应用。2026-07-17 首次迁移核验时，原有 Registry 33 条、版本 114 条、运行记录 931 条均回填为 `native_adapter`；这些数量是历史核验值，不代表后续实时数量。

### 2.2 当前状态解释

- DataBridge 刷新成功只证明统一输入可用，不代表任何方案 active。
- Harness 七个 Gate 通过只证明技术契约验收通过，不代表已调度、API 可见或算法效果达标。
- `api-readiness` 当前是结构检查，不是独立 HTTP API、Registry 或 scheduler 探针。
- “业务表零新增”必须使用独立数据库查询证明，不能只引用 Gate evidence。

## 3. DataBridge 发布记录

### 3.1 2026-07-19 02:50 首次显式发布（历史）

> 历史验证记录，不代表当前 current。

| 项目 | 记录 |
|---|---|
| generation | `full-20260719-025042-3bcfec42fb44` |
| 业务摘要 | `3bcfec42fb44c26b122788aacea2f0e99ad20d4c44c39bc974ca151112a71864` |
| 稳定性 | 连续两轮一致 |
| 发布耗时 | 1195.423 秒 |
| 文件规模 | 日 3878×774、周 847×575、月 201×123 |
| 磁盘 | current 约 24 MB；发布过程文件组合上界约 48 MB |

错误凭据 dry-run 返回 HTTP 401/退出码 1，发布前后 current 组合摘要不变。四路并发实测会令后续全量轮次长期阻塞，因此本机生产并发校准为 1，上限仍为 4。

### 3.2 2026-07-19 05:30 自然调度发布（截至 2026-07-19 13:23 的 current）

| 项目 | 记录 |
|---|---|
| 调度开始 | `2026-07-19 05:30:00` |
| 发布完成 | `2026-07-19 05:50:02` |
| 对照 current generation | `full-20260719-055002-7876ee1e5ec9` |
| business digest | `7876ee1e5ec975034396e3ff33c96ce294bd2f516e8497b9e54afefd5cf5867b` |
| 稳定性 | 连续两轮一致 |
| 耗时 | 1202.605 秒 |
| 日频 | 3878×774，`2010-07-27` 至 `2026-07-17` |
| 周频 | 847×575，`201001` 至 `202628` |
| 月频 | 201×123，`201002` 至 `202701` |

三份 current SHA256：

| 文件 | SHA256 |
|---|---|
| `daily_output.csv` | `af432b37153a09f4aa47dd20eaf1d8596abad3e48f3d46b1a7e8fa4494e1f470` |
| `weekly_output.csv` | `9dfe8a8cfaae9fd4be1e70b872ff2d89d5839f4c266af3924eb0e0f5d4ebc4d5` |
| `monthly_output.csv` | `f29607a79c66860d4c43f369d99cb4fdfbba2cf665d3931f930f496bc21d86b5` |

该记录证明 APScheduler 的 `05:30` 自然触发已实际成功，不只是完成 job 注册。

## 4. 方案入库记录

### 4.1 记录 001A：首次两文件技术验收

**执行时间**：2026-07-17 16:06；**性质**：历史验证记录，不代表当前 current。

方案：`weekly_10y_lgbm_point_v1`

| 项目 | 实测结果 |
|---|---|
| 交付内容 | 一个 `.py` 和一个 `.json`，原样只读保存 |
| 脚本 SHA256 | `6e3ee104e9db652d0d3a291a13695c616ec605547693307bdfe61aa3fee45c72` |
| Metadata SHA256 | `e60e9237f2f02d973033030e52fa8744fc62e841d5f2073ea6f273ee431901be` |
| 任务 | `10Y + weekly_point + horizon=1` |
| Request | `predict_date=2026-07-11`、`feature_date=2026-07-10`、`target_date=2026-07-17` |
| Snapshot | `snapshot-97b77c845e76073a04ce427e` |
| 快照规模 | 日 3873×774、周 847×575、月 198×123 |
| 单点方向 | `-1` |
| Harness | `hr_20260717T080635Z_f056b3257f6f`，七 Gate 通过 |
| 版本 | `3769746b4014` validated；`a6f732329b9b` shadow |
| Registry | `weekly_10y_lgbm_point_v1__h1__10Y`，paused |

该轮确认脚本只读取 `weekly_output.csv` 也符合契约；平台仍提供完整三频快照。100 条回测、重复、分批、顺序和后续行隔离通过且不持久化。

### 4.2 记录 001B：首次 DataBridge current 复验

**执行时间**：2026-07-19 02:54；**Request predict_date**：2026-07-18。
**性质**：历史验证记录，不代表当前 current。

| 项目 | 实测结果 |
|---|---|
| generation | `full-20260719-025042-3bcfec42fb44` |
| Request | `feature_date=2026-07-17`、`target_date=2026-07-24` |
| 截止键 | `2026-07-17 / 202628 / 202608` |
| Snapshot | `snapshot-9f35bec6614728c225dc5d23` |
| 快照规模 | 日 3878×774、周 847×575、月 201×123 |
| 单点方向 | `1` |
| Harness | `hr_20260718T185456Z_1419af611e8c`，七 Gate 通过 |
| 回测 | 100/100，`persist=false` |

该轮曾发现 DataBridge 日键原始表示与 Request 日截止键格式不同；平台在 Compare 探针中规范化日键后定位，未修改上游脚本。这里的月截止键 `202608` 只属于该次历史 Request，不得当作最新复验结果。

### 4.3 记录 001C：自然刷新 generation 的统一输入复验

**执行时间**：2026-07-19 13:22 至 13:23。
**当前结论时点**：2026-07-19 13:23。

| 项目 | 实测结果 |
|---|---|
| generation | `full-20260719-055002-7876ee1e5ec9` |
| Snapshot | `snapshot-245c54a6363ed5251475e8f5` |
| Harness run | `hr_20260719T052252Z_3add9b3072cd` |
| Scheme version | `2110193568a9` |
| Request | `predict_date=2026-07-18`、`feature_date=2026-07-17`、`target_date=2026-07-24` |
| 截止键 | `2026-07-17 / 202628 / 202607` |
| 单点方向 | `1` |
| 七个 Gate | 全部 passed |
| 回测 | 100/100，`persist=false` |
| 临时快照 | 运行结束后已删除 |

Input Gate 三份文件 SHA256 与第 3.2 节 generation 的 current 文件逐一完全一致，因此本轮可以证明平台提供给算法的 Snapshot 内容与统一 current 相同。由于 Input 报告不记录 generation ID，且 sandbox 当前允许全局文件读取，该证据不能单独证明 generation 身份，也不能证明脚本绝无可能读取其他本地文件。

独立数据库副作用核验：

| 数据对象 | trial 记录数 |
|---|---:|
| `t_scheme_runs` | 0 |
| `t_scheme_predictions` | 0 |
| `t_backtest_runs` | 0 |
| `t_backtest_predictions` | 0 |

截至结论时点，方案仍为 `version_status=shadow`、Registry=`paused`，scheduler 不执行，active API/前端不可见。本轮只能表述为“统一输入链路下完成 shadow 技术验收”，不得表述为 active、正式上线或生产预测成功。

### 4.4 记录 001D：灰度实验室候选标记

**标记日期**：2026-07-19，`Asia/Shanghai`。

| 项目 | 标记内容 |
|---|---|
| 方案 | `weekly_10y_lgbm_point_v1` |
| 文档标记 | `GRAY_LAB_READY` |
| 允许范围 | 使用最新通过完整性校验的 DataBridge generation，执行 no-persist 单点预测、批量回测、重复性和成对对照实验 |
| 禁止范围 | `activate`、`live`、生产 scheduler、业务表写入、active API 或前端可见性 |
| 实际平台状态 | `version_status=shadow`、Registry=`paused`，保持不变 |

该标记表示现有技术证据足以安排一次灰度实验室测试，不表示测试已经执行或效果达标，也不等同于平台字段 `prediction_phase=gray_live`。实际测试启动前仍须重新确认当次 generation、Request、Snapshot 和业务表零写入基线。

### 4.5 记录 001E：持续回归基线第一轮

**执行时间**：2026-07-19 17:02:40 至 17:03:06，`Asia/Shanghai`。

| 项目 | 实测结果 |
|---|---|
| 灰度实验室标记 | `GRAY_LAB_READY` |
| generation | `full-20260719-055002-7876ee1e5ec9` |
| environment fingerprint | `720ad40ab77cd6c7156ff35a80cf3604ac3a6153425ed235a4e3158b0631f8bd` |
| Snapshot | `snapshot-245c54a6363ed5251475e8f5` |
| Harness run | `hr_20260719T090240Z_64d1d64d4def` |
| Scheme version | `2110193568a9` |
| Request | `predict_date=2026-07-18`、`feature_date=2026-07-17`、`target_date=2026-07-24` |
| 截止键 | `2026-07-17 / 202628 / 202607` |
| 单点方向 | `1` |
| 七个 Gate | 7/7 passed，控制面审计记录 7 条 |
| Compare | 重复、predict/backtest、分批、顺序和未来行隔离全部通过 |
| Backtest | 100/100，`persist=false` |
| Harness 自动段 | 约 26 秒 |
| 临时 Snapshot | 运行结束后已删除 |

独立前后查询结果保持不变：

| 数据对象 | 运行前 | 运行后 |
|---|---:|---:|
| `t_scheme_runs` | 0 | 0 |
| `t_scheme_predictions` | 0 | 0 |
| `t_scheme_run_log` | 0 | 0 |
| `t_backtest_runs` | 0 | 0 |
| `t_backtest_predictions` | 0 | 0 |

Registry 继续为 `weekly_10y_lgbm_point_v1__h1__10Y + paused`。本轮建立持续回归的首个效率和稳定性基线，不改变 `shadow + paused` 状态，也不证明算法效果达到业务门槛。

### 4.6 记录 001F：隔离全链路稳定性认证

**执行时间**：2026-07-19 17:25 至 18:00，`Asia/Shanghai`。

**完整报告**：[FULL_PIPELINE_STABILITY_AUDIT_20260719.md](FULL_PIPELINE_STABILITY_AUDIT_20260719.md)。

**机器证据**：[FULL_PIPELINE_STABILITY_AUDIT_20260719.evidence.json](FULL_PIPELINE_STABILITY_AUDIT_20260719.evidence.json)。

| 项目 | 实测结论 |
|---|---|
| 隔离环境 | 独立 worktree、独立 MySQL Schema、源表只读 View、临时后端端口 |
| Harness 稳定性 | 同一真实交付 10/10 轮、每轮 7/7 Gate 通过，P95 26 秒 |
| 批量回测 | 100/101/500/1000 条结果一致；持久化为确定性失败 |
| 正式激活 | ActivationGate 版本身份不一致，失败 |
| 下游兼容 | 仅在隔离库强制 active 后完成 gray_live、scheduler、actual、API 和前端验证 |
| 故障注入 | timeout、缺 CSV、网络、data-dir 写入正确阻断；任意文件读取和环境变量读取未阻断 |
| 最终评级 | `SHADOW_READY=PASS`；`PRODUCTION_READY=FAIL`；`PRODUCTION_BLOCKED` |

本轮未修改生产 trial，也未向生产业务表写入任何记录。生产 Registry 仍为 `paused`，version 仍为 `shadow`。强制 active 证据只说明下游技术兼容，不能用于签发生产授权。

### 4.7 记录 001G：真实生产灰度专项激活

**执行时间**：2026-07-20 10:53 至 11:07，`Asia/Shanghai`。

**完整记录**：[PRODUCTION_GRAY_ACTIVATION_20260720.md](PRODUCTION_GRAY_ACTIVATION_20260720.md)。

**机器证据**：[PRODUCTION_GRAY_ACTIVATION_20260720.evidence.json](PRODUCTION_GRAY_ACTIVATION_20260720.evidence.json)。

| 项目 | 生产实测结果 |
|---|---|
| 专项授权 | 用户明确批准 `weekly_10y_lgbm_point_v1` 激活为生产灰度测试方案 |
| scheme version | `0666a6989d6b` |
| Harness run | `hr_20260720T025353Z_b176dbf5eb3e`，7/7 Gate passed |
| generation / snapshot | `full-20260720-055026-00e12e3803a8` / `snapshot-fd8a1f8736d3a4d057fbd98e` |
| Shadow / Activation | 正式签名门禁通过；配置、版本和 Registry 统一为 active |
| 回测落库 | run `165`；100 predictions；24 monthly metrics；总体准确率 `50.0%` |
| gray live | run `955`；方向 `1`；精确新增 1 run、1 prediction、1 log |
| Request | `predict=2026-07-20`、`feature=2026-07-17`、`target=2026-07-24` |
| API | schemes、metrics、backtest HTTP 200；1 条 live 可见、actual pending |
| 前端 | 正确进入 `10Y + 周度` 格子，方案版本和灰度日期可见，控制台 0 error |
| scheduler | 已注册周六 11:32 的错峰任务 |
| 当前状态 | `PRODUCTION_GRAY_ACTIVE` |

正式 API Gate 的实例 fingerprint、Registry、live 和回测均匹配，但因目标日尚未到达，actual、月度 live 指标和 `metric_samples` 条件暂未通过。必须在 `2026-07-24` actual 刷新后复验；当前不得报告这条 live 的准确率。

本次只授权当前真实交付方案。平台总体仍为 `PRODUCTION_PATH_READY`，尚未达到面向所有新方案的 `PRODUCTION_READY`。

### 4.8 记录 002A：1Y T+5 四方案 Shadow 技术入库

**执行时间**：2026-07-20 16:39 至 17:19，`Asia/Shanghai`。

**完整记录**：[PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md](PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md)。

**机器证据**：[PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json](PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json)。

| 项目 | 实测结果 |
|---|---|
| 方案范围 | `one_y_t5_liq_excess_a_v1`、`one_y_t5_liq_excess_a_w252_l7_v1`、`one_y_t5_liq_excess_a_w350_l7_v1`、`one_y_t5_liq_excess_b_w252_l7_v1` |
| 交付 | 从 8 方案 ZIP 中仅拆分并 Intake 指定四组；另外四组未落地 |
| generation / snapshot | `full-20260720-055026-00e12e3803a8` / `snapshot-fd8a1f8736d3a4d057fbd98e` |
| 稳定性 | 四方案各 10/10 轮、每轮 7/7 Gate passed；P95 为 28/29/29/29 秒 |
| 平台整改 | 修复 CompareGate 未来日频行超出 pandas 范围及日期文本格式不一致；未修改交付文件 |
| Shadow | 四方案 lifecycle journal 均 `verified`；配置和 version 为 shadow，Registry 为 paused |
| 业务表 | 每方案正式 run、prediction、run log、backtest run 和明细均为 0 |
| API / scheduler | active API 不可见；scheduler 未重启、未挂载新任务 |
| 当前状态 | `SHADOW_READY`；尚未激活、持久化回测或写入 gray live |

本批取得的是四个具体方案的 Shadow 技术入库结论。下一检查点是 `one_y_t5_liq_excess_a_w252_l7_v1` Canary 的独立生产 Gate、持久化回测、gray live、API/前端与自然 scheduled-live 验收；在该检查点通过前不批量激活其余三个方案。

## 5. 已确认的通用迭代规则

1. 技术 Onboarding 可以使用最新通过完整性校验的 generation；scheduled-live 必须使用当日成功 generation，两者分开记录。
2. 算法可以只消费三频文件的一部分；平台仍提供完整快照并统一生成 `data_snapshot_id`。
3. Request 永远包含三个截止键；算法逐 Request 截断实际消费文件，不自行推导周/月键。
4. Schema 变化必须升级 `data_schema_version`，原方案不得静默继续运行。
5. 通用 Gate 只证明接口、复现性、截止隔离和平台结构兼容，不证明准确率或与原生方案等价。
6. 若业务方声明黑盒方案与某原生方案相同，平台另行记录 `reference_base_scheme_id`，使用同一 Snapshot 和 Request 做成对比较。
7. 具体方向、行数、耗时、generation、snapshot、run 和数据库数量只属于带时点的试验记录，不提升为通用契约。

## 6. 平台后续整改台账

| 优先级 | 整改项 | 状态 | 后续动作 |
|---|---|---|---|
| P0 | BBV2-01 至 BBV2-07：generation、profile、审计、生命周期、生产门禁、Result 和 sandbox | `CLOSED` | 按回归测试持续守护，不重新描述为待实现能力 |
| P0 | 当前 gray live 的 actual 与正式 API Gate | `PENDING` | `2026-07-24` actual 刷新后复验并追加记录 |
| P1 | 真实交付覆盖门槛 | `PENDING` | 再接入至少两个真实包并覆盖日、周、月三种频率 |
| P1 | 通用生产晋级 SOP | `BLOCKED` | 覆盖门槛完成且业务、平台、运维共同确认后转为 `CURRENT` |
| P2 | 原始失败证据保留策略 | `OPEN` | 明确保留范围、期限和脱敏规则 |

平台 SOP 必须区分“当前方案专项生产灰度授权”和“所有新方案通用生产授权”，不得把前者写成后者。

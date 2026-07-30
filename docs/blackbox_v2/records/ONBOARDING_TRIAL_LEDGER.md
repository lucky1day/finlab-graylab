# Bond Factor Lab Blackbox V2 接入规划与试验记录

**文档状态**：`HISTORICAL`

**目标读者**：平台入库、审计和复盘人员

**最后核验日期**：2026-07-27

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

### 4.9 记录 002B：1Y T+5 代表性 Canary 灰度激活

**执行时间**：2026-07-20 17:23 至 17:39，`Asia/Shanghai`。

**完整记录**：[PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md](PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md)。

**机器证据**：[PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json](PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json)。

| 项目 | 生产实测结果 |
|---|---|
| Canary | `one_y_t5_liq_excess_a_w252_l7_v1`，version `103c93bbc913` |
| 最新 Harness | `hr_20260720T092351Z_1b6e76e498c0`，7/7 Gate passed |
| Activation | config、exact version 和 composite Registry 统一 active |
| 历史口径整改 | 忽略样本窗口外的通用日历/债券观测旧差异；所选窗口缺口仍 fail-closed；54 项回归通过 |
| 回测落库 | run `166`；100 predictions；6 monthly metrics；总体准确率 `59.0%` |
| gray live | run `956`；方向 `1`；精确新增 1 run、1 prediction、1 log |
| Request | `predict=2026-07-20`、`feature=2026-07-17`、`target=2026-07-24` |
| API | 本地与公网只显示当前 Canary；metrics/backtest HTTP 200；actual pending |
| 前端 | `1Y + T+5` 格子显示 1 个候选，灰度 `--（0/0）`，控制台 0 error |
| 公网 | 只读 200/403 矩阵 14/14 通过 |
| 进程 | 只重启 backend；scheduler PID `52329` 保持不变 |
| 当前状态 | `CANARY_GRAY_ACTIVE`，等待下一交易日自然 `scheduled_live` |

其余三个选中方案仍为 `shadow + paused` 且业务表零写入；被排除四方案在目录、版本、Registry、业务表和 API 中均为零。只有下一交易日 Canary 自然调度通过后，才进入剩余三方案激活阶段。

### 4.10 记录 002C：1Y T+5 四方案专项全量灰度激活

**执行时间**：2026-07-20 18:41 至 18:52，`Asia/Shanghai`。

**完整记录**：[PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md](PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md)。

**机器证据**：[PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json](PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json)。

| 项目 | 生产实测结果 |
|---|---|
| 时序授权 | 用户明确要求不再等待 Canary 自然调度，直接激活同批全部四方案；scheduler 当天仍不重启 |
| 名称治理 | 上游 SOP 要求简洁 Metadata name；已有不可变交付由平台 `display_name` 覆盖，scheme version 不变 |
| 新增 all-stage | A、A_W350_L7、B_W252_L7 各 7/7 Gate passed，绑定当天 generation/snapshot/fingerprint |
| Activation | 四个 config、exact version、composite Registry 全部 active；Registry 名称为四个业务短名称 |
| 回测落库 | run `166..169`；每方案 100 predictions、6 monthly metrics；current snapshot as-of replay |
| gray live | run `956..959`；每方案精确 1 run、1 prediction、1 log，方向均为 `1` |
| Request | 四方案均为 `predict=2026-07-20`、`feature=2026-07-17`、`target=2026-07-24` |
| API / 前端 | `1Y + T+5` 恰好四个短名称候选；被排除方案不可见；控制台 0 error |
| 公网 | 只读/拒绝 200/403 矩阵 14/14 通过 |
| 进程 | backend 重启为 PID `23395`；scheduler PID `52329` 保持不变，无 startup catchup |
| 当前状态 | 四方案 `GRAY_ACTIVE`；自然 `scheduled_live` 和 actual 仍待时点复验 |

本次授权只覆盖用户明确列出的四个方案，不改变新 Blackbox 方案必须逐方案通过生产准备与专项授权的通用边界。

### 4.11 记录 002D：1Y T+5 四方案完整历史刷新

**执行时间**：2026-07-20 19:41 至 20:22，`Asia/Shanghai`。

**完整记录**：[PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md](PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md#9-完整历史区间刷新)。

**机器证据**：[PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json](PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json)。

| 项目 | 生产实测结果 |
|---|---|
| 平台升级 | 完整 persist 由日期区间定义；默认起点 `2025-01-01`；单批 100 不再被解释为完整总量 |
| 授权 | 四个新 token 分别绑定起点、截止日、exact version 和 latest all-stage run，未跨方案复用；缺失截止日 fail-closed |
| active recertification | ReadinessGate 区分 pre-shadow 与 active；四个 fresh all-stage 均 7/7 passed |
| 分批 | 每方案 367 个 HistoricalCase，固定分为 `100/100/100/67` 四批，共享一个总预算 |
| 原子落库 | canonical run `174..177`；每方案 367 predictions、19 monthly metrics；先前 run `166..173` 不修改不删除 |
| durable summary | 每个 canonical run 均持久化授权起止、实际 predict/target 范围、请求数、批次、总预算、最大/实际子进程数及 replay 语义 |
| 日期 | predict/feature `2025-01-02..2026-07-10`；target `2025-01-09..2026-07-17` |
| 前端 | `1Y + T+5` 四个短名称候选各显示 367；2025-01 有 13 条；同月回测和 gray live 均保留；控制台 0 error |
| 公网 | 页面、health、schemes、backtests 和四个 metrics 为 200；默认 API、predictions、admin、trigger 为 403 |
| 回归 | 全量 `unittest` 1120/1120 通过；包含缺失截止日、恶意空截止日 token 和 durable summary 覆盖 |
| 进程 | 只重启 backend 为 PID `16726`；scheduler PID `52329` 保持不变，无 scheduled-live 增量 |
| 当前状态 | 四方案继续 `GRAY_ACTIVE`；等待自然 `scheduled_live` 和 2026-07-24 actual |

该记录证明 Contract 单批 100 是调用边界而非完整回测上限。完整历史仍是 current snapshot as-of replay，不得描述为 historical vintage PIT。

### 4.12 记录 003A：10Y T+5 方案 1 技术 Gate

**执行时间**：2026-07-26 15:20 至 15:21，`Asia/Shanghai`。

**机器证据**：[TECHNICAL_ONBOARDING_10Y_T5_4SCHEMES_20260726.evidence.json](TECHNICAL_ONBOARDING_10Y_T5_4SCHEMES_20260726.evidence.json)。

| 项目 | 实测结果 |
|---|---|
| 方案 | `ten_y_t5_maj3_k3_ic_static_v1` |
| 过渡边界 | 本包在 `description` 新政策生效前已接收；缺少 `description` 且 `name` 重复任务格子信息，按用户明确授权继续技术 Gate，状态保持待上游修订 |
| 原始摘要 | Python `75749f165e3ce2c5cb70f86fae1336e52e693655198b05c78e45422e165471de`；Metadata `10c41c6d3e271e76c6c03afc4e9ff3ad998ffefb92b557d5bb868d69e077329d`；Intake 后逐字节摘要一致 |
| scheme version | `c54b90bcafa7` |
| Harness | `hr_20260726T072056Z_0d6e33033ba3`，`static/input/unit/dry-run/compare/backtest/api-readiness` 7/7 passed |
| generation / snapshot | `full-20260724-062251-4977e502dadf` / `snapshot-46ff3231de2c4a080c46ba56` |
| Request | `predict=2026-07-24`、`feature=2026-07-23`、`target=2026-07-30` |
| no-persist backtest | 100 requests / 100 records；分批、变序一致；`persist=false` |
| 结构 readiness | composite ID `ten_y_t5_maj3_k3_ic_static_v1__h5__10Y`；`pre_shadow + paused`；scheduler/API 预期均不可见 |
| 数据库边界 | Harness 使用已验证仅能读取九张源表的 `bfl_source_readonly`；未写 Harness 审计、Registry、run、prediction、backtest 或 gray-live 表 |
| Registry 检查范围 | 仓库与 Git 全历史无同 ID；本机 active `/api/schemes` 无冲突；生产控制面 Registry SQL 因只读账号无权限未执行 |
| 当前状态 | `TECHNICAL_GATES_PASSED_DESCRIPTION_PENDING`；不是 `SHADOW_READY`，未激活、未登记 Registry、未持久化回测、未写实盘 |

上游补充 `description` 和简洁 `name` 后，Metadata 摘要与 canonical
`scheme_version` 必须变化；该方案届时按新交付版本重新执行七个 Gate，不直接覆盖并沿用本次结论。

### 4.13 记录 003B：10Y T+5 方案 2 技术 Gate

**执行时间**：2026-07-26 15:23 至 15:24，`Asia/Shanghai`。

**机器证据**：[TECHNICAL_ONBOARDING_10Y_T5_4SCHEMES_20260726.evidence.json](TECHNICAL_ONBOARDING_10Y_T5_4SCHEMES_20260726.evidence.json)。

| 项目 | 实测结果 |
|---|---|
| 方案 | `ten_y_t5_maj4_k3_ic_static_v1` |
| 过渡边界 | 本包在 `description` 新政策生效前已接收；缺少 `description` 且 `name` 重复任务格子信息，按用户明确授权继续技术 Gate，状态保持待上游修订 |
| 原始摘要 | Python `64000f9a4521dfdf8da04792e8b083bf12cd0837870b7714b27725d4dcbc955b`；Metadata `eee777b89f0a9138dce0454044e0569f91459c28f0f5426d8a7d5c06202da005`；Intake 后逐字节摘要一致 |
| scheme version | `6bdabf86b4a6` |
| Harness | `hr_20260726T072359Z_611676555f0d`，`static/input/unit/dry-run/compare/backtest/api-readiness` 7/7 passed |
| generation / snapshot | `full-20260724-062251-4977e502dadf` / `snapshot-46ff3231de2c4a080c46ba56` |
| Request | `predict=2026-07-24`、`feature=2026-07-23`、`target=2026-07-30` |
| no-persist backtest | 100 requests / 100 records；分批、变序一致；`persist=false` |
| 结构 readiness | composite ID `ten_y_t5_maj4_k3_ic_static_v1__h5__10Y`；`pre_shadow + paused`；scheduler/API 预期均不可见 |
| 数据库边界 | Harness 使用已验证仅能读取九张源表的 `bfl_source_readonly`；未写 Harness 审计、Registry、run、prediction、backtest 或 gray-live 表 |
| Registry 检查范围 | 仓库与 Git 全历史无同 ID；本机 active `/api/schemes` 无冲突；生产控制面 Registry SQL 因只读账号无权限未执行 |
| 当前状态 | `TECHNICAL_GATES_PASSED_DESCRIPTION_PENDING`；不是 `SHADOW_READY`，未激活、未登记 Registry、未持久化回测、未写实盘 |

上游补充 `description` 和简洁 `name` 后，Metadata 摘要与 canonical
`scheme_version` 必须变化；该方案届时按新交付版本重新执行七个 Gate，不直接覆盖并沿用本次结论。

### 4.14 记录 003C：10Y T+5 方案 3 技术 Gate

**执行时间**：2026-07-26 15:25 至 15:26，`Asia/Shanghai`。

**机器证据**：[TECHNICAL_ONBOARDING_10Y_T5_4SCHEMES_20260726.evidence.json](TECHNICAL_ONBOARDING_10Y_T5_4SCHEMES_20260726.evidence.json)。

| 项目 | 实测结果 |
|---|---|
| 方案 | `ten_y_t5_maj4_k3_ic_yearly_v1` |
| 过渡边界 | 本包在 `description` 新政策生效前已接收；缺少 `description` 且 `name` 重复任务格子信息，按用户明确授权继续技术 Gate，状态保持待上游修订 |
| 原始摘要 | Python `7e55fae577b3a14085fdba98af638e449c11b935db373a6176238d72afea3781`；Metadata `be7b6950d329ce058726d7bff80e4069192d2724223a364b02549527ec9f720a`；Intake 后逐字节摘要一致 |
| scheme version | `af04567a19c3` |
| Harness | `hr_20260726T072555Z_623181f5aecd`，`static/input/unit/dry-run/compare/backtest/api-readiness` 7/7 passed |
| generation / snapshot | `full-20260724-062251-4977e502dadf` / `snapshot-46ff3231de2c4a080c46ba56` |
| Request | `predict=2026-07-24`、`feature=2026-07-23`、`target=2026-07-30` |
| no-persist backtest | 100 requests / 100 records；分批、变序一致；`persist=false` |
| 结构 readiness | composite ID `ten_y_t5_maj4_k3_ic_yearly_v1__h5__10Y`；`pre_shadow + paused`；scheduler/API 预期均不可见 |
| 数据库边界 | Harness 使用已验证仅能读取九张源表的 `bfl_source_readonly`；未写 Harness 审计、Registry、run、prediction、backtest 或 gray-live 表 |
| Registry 检查范围 | 仓库与 Git 全历史无同 ID；本机 active `/api/schemes` 无冲突；生产控制面 Registry SQL 因只读账号无权限未执行 |
| 当前状态 | `TECHNICAL_GATES_PASSED_DESCRIPTION_PENDING`；不是 `SHADOW_READY`，未激活、未登记 Registry、未持久化回测、未写实盘 |

上游补充 `description` 和简洁 `name` 后，Metadata 摘要与 canonical
`scheme_version` 必须变化；该方案届时按新交付版本重新执行七个 Gate，不直接覆盖并沿用本次结论。

### 4.15 记录 003D：10Y T+5 方案 4 技术 Gate

**执行时间**：2026-07-26 15:28 至 15:29，`Asia/Shanghai`。

**机器证据**：[TECHNICAL_ONBOARDING_10Y_T5_4SCHEMES_20260726.evidence.json](TECHNICAL_ONBOARDING_10Y_T5_4SCHEMES_20260726.evidence.json)。

| 项目 | 实测结果 |
|---|---|
| 方案 | `ten_y_t5_say_k5_sharpe_static_v1` |
| 过渡边界 | 本包在 `description` 新政策生效前已接收；缺少 `description` 且 `name` 重复任务格子信息，按用户明确授权继续技术 Gate，状态保持待上游修订 |
| 原始摘要 | Python `960a058e9f98525022d21b19034e7055d49bb56e2bba54447dc0c79870d9010c`；Metadata `a6fd633a49cdb7e8e52d900cf1372fbdeccb61528426d3606d30f1a141b80311`；Intake 后逐字节摘要一致 |
| scheme version | `e8137af4b655` |
| Harness | `hr_20260726T072831Z_2c1bbc549d72`，`static/input/unit/dry-run/compare/backtest/api-readiness` 7/7 passed |
| generation / snapshot | `full-20260724-062251-4977e502dadf` / `snapshot-46ff3231de2c4a080c46ba56` |
| Request | `predict=2026-07-24`、`feature=2026-07-23`、`target=2026-07-30` |
| no-persist backtest | 100 requests / 100 records；分批、变序一致；`persist=false` |
| 结构 readiness | composite ID `ten_y_t5_say_k5_sharpe_static_v1__h5__10Y`；`pre_shadow + paused`；scheduler/API 预期均不可见 |
| 数据库边界 | Harness 使用已验证仅能读取九张源表的 `bfl_source_readonly`；未写 Harness 审计、Registry、run、prediction、backtest 或 gray-live 表 |
| Registry 检查范围 | 仓库与 Git 全历史无同 ID；本机 active `/api/schemes` 无冲突；生产控制面 Registry SQL 因只读账号无权限未执行 |
| 当前状态 | `TECHNICAL_GATES_PASSED_DESCRIPTION_PENDING`；不是 `SHADOW_READY`，未激活、未登记 Registry、未持久化回测、未写实盘 |

上游补充 `description` 和简洁 `name` 后，Metadata 摘要与 canonical
`scheme_version` 必须变化；该方案届时按新交付版本重新执行七个 Gate，不直接覆盖并沿用本次结论。

### 4.16 记录 003 终态：10Y T+5 四方案历史入库完成、实时灰度等待

**最终核验日期**：2026-07-26，`Asia/Shanghai`。

**专项记录**：[GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md](GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md)。

本条只追加终态，不改写记录 003A 至 003D 的技术 Gate 时点证据；以下当前状态
明确 supersede 003A 至 003D 的 `pre_shadow + paused`、未激活和未持久化回测
状态。

| 项目 | 最终核验 |
|---|---|
| exact identity | 四个 exact version 和 composite Registry 均为 `active` |
| description 豁免 | 四个不可变既有 Metadata 均为 `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED`；豁免不改写 Metadata，也不外推到其他交付 |
| persistent backtest | run `182` 至 `185` 分别对应四方案；每方案恰有 1 个成功 run、333 条 canonical prediction 和 17 个月度指标 |
| API / frontend | `/api/schemes` 返回四方案；dashboard 10Y/T+5 格子共有 8 个候选；四方案历史均在 `/api/backtests/factor-lab` 和前端可见 |
| live boundary | production `t_input_generations=0`；四方案 `gray_live`、`live_write`、`scheduled_live` 均为 0，实时 metrics 为空，状态为 `GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION` |
| provenance | `t_backtest_runs.code_hash/config_hash/input_artifact_hash` 三个可选列为 NULL；当前身份仍由 harness run、exact version、prediction extra 和 code/config/Metadata hashes 闭合 |
| 第四方案 P1 | 首次 all-stage run 因系统 Python 3.9 启动，在 compare Gate 失败；服务 Python 3.12 重跑 7/7 成功，但成功 `report_uri` 位于 `/tmp`，尚非耐久审计存储 |
| 调度边界 | scheduler、真实 21/25、rollout=`legacy`、admission=`BLOCKED` 均未改变；四个 active 配置虽含 `schedule_cron`，gray admission 前不得把本 integration 合入或用于重启 legacy scheduler |
| 下一步 | 合法同日 `SEALED` generation 到位后按 exact version 手工 `manual gray_live`；调度设计和 gray admission 通过后才可自动灰度调度 |

该终态只证明历史入库和可见性已完成，不能表述为完整 gray 入库完成，也不授予
`scheduled_live`、formal 或通用生产权限。

### 4.17 记录 003E：10Y T+5 四方案手工灰度入库完成

**最终只读核验时间**：2026-07-27 00:58:19，`Asia/Shanghai`。

**机器证据**：[GRAY_ACCEPTANCE_10Y_T5_4SCHEMES_20260726.evidence.json](GRAY_ACCEPTANCE_10Y_T5_4SCHEMES_20260726.evidence.json)。

本条是记录 003 的最新追加终态，supersede 4.16 的
`GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION` 时点状态，但不删除或改写旧证据。

| 项目 | 最终核验 |
|---|---|
| exact identity | 四个 exact version 和 composite Registry 继续为 `active`，target 均为 `10Y / T+5` |
| persistent backtest | run `182..185`，每方案 333 条 canonical prediction 和 17 个月度指标 |
| manual gray_live | 每方案 39 条，共 156 条；run/prediction/run-log 一一对应，run ID 依次为 `1194..1232`、`1233..1271`、`1272..1310`、`1311..1349` |
| DataBridge provenance | generation `full-20260724-062251-4977e502dadf`；refresh date `2026-07-24`；runtime snapshot `snapshot-46ff3231de2c4a080c46ba56` |
| 日期范围 | `predict_date=2026-05-26..2026-07-20`、`feature_date=2026-05-25..2026-07-17`、`target_date=2026-06-01..2026-07-24`；与历史 `target_date<=2026-05-29` 无重叠 |
| API / frontend | 四方案 metrics 各 39 条；dashboard 每方案 `333 backtest + 39 gray_live = 372` 条展示记录；10Y/T+5 格子仍为 8 个候选 |
| 调度边界 | `scheduled_live=0`，三层 ledger=`0/0/0`，rollout/admission=`legacy/BLOCKED`；没有启动或修改 scheduler |
| 现存 P1 | 第四方案成功 Gate run `hr_20260726T134918Z_b71762de0a2f` 的 `report_uri` 仍位于 `/tmp`，当前可读但未进入受治理的长期审计存储 |
| 后续 | 先完成真实 21/25、迁移、replay、容量与恢复门禁，再通过独立 gray admission 讨论自动调度 |

本条只证明本批手工灰度入库、DB/API 和前端数据链路完成，不授予
`scheduled_live`、formal、08:00 SLA 或通用生产权限。

### 4.18 记录 003F：10Y T+5 四方案代码安全同步开发分支

**最终核验日期**：2026-07-27，`Asia/Shanghai`。

四个方案的 exact delivery/Metadata bytes、配置、技术证据和手工灰度验收
记录已在最新开发基线上完成重基。版本化
`blackbox_scheduler_admission_v1` 将四个 exact
`scheme_id + scheme_version` 冻结为 `gray`；连同 FengRL 五个月度方案，
当前 admission 为 5 个 `formal` 与 9 个 `gray`。

active daily discovery 因四方案变为 25 item/29 target，但正式 daily
policy、capacity candidate、真实 replay 和 DailyRuntime 均只选择原有
21 item/25 target。legacy scheduler 注册、startup catch-up 和 scheduled
wrapper 不会执行这四个方案；本次没有重启 scheduler、写入
`scheduled_live`、修改 rollout/admission 状态或授予自动灰度权限。

### 4.19 记录 004A：cgb_causal_wk_1y 技术入库

**最终核验时间**：2026-07-30 17:07:26，`Asia/Shanghai`。

**专项记录**：[CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.md](CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.md)。

| 项目 | 实测结果 |
|---|---|
| exact identity | `cgb_causal_wk_1y@05022a0eeec7`；Registry ID `cgb_causal_wk_1y__h1__1Y` |
| 原始摘要 | Python `90f3abcc1501eb7173c706fc2ad5d76fda89bf9004c88ee976b98378bbb6d450`；Metadata `efc8e03c5db98c33f0b830d62cc4465f7f890b5a783de68fee58e4ae19d4162b` |
| Harness | `hr_20260730T090642Z_66a2646bd276`，7/7 check-only PASS |
| generation / snapshot | `full-20260730-081804-9794ce962c1a` / `snapshot-2c964086367c6a987f193bcd` |
| Request | `predict=2026-07-25`、`feature=2026-07-24`、`target=2026-07-31` |
| no-persist backtest | 100 requests / 100 records；分批、逆序、未来行隔离通过 |
| 平台修复 | CompareGate prior daily cutoff 按 `api_wind_date-v1` 重新映射权威业务周；未修改交付算法 |
| 当前状态 | `paused + draft`；控制面和业务表零写入；不是 shadow、active 或已上线 |
| 生产边界 | backtest 起点 `2025-01-01`；gray target 起点 `2026-06-01`；首条自然调度三日期 `2026-08-01 / 2026-07-31 / 2026-08-07` |

本轮只证明平台输入、接口、确定性、截止隔离和标准输出通过，不验证算法内部
逻辑或效果。生产写库仍须 exact persisted all-stage、专项授权和 `master`
合并确认。

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

# cgb_causal_wk_3y 周度方案生产入库设计

**日期**：2026-07-30

**状态**：APPROVED

## 目标

将上游 `cgb_causal_wk_3y` Blackbox V2 两文件交付按与 1Y 周度方案相同的
平台路径接入，保持 CV2.0.0-rc1 算法原样，完成生产激活、历史回测、2026 年
6–7 月灰度入库和前端展示；周度 scheduler 暂不挂载。

## 已确认范围

- Base scheme ID：`cgb_causal_wk_3y`。
- Registry ID：`cgb_causal_wk_3y__h1__3Y`。
- 任务：`3Y / weekly_point / horizon=1`。
- 上游算法版本：`2.0.0-rc1`。
- 上游 Python SHA256：
  `849c2fe6e24343230ddcc17212979f981144cb03f44d0ff9b1457f96fa7d0383`。
- 上游 Metadata SHA256：
  `84b09972a583314c401228957a83d9195821ed0fbab93040cfb76e2e3c4bbb38`。
- 历史构造起点：`2025-01-01`。
- 历史持久化边界：`target_date < 2026-06-01`。
- 灰度边界：`2026-06-01 <= target_date <= 2026-07-31`。
- 未来首个完整周期：
  `predict_date=2026-08-01`、
  `feature_date=2026-07-31`、
  `target_date=2026-08-07`。
- 本次不生成 `scheduled_live`，不重启或挂载周度 scheduler。
- 完成后同步开发分支、本地及远程 `master`，不创建 PR。

## 架构选择

采用现有 Blackbox V2 标准链路，不新增第二套入库实现：

```text
上游 .py + .json
  → intake-blackbox
  → paused + draft
  → all-stage check-only
  → 同代 DataBridge/日历核对
  → persisted all-stage
  → shadow / activate
  → historical backtest
  → gray_live backfill
  → API / frontend verification
```

上游目录中的 `api_wind_date.csv` 只用于交付自测摘要对齐，不复制进方案
delivery。Intake 使用 `--platform-input api-wind-date-v1`，平台运行时由统一
provider 把权威日历与 DataBridge 三频父快照组合成四文件只读运行视图。

## 算法保真边界

平台不修改或重新解释下列 CV2 行为：

- 最近 156 个有效样本的滚动训练窗口；
- 半衰期 104 周的时间衰减；
- 上行样本 `0.8` 权重；
- Logistic 与 Ridge 的组合概率；
- `|p-0.5| <= 0.10` 时方向反转；
- 65 维特征顺序和 3Y 活跃券目标 `TB3YWI3C`；
- 不使用外部 base overlay。

上游报告已证明交付脚本在其 0725 sample snapshot 上与研究基准逐周方向
`376/376` 一致。平台不把该结论外推为生产 generation 与上游截图逐值一致。
CV2 的低置信反转会放大数据 vintage 扰动；这一风险只进入专项记录，不通过调参、
改阈值或改算法贴合截图。

## 身份、生命周期与调度

Intake 必须确认仓库、`t_scheme_versions`、Registry 和业务表不存在同名
`cgb_causal_wk_3y` 身份。若发现占用，停止并报告，不覆盖或删除未知数据。

生命周期按以下顺序推进：

1. `paused + draft`；
2. check-only all-stage 7/7；
3. persisted all-stage 7/7；
4. `shadow + shadow`；
5. exact version `active`，Registry composite 行 `active`。

Scheduler admission 只冻结 exact `scheme_id + scheme_version`，配置为：

```text
mode=gray
frequency=weekly
task_type=weekly_point
horizon=1
target_tenor=3Y
capabilities=[]
```

因此服务重启也不会将该方案注册为 recurring 或 direct scheduled job。

## 日期与数据流

平台用权威 `api-wind-date-v1` 从每条 Request 的
`daily_cutoff_key` 精确映射 `weekly_cutoff_key`，并满足：

- 历史：`predict_date=feature_date`；
- 灰度：从目标周反推上一业务周 feature，再按平台 live 语义保存
  `predict_date`；
- `feature_date` 是唯一数据截止；
- 月度指标和历史/灰度分区按 `target_date`；
- 实际方向为 0 保留在分母，只剔除预测为 0 的样本。

历史先写入新 canonical backtest run，再核对预测数量、日期范围和月度指标。
灰度使用 insert-only 受控入口逐目标写入，不允许覆盖其他版本或方案数据。

## 生产门禁与失败策略

以下任一情况立即停止生产写入：

- 两文件摘要或 canonical scheme version 漂移；
- Metadata/目录/Registry identity 不一致；
- DataBridge generation、三频摘要、权威日历摘要或 combined snapshot 不完整；
- Request 日期身份或 cutoff 映射不一致；
- all-stage 非 7/7、不是 exact persisted run；
- 历史与灰度 target 重叠；
- 方案输出数量不是每 Request 一条；
- 前端选中的 canonical run 与刚写入 run 不一致。

所有具有副作用的 activate、backtest persist 和 gray live 操作都使用
`harness authorize` 生成的 exact action token，并在 apply 前先执行 dry-run
或只读预检。

## 复现与前端验收

专项记录必须区分：

1. 上游 0725 sample snapshot 内的研究基准复现；
2. 平台选定 DataBridge generation 内的生产执行；
3. 两个 snapshot 之间因历史修订产生的方向和月度指标差异。

最终验收至少包括：

- Registry 与版本 exact active；
- 历史 run 成功，日期和预测数量与构造范围一致；
- 灰度覆盖 2026-06～07 且与历史零重叠；
- `scheduled_live=0`；
- `/api/schemes`、metrics、factor-lab backtest 和 dashboard 使用 composite ID；
- 前端月度单元与 canonical backtest run 逐格一致；
- 代码、配置、文档、数据库与远程 Git 身份一致。

## 最小化原则

本方案不新增通用 repository、回测或灰度替换工具。1Y 为已存在版本做替换时
需要的旧数据删除逻辑不适用于全新的 3Y 身份；3Y 只复用现有 Intake、生命周期、
持久化回测、gray backfill 和前端核验入口。

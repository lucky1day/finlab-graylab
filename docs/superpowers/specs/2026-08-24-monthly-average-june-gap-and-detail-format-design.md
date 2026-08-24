# 月均 2026-06 缺口与明细格式修复设计

> **实施结果：已完成。** 本设计的最小前端与 gap 语义已进入 exact release `053d562fc40d7ecf5596f56f1beb00e4a3b58178`，并在 ECS 与 Mac3 生效；Mac3 后续补齐五条目标月 2026/06 Prediction，Actual 由 Mac3 本地 authority 生成。本文中的“仅 ECS”描述保留为当时的实施边界，不再代表当前现场状态。

## 目标

仅在 ECS 灰度环境完成以下结果：

1. 为 `m0_monthly_avg_mid_{1y,3y,5y,7y,10y}_v1` 各补一条目标月为 `2026-06` 的 `gray_live` 预测记录。
2. 月均“预测明细”抽屉中的预测日由 `YYYY-MM-DD` 显示为 `MM/DD`。
3. 同一抽屉中的目标月由 `YYYY-MM` 显示为 `YYYY/MM`。

本次不修改月均算法交付文件、Actuals、准确率口径、Registry、调度 timer、systemd unit、Nginx、DNS 或 Mac3 数据。

## 已确认根因

目标月 `2026-06` 对应的月均信号为：

- `predict_date=2026-05-15`
- `feature_date=2026-05-15`
- Contract 内部日期指针 `target_date=2026-05-16`
- 业务目标月 `2026-06`

当前历史候选使用下一目标桶锚点 `2026-06-15` 判断是否早于 `2026-06-01` cutoff，因此该信号未进入 canonical backtest。当前 signal-gap 规划又按内部 `target_date >= 2026-06-01` 判断 gray-live 范围，导致 `2026-05-16` 被排除。现有 gray-live 从 `predict_date=2026-06-15` 开始，对应业务目标月 `2026-07`，最终留下一个业务月份缺口。

## 方案选择

修正现有 `signal-gap-plan` 对 `monthly_average` 的 gray-live eligibility 判断，再通过既有 `signal-gap-fill` 入口补数。

不采用一次性手工 INSERT，因为它会绕过现有的 active version、Registry、DataBridge authority、算法重算、运行记录和业务键缺失校验。不在本次实施完整 `target_month` 数据库迁移，因为该方案涉及数据库、Actuals、API 和全部月均业务键，超出本次最小修复范围。

## 数据补齐设计

### 缺口规划

`monthly_average` 的 gray-live 起点比较使用业务目标月：

1. 从 Contract 内部 `target_date` 所在自然月前进一个月，得到 `YYYY-MM` 目标月。
2. 将平台 `2026-06-01` 起点规范化为目标月 `2026-06`。
3. 当目标月大于等于起始目标月时，将该 case 纳入 gap plan。

其他任务继续沿用现有 `target_date >= PLATFORM_LIVE_TARGET_START_DATE` 判断，不顺带改变季度平均、年度平均或日期型任务语义。

### 受控执行

部署包含上述规划修复的 immutable release 后，对以下五个 scheme 分别运行一次现有 `signal-gap-fill --predict-date 2026-05-15 --scheme-id <exact-id>`：

- `m0_monthly_avg_mid_1y_v1`
- `m0_monthly_avg_mid_3y_v1`
- `m0_monthly_avg_mid_5y_v1`
- `m0_monthly_avg_mid_7y_v1`
- `m0_monthly_avg_mid_10y_v1`

每次操作必须满足：

- scheme exact version 与 active Registry 一致；
- DataBridge generation、business digest、stable identity 和 Request cutoff 通过现有校验；
- 目标业务键写入前不存在；
- 算法返回完整且唯一的预期 target；
- repository 在单事务内写入 prediction、完成 `gray_live` run 并写 run log；
- 任一已存在键或身份漂移均 fail-closed，不覆盖历史记录。

五个 scheme 逐个执行和读回。若任一 scheme 失败，停止后续写入并报告已完成范围，不通过删除或更新回滚已经成功的 insert-only 历史。

## 前端格式设计

格式变化只作用于 `monthly_average` 的“预测明细”抽屉：

- `predictDate=2026-06-15` 渲染为 `06/15`；
- `targetMonth=2025-02` 渲染为 `2025/02`。

原始值保持不变：

- `predictDate` 继续以 ISO 日期参与排序、阶段判断和数据校验；
- `targetMonth` 继续以 `YYYY-MM` 参与月度分组、冲突检测和抽屉选择；
- 只在生成 HTML 单元格时调用纯展示格式函数。

主表月份、趋势图月份、抽屉标题和非月均任务保持当前格式，避免把本次明细格式要求扩展到其他界面。

非法日期或月份不做猜测。月均 Dashboard 数据已经通过严格 ISO 校验；展示函数仍必须对不符合 `YYYY-MM-DD` 或 `YYYY-MM` 的值 fail-closed。

## 测试与验收

### 自动化测试

1. 先增加回归测试，证明修复前 `2026-05-15 / 2026-05-16` 未被纳入 gray-live plan。
2. 修复后断言该 case 被识别为目标月 `2026-06` 的 actionable gap。
3. 断言非月均任务仍使用原有日期边界。
4. 断言月均明细显示 `06/15` 和 `2025/02`，同时视图模型中的 ISO 原始值不变。
5. 运行相关 signal-gap、前端测试及完整本地测试套件。

### ECS 写入验收

五个 scheme 各自满足：

- 恰好新增一条 prediction；
- `predict_date=feature_date=2026-05-15`；
- `target_date=2026-05-16`；
- `prediction_phase=gray_live`；
- scheme version 为当前 active exact version；
- run 为 `success` 且 `records_written=1`；
- 与已有 backtest、gray-live、scheduled-live 业务键无重复。

### 浏览器验收

在 ECS loopback 前端逐个检查五个月均期限：

- 月份序列包含 `2026-06`，且无重复月份；
- `2026-06` 抽屉包含预测日 `05/15`、目标月 `2026/06`；
- 原 `2026-06-15` 行显示预测日 `06/15`；
- Dashboard、HTML、JS、CSS 和 health 均为 HTTP 200；
- 浏览器控制台无 warning/error。

## 发布边界

从精确 `codex/develop` 提交构建确定性 archive，核对 archive、manifest 和安装后 source-tree 摘要，仅晋级 ECS `current`。只重启 `bond-factor-lab-backend.service` 以加载静态前端；不替换或重启任何 timer/service，不改变 Mac3 `current`。

# 全方案问题、结论与验证台账

**文档状态**：`CURRENT`

**目标读者**：项目负责人、算法工程师、平台入库、运维和审计人员

**最后核验日期**：2026-07-31

**记录时区**：除明确标注外，均为 `Asia/Shanghai`

本文是全方案问题的统一记录入口，覆盖 `native_adapter` 和 `blackbox_v2`。
凡发现效果异常、复现差异、输入/日历冲突、落库缺口、调度异常或前端展示问题，
都必须在本文追加一条记录，并至少写明：

1. 发现的问题和影响范围；
2. 已确认的结论，以及尚不能确认的边界；
3. 做过的验证、样本数量和结果；
4. 证据链接、当前处置和后续动作。

本文记录带时点的事实，不修改算法契约，不代替
[当前状态](../CURRENT_STATUS.md)和[当前待办](../TODO.md)。通用规则仍以
[统一入库导航](../onboarding/README.md)、SOP 和机器契约为准。

## 状态定义

| 状态 | 含义 |
|---|---|
| `OPEN` | 问题仍需处理或补齐证据 |
| `EXPLAINED` | 差异已经归因，但若要逐值一致仍需满足记录中的条件 |
| `ACCEPTED_RISK` | 问题已验证，业务明确接受现状，当前不做修复 |
| `RESOLVED` | 已修复并完成相应回归或生产只读验收 |

## 当前问题索引

| Issue | 方案 | 类型 | 状态 | 当前结论 |
|---|---|---|---|---|
| `ISSUE-20260731-001` | `one_y_t1_quote_state_hv_v1` | 效果偏弱 | `ACCEPTED_RISK` | 平台日期、标签和 week_id 对齐；主要是算法样本外方向能力弱，波动率反转进一步拖累 |
| `ISSUE-20260730-002` | `cgb_causal_wk_1y` | 上游/生产复现差异 | `EXPLAINED` | 上游 0725 sample 与生产 0730 generation 不同；72 条中 2 条预测翻号 |
| `ISSUE-20260730-003` | `cgb_causal_wk_3y` | 上游/生产复现差异 | `EXPLAINED` | snapshot、随包日历和统计边界不同；历史 72 条中 3 条、严格灰度 7 条中 1 条预测翻号 |
| `ISSUE-20260731-004` | `weekly_10y_d_overlay_0529` | 生产 week_id/交易日历冲突 | `RESOLVED` | `202625` 源数据修复后四个缺口已按真实算法输出补齐，生产侧为 `81/80` |
| `ISSUE-20260731-005` | `weekly_10y_lgbm_point_v1` | 灰度信号缺口 | `RESOLVED` | `target_date=2026-07-31` 已于 7 月 28 日受控写入，当前为 `81 signal / 80 valid` |
| `ISSUE-20260731-006` | `weekly_10y_d_overlay_0529` | 历史日历覆盖边界 | `RESOLVED` | 双表补齐跨年周 `200951` 的四个交易日，平台映射恢复为 `2009-12-31` |
| `ISSUE-20260731-007` | `weekly_10y_d_overlay_0529` | 当前输入 vintage/benchmark 漂移 | `OPEN` | 当前输入可构造 72 行，但 45 个 benchmark 周中 14 周有内部值差异、3 周方向翻转 |

---

## ISSUE-20260731-001：one_y_t1_quote_state_hv_v1 效果偏弱

### 身份与状态

| 项目 | 记录 |
|---|---|
| Registry ID | `one_y_t1_quote_state_hv_v1__h1__1Y` |
| Scheme version | `d6d0cb43aacd` |
| 任务 | `1Y / T+1 / daily / horizon=1` |
| 发现日期 | 2026-07-31 |
| 当前状态 | `ACCEPTED_RISK` |
| 当前处置 | 保持已上线状态；本轮只读诊断，不修改算法、历史数据、灰度数据或 Registry |

完整生产身份和入库证据见
[1Y T+1 生产入库记录](../blackbox_v2/records/ONE_Y_T1_QUOTE_STATE_HV_ONBOARDING_20260730.md)。

### 发现的问题

前端合并历史和灰度后的已评估结果为 `89/213=41.8%`，明显弱于同一
`1Y / T+1` 格子中的既有日度方案。需要判断是否由日期错位、实际方向为 0、
周特征 week_id、平台入库或算法内部规则造成。

### 效果拆解

| 区间 | 总样本 | 预测非 0 | 正确 | 正式准确率 |
|---|---:|---:|---:|---:|
| 历史回测 run `195` | 337 | 191 | 78 | 40.8% |
| 2026-06 至 2026-07 灰度，actual 已生成 | 42 | 22 | 11 | 50.0% |
| 合计 | 379 | 213 | 89 | 41.8% |

同日期的既有 `daily_1y_xgb_1y13_0629` 对照为：

| 区间 | 预测非 0 | 正确 | 准确率 |
|---|---:|---:|---:|
| 相同 337 条历史 | 216 | 114 | 52.8% |
| 相同 42 条已有 actual 的灰度 | 15 | 13 | 86.7% |
| 合计 | 231 | 127 | 55.0% |

灰度对照只有 15 条有效信号，不能单独据此评价长期效果；但相同日期和相同 actual
下既有方案能得到不同结果，足以排除“前端、actual 或统一统计链路必然把准确率
压低到 41.8%”。

### 做过的验证

#### 1. 日期、目标日和实际方向

使用当前 DataBridge 日频 `TB1YWI0C`，对每条记录独立重算
`sign(yield(target_date) - yield(feature_date))`，同时检查目标日是否为
`feature_date` 后的下一交易日：

| 范围 | 可重算 | actual 不一致 | 下一交易日不一致 |
|---|---:|---:|---:|
| 新方案历史 | 337/337 | 0 | 0 |
| 新方案灰度 actual 已生成部分 | 42/42 | 0 | 0 |
| 既有对照方案历史 | 337/337 | 0 | 0 |
| 既有对照方案灰度 | 42/42 | 0 | 0 |

结论：不是 T/T+1 错位，也不是平台实际方向计算错误。

#### 2. week_id 和周频特征映射

把算法按日频交易序列生成的周分组，与 `api_wind_date.week_id` 的权威映射逐周
对比。日频共 3886 行、820 个可比交易周；算法所选 820 个周键与权威日历
`820/820` 一致，不一致为 0。

结论：周频特征没有因 ISO week、自然周或尾部错位而使用错误 week_id。

#### 3. 实际方向为 0 的影响

平台规则保持不变：只剔除 `predicted_direction=0`，不得剔除
`actual_direction=0`。

- 历史 191 条有效信号中有 34 条 actual 为 0，占 17.8%，这些记录按规则计错；
- 仅用于诊断地排除这 34 条后为 `78/157=49.7%`；
- 灰度 22 条有效信号中有 3 条 actual 为 0；诊断性排除后为
  `11/19=57.9%`。

结论：actual 为 0 会显著压低正式指标，但不是数据问题；即使诊断性排除，历史
方向能力仍接近随机，不能解释全部弱表现。

#### 4. 算法信号来源

按交付脚本原始字节，在相同 snapshot 上只读复算，并确认诊断输出的最终方向与
已落库预测逐条一致：

| 历史信号来源 | 信号数 | 正确 | 准确率 | actual=0 | 说明 |
|---|---:|---:|---:|---:|---|
| primary | 87 | 33 | 37.9% | 15 | 同时通过 move probability 和 direction margin |
| quota | 104 | 45 | 43.3% | 19 | 为满足月内 50% coverage floor 补足 |
| abstain | 146 | — | — | — | 最终预测为 0，不进入指标 |

灰度 22 条有效信号中，primary 为 7 条、quota 为 15 条；即 68.2% 的灰度有效
信号由 coverage floor 补足。

结论：50% coverage floor 确实扩大了弱信号暴露，但不是唯一根因。历史 primary
本身也只有 37.9%，说明基础方向模型或其门控没有形成稳定样本外优势。

#### 5. 波动率反转规则

交付算法会在 `vol10 / vol60 > 0.70` 时反转所有非 0 方向。历史 191 条有效
信号中有 92 条触发反转：

| 口径 | 正确 | 准确率 |
|---|---:|---:|
| 反转前的同一批信号 | 88 | 46.1% |
| 当前最终输出 | 78 | 40.8% |

该规则在历史上净损失 10 条正确结果，拖累约 5.3 个百分点；但灰度中当前反转
规则略有帮助，说明固定阈值的跨阶段表现不稳定。取消反转后的历史 46.1% 仍未
达到可证明的稳定优势，因此不能把全部问题只归因于这一条规则。

#### 6. Gate 能证明和不能证明的范围

七个 Gate 已证明：输入 snapshot 固定、重复执行确定、predict/backtest 一致、
分批/顺序不改变结果、未来行隔离和标准输出符合契约。

交付包没有提供与生产 DataBridge generation 对齐的上游逐日预期预测或效果
benchmark。因此 Gate PASS 不等于算法效果达标，也不能证明上游研究环境与生产
环境的逐日模型数值一致。

### 最终结论

1. 平台输入、T+1 日期、actual、week_id、落库和前端统计均未发现错位；
2. actual=0 按已确认规则保留，确实拉低指标，但不是异常；
3. 固定波动率反转在历史上造成了可量化伤害；
4. coverage floor 强制生成大量弱信号，但 primary 信号同样偏弱；
5. 根因归类为算法样本外方向能力不足及规则跨阶段不稳定，不是平台故障。

2026-07-31，用户在本任务中明确回复“没事这块”，要求保持上线并把发现的问题、
结论和验证统一落入本文；该决定没有授权修改算法或业务数据。因此本项标记
`ACCEPTED_RISK`。若以后要求优化，必须由上游提供同一 DataBridge generation、
同一日期边界和“只剔除预测为 0”口径的逐日基准；取消反转、取消
coverage floor 等只能作为新的 Blackbox 版本做 no-persist 消融，不能直接修改
当前生产版本。

---

## ISSUE-20260730-002：cgb_causal_wk_1y 上游与生产复现差异

### 发现的问题

上游复现报告与平台生产月度结果部分不一致，曾怀疑实际方向为 0、周历和日期归属
未对齐。

### 做过的验证

- 历史 `feature_date + target_date` 为 72/72 对齐；
- 实际方向为 72/72 对齐；
- 预测方向为 70/72 对齐；
- 差异只出现在 `week_id=202538` 和 `week_id=202603`；
- 上游 0725 sample 与生产 0730 generation 摘要不同，相关日频字段存在历史
  值/空值修订；
- 上游按 `feature_date` 月份汇总，平台按 `target_date` 月份汇总；
- 双方均保留 `actual_direction=0`，只剔除预测为 0，因此零值口径已经对齐；
- 新版交付已强制外部读取 `api_wind_date.csv`，平台使用
  `api-wind-date-v1`，不再使用脚本内嵌周历或尾部推算。

### 结论与状态

状态为 `EXPLAINED`。当前差异来自输入 vintage、月份归属和历史字段展示语义，
不是实际方向为 0 的过滤错误，也不是新版平台执行失败。要宣称生产逐值完全一致，
上游必须下载并固定与平台相同的 DataBridge generation、相同
`api-wind-date-v1` 摘要，并统一 `target_date` 月份和历史/灰度边界。

完整逐行证据见
[1Y 周度生产入库记录](../blackbox_v2/records/CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.md)。

---

## ISSUE-20260730-003：cgb_causal_wk_3y 上游与生产复现差异

### 发现的问题

上游报告的总准确率、部分月份和平台前端结果不完全一致，需要区分算法复现问题、
输入 snapshot 差异、周历差异和统计分区差异。

### 做过的验证

- 上游在其 0725 sample 内与研究参考方向 `376/376` 一致；
- 平台历史与上游同窗口日期 72/72、actual 72/72 对齐；
- 历史预测 69/72 对齐，3 条差异为 `202512`、`202514`、`202527`；
- 严格同窗口灰度预测 6/7 对齐，唯一差异为 `202626`；
- 上游 sample 与生产 generation 的 daily、weekly、monthly、calendar 四份摘要
  均不同；
- 上游随包日历有 4 处非严格升序，并缺少权威周 `202625`；平台未使用该日历；
- 上游按 `feature_date` 分月，平台按 `target_date` 分月，并按
  `target_date >= 2026-06-01` 切入灰度；
- 双方均只剔除预测为 0，不剔除 actual 为 0。

### 结论与状态

状态为 `EXPLAINED`。上游 snapshot 内复现一致，生产 generation 内平台执行和
落库一致；二者不是同一输入 vintage，不能混称生产逐值完全一致。当前没有证据
表明平台日期或 actual 计算错误。

完整逐行和尾部周历证据见
[3Y 周度生产入库记录](../blackbox_v2/records/CGB_CAUSAL_WK_3Y_ONBOARDING_20260730.md)。

---

## ISSUE-20260731-004：weekly_10y_d_overlay_0529 生产 week_id 冲突

### 发现的问题

该方案最初处理 `week_id=202625` 时，周频源表与平台权威交易日历发生契约
冲突；当时正确地 fail-closed，生产信号暂时停在 `77 signal / 77 valid`。

### 做过的验证与结论

- 当前 `api_wind_date` 已将 `2026-06-29..2026-07-05` 统一映射为
  `week_id=202625`，最后交易日为 `2026-07-03`；
- `api_wind_weekly` 和 `api_wind_derivative_weekly` 自 2025 年以来与当前权威
  日历的逐周 mismatch 均为 0；
- 2026-07-28 已使用正式 no-write runner 逐点复核并按真实算法结果补齐：
  - run `1438`：`202625 -> 202626`，方向 `-1`；
  - run `1439`：`202626 -> 202627`，方向 `-1`；
  - run `1440`：`202627 -> 202628`，方向 `-1`；
  - run `1441`：`202628 -> 202629`，方向 `+1`；
- 四条均为 `gray_live`，带
  `manual_backfill_reason=week_id_contract_repaired_july_gap`，没有伪造成
  `scheduled_live`；
- 当前生产预测 9 条、已有 actual 8 条；与 72 条历史合并后为
  `81 signal / 80 valid`。

状态为 `RESOLVED`。生产 `202625` 冲突和四个信号缺口均已闭合；不得再把旧的
`77/77` 写作当前状态。历史 `200951` 覆盖边界和当前输入 vintage 漂移是两个
独立问题，分别记录在 `ISSUE-20260731-006/007`。

---

## ISSUE-20260731-005：weekly_10y_lgbm_point_v1 灰度信号缺口

### 发现的问题

早期核对曾把该方案记录为 `80 signal / 80 valid`，并误判缺少
`target_date=2026-07-31` 的灰度信号。

### 做过的验证与结论

- 只读生产查询确认 prediction id `1564`、run `1424` 已于
  `2026-07-28 10:10:38` 写入；
- 该行为 `gray_live`，`predict_date=2026-07-25`、
  `feature_date=2026-07-24`、`target_date=2026-07-31`，不是伪造的
  `scheduled_live`；
- latest backtest 72 行，live 9 行，合计 `81 signal`；截至当前已有 8 个 live
  target 的 actual，合计 `80 valid`；
- 当前 active Registry 中有 6 个 `weekly_point` 和 3 个
  `weekly_average`；数量对齐只在同一 `task_type` 内比较。

状态为 `RESOLVED`。不得再次补写 `target_date=2026-07-31`，避免重复预测。

---

## ISSUE-20260731-006：weekly_10y_d_overlay_0529 历史日历覆盖边界

### 发现的问题

历史 runner 请求从 `start_week=200901` 构造输入，但当前实际 artifact 的首周为
`200951`。`api_wind_date` 已有 `2009-12-31 -> 200951`，而
`t_trade_calendar` 从 `2010-01-01` 才开始，且 `2010-01-01..03` 均为
非交易日。两表 JOIN 后找不到 `200951` 的最后交易日，runner 因此在算法执行前
fail-closed。

### 做过的验证与结论

- 上游权威生成器
  `/Users/macstudio0/bondprojectpro/BondPrediction/cn_stock_calendar.py`
  将 `2009-12-28..31` 全部判为交易日并映射到 `200951`；
- 2026-07-31 在命名锁、数据库 identity 围栏和单事务内执行最小补数：
  - `api_wind_date` 新增 `2009-12-28..30` 3 行，既有 `12-31` exact no-op；
  - `t_trade_calendar` 新增 `2009-12-28..31` 4 行；
- 全程只插缺失行，未执行 `UPDATE/REPLACE/ON DUPLICATE KEY UPDATE`，未扩展
  到整个 2009 年；
- 只读 postcheck 确认两表目标日期均为 4 行/4 个唯一日期，全表重复日期组均为
  0；`CalendarService` 映射 `200951 -> 2009-12-31`，同时
  `202625 -> 2026-07-03` 保持不变；
- 修复后两表总行数分别为 `6067` 和 `6213`；
- 三份已封存 Native generation 的 manifest、11 个 payload、mtime 和只读权限
  均未变化；旧 generation 保留旧日历是不可变设计的正确行为；
- 新 `api-wind-date-v1` 制品 SHA256 为
  `18db31076b58eb119b6f4110d3448653a506fb2d21ca2577ed1aae4d177bcc23`，
  后续新 generation/combined snapshot 必须使用新身份，不能覆盖旧入库证据；
- 依赖该平台输入的 7 个 active Blackbox Registry 均为
  `gray + capabilities=[]`，当前无 scheduler 或在途 run，因此没有立即生产风险。

状态为 `RESOLVED`。该修复只关闭数据库日历覆盖边界，不代表当前输入已重新通过
旧 benchmark；完整设计和操作边界见
[week 200951 日历修复设计](../superpowers/specs/2026-07-31-week-200951-calendar-boundary-repair-design.md)。

---

## ISSUE-20260731-007：weekly_10y_d_overlay_0529 当前输入 vintage 漂移

### 发现的问题

在内存模拟、随后生产补齐 `200951` 日历后，当前 DB 输入能够完成算法计算并构造
72 行历史结果，但不再与旧 original benchmark 逐值一致。

### 做过的验证与结论

- 历史构造为 72 行、17 个月，日期和 target/label 口径保持一致；
- 45 个 benchmark feature week 中，14 个周存在 20 个内部字段差异；
- `202538`、`202548`、`202602` 三周预测方向翻转；
- 首个差异为 `202533` confidence：
  当前 `0.8618307845278366`，benchmark `0.90256711825722`；
- 本次只读重算输入为 840 行、范围 `200951..202620`，内存 round-trip SHA256
  为 `3db4bb568dba38af6ae7b149238c3309c66fe54abd4caf3e639de5632eff5ba7`；
- 旧 latest run `108` 和旧 artifact 可 `45/45`，证明 benchmark 自身不是本次
  日历修复产生的；差异来自当前受治理输入 vintage。

状态为 `OPEN`。CompareGate 继续 fail-closed；禁止调算法、改 benchmark、删除
预热周或使用旧 generation fallback 贴合。后续必须按输入 artifact/source 口径
专项研究，闭合后才能重新声明当前 DB 输入下 `45/45`。

## 后续追加模板

```markdown
## ISSUE-YYYYMMDD-NNN：{scheme_id} {问题摘要}

### 身份与状态
- Registry / base scheme / version：
- 发现时间：
- 状态：OPEN / EXPLAINED / ACCEPTED_RISK / RESOLVED
- 影响范围：

### 发现的问题
{现象、样本范围、正式统计口径}

### 做过的验证
{输入 generation、日期、SQL/API/前端、逐行对比、Gate 或消融结果}

### 结论与边界
{已证明什么、尚不能证明什么、是否属于平台/算法/输入/调度问题}

### 处置与证据
{当前动作、负责人/授权边界、证据链接；修复后只追加，不覆盖原记录}
```

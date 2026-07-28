# Liwei 日频增量缓存 MVP 设计

**文档状态**：`CURRENT`

**核验日期**：2026-07-29

## 目标

在不改变 Liwei 0616 系列滚动训练语义的前提下，复用现有 Phase A
不可变缓存，使普通日频运行只计算新增或真实受影响的日期。缓存改造服务于
当前日频 25 execution / 29 target MVP，不扩展周频、月频调度和长期存储
治理。

完成后，普通交易日必须满足：

- 不清空缓存，不使用 forced-cold root；
- 七个 `cache_family + tenor` 独立准备缓存；
- 已有历史 Phase A 结果继续复用；
- 正常 append 只训练新增一至两个日期；
- 同缓存家族的后续 consumer 只读同一 generation；
- 单个缓存家族异常时允许该家族自动 full rebuild 并告警，但不阻塞其他
  Native 或 Blackbox；
- prediction、run、item 和 target receipt 的既有原子提交语义不变。

性能目标是把普通 Liwei 单方案运行降到几分钟级。该目标必须由一次真实
warm-incremental 联跑验证；不得用 forced-cold 耗时代表日常运行耗时。

## 已确认的根因

当前缓存状态由整张 `daily/weekly/monthly` 原始表生成。周表和月表的摘要
覆盖所有字段及所有周期，因此以下变化都会被误判为影响算法：

- 算法未使用字段发生变化；
- 当前 `feature_date` 尚不可见的未来周期补值；
- 未使用字段的浮点序列化末位变化。

任一 weekly/monthly `revision` 都会退化为 full rebuild；append
在无法映射到日频日期时也会 full rebuild。真实 Liwei cache spec 尚未
声明 daily dependency proof。

2026-07-28 的 10Y 现场证据为：

- daily：`append`；
- weekly：`revision`，实际是 `week_id=202629` 三个未使用字段的浮点
  文本末位变化；
- monthly：`revision`，实际是 `month_id=202608` 四十个未使用字段由
  NULL 变为数值，且对 `feature_date=2026-07-27` 不可见；
- 缓存最终选择 `full`。

共享缓存还有独立的覆盖范围缺陷：10Y 家族的宽缓存曾包含 618 个日期，
随后被窄窗口 consumer 发布的 42 日期 generation 替换。Ledger policy
虽然可以用 prerequisite 控制执行顺序，但缓存层本身不得允许窄 consumer
缩小共享 current。

forced-cold replay 主动创建空缓存根目录，只用于首次部署或灾难恢复验证，
不是上述生产失效问题的根因，也不属于普通日频性能门禁。

## 缓存家族与发布者

十个 Liwei execution 归并为七个缓存家族：

| cache family | tenor | 唯一发布者 | consumer |
|---|---|---|---|
| `liwei_0616_10y_v61` | `10Y` | `liwei_0616_10y01_full_oos_k3_div_k10` | 两个 10Y cons 方案 |
| `liwei_0616_5y_v31` | `5Y` | `liwei_0616_5y01_full_oos_k3_div_k10` | `liwei_0616_cons_sda_k3_div_k10` |
| `liwei_0616_5y_allk10_auc_static_v1` | `5Y` | 方案自身 | 无 |
| `liwei_0616_5y_allk10_auc_yearly_v1` | `5Y` | 方案自身 | 无 |
| `liwei_0616_5y_allk10_ic_yearly_v1` | `5Y` | 方案自身 | 无 |
| `liwei_0616_7y01_v31` | `7Y` | 方案自身 | 无 |
| `liwei_0616_7y03_v31` | `7Y` | 方案自身 | 无 |

共享家族只允许唯一发布者创建或切换 generation。consumer 只能读取
当前已验收 generation；输入身份不一致、覆盖范围不足或缓存尚未发布时，
consumer 返回可恢复的等待状态，不得自行重建或替换 current。

## 有效输入投影

### 边界

本 MVP 保留 daily 原始输入的精确摘要和现有 append 逻辑。daily 历史
revision 缺少证明时继续 full rebuild，不在本轮推导新的 daily 依赖窗口。

只重构造成现场假失效的 weekly/monthly 判断。共享缓存层不得自行复刻
Native core 的对齐算法；每个 inference adapter 使用自身 core 的既有纯
函数生成日频有效输入投影，再把投影交给共享缓存层。

### 投影内容

weekly/monthly 投影必须：

1. 只选择 core 声明的 `WEEKLY_COLS` 和 `MONTHLY_COLS`，保持原列序；
2. 使用实际 `date_to_week`，包括 explicit/fallback 模式；
3. 使用原算法的 legacy weekly placement、ffill 和变换；
4. 使用上一自然月 monthly alignment、ffill 和变换；
5. 严格裁剪到当前 daily grid 和 `feature_date`；
6. 为每个日频日期生成确定性的有效输入行摘要。

投影证明至少绑定：

- 规范化 `date_to_week` 内容及 SHA-256；
- explicit/fallback 模式；
- weekly/monthly 对齐函数摘要；
-使用字段、顺序和 dtype；
- projection schema/ABI；
- core/spec fingerprint；
- daily grid 和 feature cutoff。

不对有效字段做任意 round。未使用字段不进入投影；有效字段保持精确比较。

## 增量决策

缓存层比较 parent 与 current 的日频有效输入投影：

| 条件 | 决策 |
|---|---|
| 投影前缀不变，仅新增 requested date | `append` |
| 投影最早在日频日期 `D` 变化 | `suffix`，重算 `[D, current]` |
| 未来周/月新增但尚未映射到 daily grid | 保留旧前缀，只处理新增 requested date |
| 无 parent 或 cache 文件损坏 | 该家族 `full` 并告警 |
| schema、used columns、alignment、calendar proof 漂移 | 该家族 `full` 并告警 |
| 无法确定最早影响日期 | 该家族 `full` 并告警 |

如果投影变化落入固定 IC screening 的全局依赖区，suffix 起点必须保守降为
该 generation 最早缓存日期，等价于该家族 full rebuild。不能为了命中缓存
忽略真实输入修订。

## 旧缓存迁移边界

不执行集中式全量预热，也不对旧缓存做不可证明的无训练接管。现存 schema 2
generation 没有记录有效周/月投影和 `date_to_week` 证明，单凭原始 frame
前缀或结果重叠不足以证明可复用。

因此首次由新代码处理某个旧 family 时，该 family 单独执行一次自动 full
rebuild 并告警；其他 family 和无依赖日频任务继续运行。新 schema 3
generation 发布后，后续日批才按有效投影执行 append/suffix。未来只有在
source generation 自带完整投影、mapping 和 lineage 证明时，才可另行设计
无训练 adoption；它不属于本次 MVP。

缓存覆盖范围必须单调：新 generation 对相同 spec 和输入 lineage 的
`test_dates` 必须包含 parent 的全部有效日期。窄请求只能读取子集，
不得发布窄 generation。

## 失败与恢复

- staging 构建失败、进程 kill、磁盘写入失败时不切换 current；
- 旧 generation 保持可读；
- corrupt current 可回退到 lineage 中最近一个通过完整性和输入证明的
  generation；这只是 cache 回退，不是使用旧 Native/DataBridge 输入；
- 找不到可信 cache 时，该家族自动 full rebuild并发出结构化告警；
- full rebuild 只占该家族的 prewarmer lane，不阻塞其他无依赖方案；
- consumer 在 prewarmer 终态前保持等待；prewarmer 失败后只收口该家族
  consumer；
- scheduler 的 attempt、stale fence、SLA 和原子提交语义不变。

## 最小验证门禁

### 1. 缓存针对性测试

- 2026-07-27 → 2026-07-28 真实回归 fixture；
- 未使用 weekly 字段的 1-ULP/文本末位变化不失效；
- 未使用 monthly 字段补值不失效；
- 未来 month/week 未映射到 daily grid 时不失效；
- 正常 append 只把新增日期传给 trainer；
- 有效 weekly/monthly 历史修订从最早投影变化日期 suffix；
- projection proof 或 schema 漂移时 full；
- 618 日期缓存不能被 42 日期 consumer 缩小；
- generation 构建或原子切换被 kill 时旧 current 不变；
- 缓存缺失或损坏时只有该家族 full rebuild。

### 2. 七家族增量验证

- 七个唯一发布者各执行一次 warm incremental；
- trainer 收到的日期精确等于新增或 suffix 日期；
- 两个共享家族的 consumer 为只读 hit；
- 新增/受影响日期以独立 cold 计算验证 Phase A
  `config/test_dates/preds/probs`；
- 最终 direction、vote score、baseline score/sign、probability、
  confidence 和 extra 完全满足原 CompareGate。

### 3. 日频调度验证

- 使用受控执行器完成一次临时 MySQL 25/29；
- 使用现有缓存快照的隔离副本完成一次真实 warm-incremental 25/29；
- 最终 29/29、无 duplicate、partial 或 orphan；
- 单个家族 fallback/full 不阻塞其他 execution；
- 重入不新增 run 或 prediction；
- 最终候选只运行一次完整 pytest、compileall 和 `git diff --check`。

## 不作为本 MVP 阻断

- 反复 forced-cold 25/29 联跑；
- 每个 commit 重跑完整 pytest；
- 20+20、连续十日和统计 P95；
- 周频/月频自动调度；
- 历史信号缺口；
- migrations 019/020；
- generation 长期归档和磁盘治理；
- 三个 0629 generation adapter；
- 全部故障注入组合；
- 通用工作流或分布式缓存系统。

上述项目继续保留在后续 TODO；不得因为本缓存修复扩大当前上线范围。

## 发布边界

- 改动只允许发生在 Liwei inference adapter、共享 Phase A cache、日频
  cache policy及对应测试；
- 不修改 Native core 算法窗口、特征、模型、投票或 fallback；
- 不修改 Blackbox、周频/月频调度、BondProjectPro 或生产凭据；
- 测试使用隔离缓存副本和隔离数据库，不写生产数据库；
- backend/frontend 在开发和联跑期间保持运行；
- 完成实现、针对性验证、一次真实 warm-incremental 25/29 和最终全量
  回归后，才形成可申请生产切换授权的候选；
- 合并 `master`、推送、服务切换和生产部署仍需用户明确授权。

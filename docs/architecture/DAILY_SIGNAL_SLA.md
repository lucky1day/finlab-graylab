# 日频信号 08:00 SLA 架构

**文档状态**：`CURRENT`

**目标读者**：平台开发、运维、架构评审和风险控制人员

**最后核验日期**：2026-07-30

本文是已经批准、待切换的日频目标合同，不是 production 已上线或某交易日已产生
ledger occurrence 的证据。动态运行事实只看[当前状态](../CURRENT_STATUS.md)；
历史 rehearsal、旧规模和旧阻塞结论只在 `docs/records/` 与
`docs/blackbox_v2/records/` 中保留，不反向定义本目标门禁。

## 1. 已批准目标生产口径

- 单台 Mac Studio 只运行一个日频 coordinator。
- 每个交易日只有一个 `daily-signals + predict_date` occurrence；重复 tick、
  重启和 operator recovery 必须复用同一 occurrence，不得创建第二批。
- occurrence 创建时冻结 active daily Registry、代码/config 摘要、输入
  generation、25 个 base execution 和 29 个业务 target。精确身份以
  `deploy/daily_scheduler_policy_v2.json` 为机器权威。
- 25 个 execution 固定由 17 个 Native V1 和 8 个 Blackbox V2 组成；Native
  最大并发 2，Blackbox V2 最大并发 2。资源不足只能延后或 fail-closed，不能
  动态扩大并发。
- 完成标准只有 29/29 个 signal 全部经 target receipt 验收；25/25 item 不是
  对外完成口径。
- 日常生产使用 warm cache。清空 cache、换空 root 或其它冷启动压力实验不属于
  生产门禁；当前也不设置额外的性能准入层、collector/operator 双签名或双重
  信任链。
- ledger 模式下 `com.bond-factor-lab.v2-preflight` 保持未加载。DataBridge
  refresh 由 coordinator 唯一拥有，旧 preflight 不得与 ledger 并行。

## 2. 日批时序

| 时间（Asia/Shanghai） | 不可变语义 |
|---|---|
| 06:30 | hard `not_before`；冻结 occurrence，并检查 T-1 日历和最小输入 readiness |
| readiness 通过 | 构建 Native generation，并由同一 coordinator 刷新当天 DataBridge generation |
| 06:55 | DataBridge readiness guardrail；未封存记 `LATE` 并告警，但可继续到 08:30 |
| DataBridge `sealed_at` | 8 个 V2 按 policy 的 `+0/+2/.../+14` 分钟释放，最大并发 2 |
| 07:00 | watchdog 只检查进度、恢复和告警，不重启 scheduler |
| 07:45 | 未启动 V2 永久记 item `sla_status=LATE`，但仍可继续 |
| 07:55 | 29 个 target 的内部就绪目标 |
| 08:00 | 少一个 target 即 write-once `sla_outcome=BREACHED` |
| 08:30 | 禁止新 attempt 和自动重试；已运行 attempt 只可在冻结 timeout 内收口 |

不跨日自动补跑，不使用昨日 generation，不把 08:00 后补齐回写为 `MET`。
同日 recovery tick 只补写到期 guardrail 并恢复同一 occurrence。

## 3. 输入 generation 与安全存储

`t_input_generations` 管理 `native_source` 与 `databridge_v1`。两类输入必须：

1. 在 MySQL `REPEATABLE READ` 只读快照中采集 source evidence，并经
   `shared.input_artifacts` 生成内容寻址 manifest；
2. 在 `.building-*` 中写完 payload 和 manifest，逐文件重开、SHA-256、只读、
   `fsync`，再原子 rename 并同步父目录；
3. 以数据库时钟写入 `sealed_at`；manifest 本机墙钟不承担释放权威；
4. 每次打开都重算 manifest 和文件 hash，schema、business/feature date、
   Native 关联或摘要任一漂移即 fail-closed；
5. 使用服务 UID 私有的绝对 root。root 及 generation 祖先不得是 symlink，owner
   必须匹配，group/other 不得可写；生产建议 `0700` 目录、`0600/0400` 文件；
6. 只清理精确可证明为 `INVALIDATED`、无 occurrence/item 引用且非父代的 payload。
   `SEALED` 或被引用 generation 不自动删除。

DataBridge scheduled-live 必须使用当天全新 SEALED generation；旧 `current`
只能审计，不能 fallback。三个固定 0629 Native 的 `live_source_0629` 兼容桥仍
必须绑定当天 Native generation fence、精确 source package hash 和实际输出水位；
该例外不得扩展。

## 4. Ledger、迁移和完成权

### 4.1 三层账本

- `t_schedule_occurrences`：唯一键 `schedule_key + predict_date`；冻结
  `feature_date`、policy、Registry digest、期望 item/target、cutoff 和 SLA。
- `t_schedule_items`：唯一键 `occurrence_id + base_scheme_id`；冻结 runtime、
  version、code/config、input/cache identity 和当前 attempt。
- `t_schedule_item_targets`：冻结 registry composite ID、tenor、horizon 与 target
  receipt；唯一键同时覆盖 occurrence registry identity 和 item target identity。

`scheduled_live` 必须绑定真实 `schedule_item_id`、winning attempt 与
occurrence。manual/background/backfill 不得伪造该 provenance。

### 4.2 migration 018

018 只把 `t_scheme_runs.started_at` 修正为
`DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6)`，使 claim 与真实子进程启动之间
的账本状态合法；它不增加 composite FK。迁移行为唯一实现在 caller-supplied
`Engine` 的 `migrations.runner`，唯一 operator 包装器是
`scripts/apply_migrations.py`。

普通 `--apply` 与 017/018 recovery 都必须在建 Engine 前提供
`--expected-database-name <database-name>` 和
`--expected-server-uuid <server-uuid>`，并在首个写动作前 exact compare。
`--inspect-applying-017`、`--inspect-applying-018` 保持只读；recovery 使用
`--recover-applying-017 --apply` 或 `--recover-applying-018 --apply` 加
state digest。恢复 017 后必须另行执行普通 `--apply` 才会推进 018。生产 UUID、
DSN 和凭据不得写入仓库或报告。

### 4.3 单实例锁与 run fence

- owner 使用 machine-global `flock` 保护 occurrence create/recover/dispatch；
  service UID、规范路径、inode、owner 和 mode 任一漂移即 fail-closed。
- 每个 item 的完成权由数据库 `current_run_id + attempt_no` fence 决定。旧、重复
  或失去完成权的 attempt 不得写 prediction、receipt 或终态。
- Native 与 V2 共用 occurrence 级 `ProcessStartGuard`，在同一短临界区完成
  pre-fence、`Popen`、PID/PGID 捕获、账本登记和 post-fence。异常时必须终止
  新进程组；无法确认清理时 poison guard 并停止后续启动。
- 仅 `TRANSIENT_INFRA` 可在全体首轮已覆盖且 08:30 前有静态预算时自动重试一次。
  data、contract、algorithm、result structure 和 timeout 不自动重试。

## 5. 原子提交与 phase 边界

一个 item 的所有 target 必须在单一事务中：

1. 锁定 occurrence、item、winning attempt 和冻结 target；
2. 重验 epoch、policy、version、code/config、input generation、cache generation
   和日期语义；
3. insert-only 写 canonical prediction；
4. 写 target receipt，再把 run/item 标记成功；
5. commit 后由只读 visibility 路径写 `visible_at`，API 可见性失败不能伪装为完成。

历史缺口和真实 ledger 的 phase 严格分开：

| predict_date | 历史缺口数 | 唯一允许 phase |
|---|---:|---|
| 2026-07-23 | 1 | `gray_live` |
| 2026-07-24 | 1 | `gray_live` |
| 2026-07-27 | 2 | `gray_live` |
| 2026-07-28 | 17 | `gray_live` |
| 2026-07-29 | 29 | `gray_live` |
| 未来首个及后续经证据验收的真实 ledger occurrence | 29/29 | `scheduled_live` |

历史缺口合计 50，其中 T+1 为 5、T+5 为 45。补缺只能 insert-only 写
`gray_live`；当前 50 条均尚未写入。不得倒签、改写或复用 ledger receipt 来制造
`scheduled_live`。2026-07-30 尚无真实 occurrence/receipt 证据，日期本身不能
成为正式 phase 起点。

## 6. Liwei Phase A schema 3 缓存契约

### 6.1 精确家族、publisher 与 consumer

每个 `cache_family + tenor` 只有一个 publisher；下表的 spec fingerprint 必须与
`deploy/daily_scheduler_policy_v2.json` 精确一致：

| family | tenor | spec fingerprint | 唯一 publisher | consumer |
|---|---|---|---|---|
| `liwei_0616_10y_v61` | `10Y` | `36e6750a78e704b337acc2ffbbea9e8fe25a32667440b0ae3a48cd7a6b150b46` | `liwei_0616_10y01_full_oos_k3_div_k10` | `liwei_0616_10y01_cons_say_k3_div_k10` |
| `liwei_0616_10y_v61` | `10Y` | `36e6750a78e704b337acc2ffbbea9e8fe25a32667440b0ae3a48cd7a6b150b46` | `liwei_0616_10y01_full_oos_k3_div_k10` | `liwei_0616_10y01_full_oos_k3_div_k10` |
| `liwei_0616_10y_v61` | `10Y` | `36e6750a78e704b337acc2ffbbea9e8fe25a32667440b0ae3a48cd7a6b150b46` | `liwei_0616_10y01_full_oos_k3_div_k10` | `liwei_0616_10y02_cons_say_k3_div_k5` |
| `liwei_0616_5y_v31` | `5Y` | `e57f30153a8758899e858c9f3dec1d057e91905b33c70d38a7097ff34b05c6eb` | `liwei_0616_5y01_full_oos_k3_div_k10` | `liwei_0616_5y01_full_oos_k3_div_k10` |
| `liwei_0616_5y_v31` | `5Y` | `e57f30153a8758899e858c9f3dec1d057e91905b33c70d38a7097ff34b05c6eb` | `liwei_0616_5y01_full_oos_k3_div_k10` | `liwei_0616_cons_sda_k3_div_k10` |
| `liwei_0616_5y_allk10_auc_static_v1` | `5Y` | `a1f802eb3695871d100cd0a37908a3a8959090031c7ab1890dfb4bc05a4f4611` | `liwei_0616_5y_auc_static_all_k3_div_k10` | `liwei_0616_5y_auc_static_all_k3_div_k10` |
| `liwei_0616_5y_allk10_auc_yearly_v1` | `5Y` | `4e0b82d89e1d95115e9f8bee2599d0e7e59be994a399ac6836935ebab61c84dc` | `liwei_0616_5y_auc_yearly_all_k3_div_k10` | `liwei_0616_5y_auc_yearly_all_k3_div_k10` |
| `liwei_0616_5y_allk10_ic_yearly_v1` | `5Y` | `8d0a73fe7b3805529f760adc1260e44f28f34c07509a8481eb9ca9927b6725f0` | `liwei_0616_5y_ic_yearly_all_k3_div_k10` | `liwei_0616_5y_ic_yearly_all_k3_div_k10` |
| `liwei_0616_7y01_v31` | `7Y` | `9fbe93b5ebdbcc9097222c4e9f1278af1dda34ee694bab1db3eb98701f7adb6c` | `liwei_0616_7y01_cons_say_k3_div_k10` | `liwei_0616_7y01_cons_say_k3_div_k10` |
| `liwei_0616_7y03_v31` | `7Y` | `234b4851c1f575fd3e735cf0cc752ee66a0bd731c04a714322b10949a3c970b4` | `liwei_0616_7y03_cons_all_k3_div_k8` | `liwei_0616_7y03_cons_all_k3_div_k8` |

publisher identity、consumer identity、family、tenor、baseline config、窗口、
horizon、purge gap 和 spec fingerprint 任一不一致都拒绝共享。

### 6.2 有效输入投影

schema 3 的 input state 必须包含
`liwei-0616-auxiliary-dependency-projection-v1` 有效输入投影：

- adapter 只通过自身 Native core 的精确 alignment/feature 纯函数生成投影；共享
  cache 不复刻算法，也不 import 任一方案 core；
- 只保留实际使用的 weekly/monthly 列和原列序，使用真实 explicit/fallback
  `date_to_week`、legacy weekly placement、上一自然月 monthly alignment、ffill
  与原变换；
- 严格裁剪到 daily grid 与 `feature_date`，日期唯一且单调；有效字段不 round；
- proof 精确绑定 projection ABI、proof-file bytes、columns/dtypes、
  `date_to_week` entries/hash/mode、daily grid、feature cutoff 和内容 SHA-256。

未使用字段或尚未映射到 daily grid 的未来周/月变化不得让历史前缀失效；proof、
schema、alignment、有效字段或映射漂移则 fail-closed。

### 6.3 generation、lineage 和增量模式

- generation 不可变；manifest schema 3 记录 `parent_generation_id`、input
  content ID、spec fingerprint、build mode、逐 baseline 文件 hash 和完整内容 hash。
- 读取 current 必须从真实文件重放 parent lineage、rehash manifest/payload，并
  复核 input diff 与 build mode；只相信 JSON 指针或自报 hash 不足以验收。
- 同 spec 与 input lineage 下覆盖范围单调：新 generation 的 `test_dates` 必须
  包含 parent 全部有效日期。窄请求只能读取子集，不能发布窄 generation。
- `hit`：投影、input、spec、lineage 和 requested coverage 全部一致，零训练。
- `append`：已验前缀不变，只训练新增 requested dates，保留 parent 全部结果。
- `suffix`：从最早受影响日 `D` 起重算 `[D, current]`，`D` 之前逐 hash 保持；
  影响落入全局 IC screening 区时保守扩为该 family 全量重算。
- 缺 parent、文件损坏、无法确定影响日或证明漂移，只允许该 family 受控 full
  rebuild 并告警，不允许旧 schema 或不完整 lineage 被静默接管。
- staging 写完、rehash、完整性验证和父代复核后，才以原子 `current.json`
  replace 发布；失败、kill、磁盘不足或 `fsync` 失败时 current 不变。

非 publisher consumer 必须零写：不得创建 family root、获取 prewarmer 写锁、
训练、stage、publish、切换 pointer 或清理 generation。它只能返回同一已验
generation 的只读 `hit`；缺失、输入漂移或覆盖不足时返回稳定的
`CACHE_PUBLISHER_REQUIRED`，等待唯一 publisher。

## 7. Blackbox V2 与生产授权

Blackbox V2 仍只接受两文件交付和 Contract 1.0 CLI。自动 Gate、active Registry
或 gray 标签都不自动授予生产运行权限；每个方案必须完成自己的 generation、
确定性、超时、截止隔离、结果结构、失败恢复和标准结果核验，并取得专项授权。
某个方案或批次的授权不得外推到其它 identity/version/runtime。

8 个 V2 只从 occurrence execution envelope 读取冻结 identity 和当天
DataBridge generation，不重新 discovery target，不使用 legacy preflight
credential，也不因前一个 V2 失败而阻塞后一个。

## 8. 待执行的上线与持续运行门禁

切换前必须验证以下可直接保护真实日批的条件；清单存在不表示已经执行：

1. 生产 schema history 经 canonical CLI 验证到 018，包含 APPLYING recovery、
   database/server identity 和 release manifest；
2. machine-global root-owned append-only epoch chain 与三服务显式 mode 一致；
   installed backend、scheduler、v2-preflight 三份 plist 必须一起切为 `ledger`，
   epoch 发布前按精确 label 逐份核对；切换或回滚只能在全局 quiescence 后
   追加更高 epoch，不能删改历史或复用旧 epoch；
3. backend 与单一 scheduler 启动后只存在一个 coordinator owner；ledger 下 V2
   preflight 继续 bootout/未加载，即使其 installed plist mode 已同步为 `ledger`；
4. Native/DataBridge generation、storage root、manifest、input cutoff 和 hash 全部
   通过安全验证，不允许旧 generation fallback；
5. Liwei schema 3 的 exact family/spec/publisher/consumer、parent lineage、单调
   coverage、hit/append/suffix 和 consumer 零写全部通过；
6. 25 个 execution 按 Native 2 / V2 2 并发上限运行，最终 29/29 signal receipt；
7. run fence、ProcessStartGuard、原子提交、08:00 write-once SLA 和 08:30 cutoff
   均无绕路；
8. 每个 Blackbox V2 identity 具备自己的专项生产授权和安全 input/runtime 验证。

仓库 rollout 文件只用于安全 bootstrap，不能覆盖 machine-global epoch。受控切换
先 bootout legacy preflight、scheduler 和 backend，确认 refresh/writer/算法进程
全部退出，再应用 migration、发布更高 epoch，并只按 backend、单 scheduler 启动；
ledger 下 preflight 保持 bootout。任何双 owner、双 occurrence 或绕过 run fence 的
状态都必须 fail-closed。

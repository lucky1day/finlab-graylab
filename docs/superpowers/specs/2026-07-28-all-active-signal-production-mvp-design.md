# 全部 Active 方案实盘信号 MVP 设计

**文档状态**：`CURRENT`

**核验日期**：2026-07-28

## 目标

在当前 Mac Studio 上复用现有 daily ledger 协调器，先完成以下最小生产能力：

1. 只补齐 `2025-01-01` 以来已有方案的真实信号缺口，不全量重跑；
2. 从首次获授权并完成 ledger epoch 切换后的交易日起，全部到期方案由
   自动调度写入 `scheduled_live`；
3. 日频 25 个 execution / 29 个 target 在 08:00 前全部可见；
4. 周频和月频按各自自然频率执行；非到期日只检查最近一期，不制造重复信号；
5. 保留 gray/formal 业务标签，但两者都承担自动实盘任务和缺失告警。

“44 个方案”指 44 个 active composite Registry target，对应 40 个 base
execution：

| 范围 | Execution | Target |
|---|---:|---:|
| 日频 | 25 | 29 |
| 周频 | 7 | 7 |
| 月频 | 8 | 8 |
| 合计 | 40 | 44 |

## 日频精确身份矩阵

下表是MVP policy必须逐项相等的25 execution / 29 target闭集。`tenors`
使用逗号分隔，机器契约测试会解析本表并计算target总数。

| base_scheme_id | horizon | tenors | admission/input |
|---|---:|---|---|
| `daily_10y_lgbm_10y04_0629` | 1 | `10Y` | `live_source_0629` |
| `daily_1y_xgb_1y13_0629` | 1 | `1Y` | `live_source_0629` |
| `daily_5y_lgbm_5y10_0629` | 1 | `5Y` | `live_source_0629` |
| `t1_daily` | 1 | `5Y,10Y` | `generation_v1` |
| `daily_5y_2_v28` | 5 | `5Y` | `generation_v1` |
| `daily_7y_1_v28` | 5 | `7Y` | `generation_v1` |
| `liwei_0616_10y01_cons_say_k3_div_k10` | 5 | `10Y` | `generation_v1` |
| `liwei_0616_10y01_full_oos_k3_div_k10` | 5 | `10Y` | `generation_v1` |
| `liwei_0616_10y02_cons_say_k3_div_k5` | 5 | `10Y` | `generation_v1` |
| `liwei_0616_5y01_full_oos_k3_div_k10` | 5 | `5Y` | `generation_v1` |
| `liwei_0616_5y_auc_static_all_k3_div_k10` | 5 | `5Y` | `generation_v1` |
| `liwei_0616_5y_auc_yearly_all_k3_div_k10` | 5 | `5Y` | `generation_v1` |
| `liwei_0616_5y_ic_yearly_all_k3_div_k10` | 5 | `5Y` | `generation_v1` |
| `liwei_0616_7y01_cons_say_k3_div_k10` | 5 | `7Y` | `generation_v1` |
| `liwei_0616_7y03_cons_all_k3_div_k8` | 5 | `7Y` | `generation_v1` |
| `liwei_0616_cons_sda_k3_div_k10` | 5 | `5Y` | `generation_v1` |
| `t5_daily` | 5 | `3Y,5Y,7Y,10Y` | `generation_v1` |
| `one_y_t5_liq_excess_a_v1` | 5 | `1Y` | `ledger_formal` |
| `one_y_t5_liq_excess_a_w252_l7_v1` | 5 | `1Y` | `ledger_formal` |
| `one_y_t5_liq_excess_a_w350_l7_v1` | 5 | `1Y` | `ledger_formal` |
| `one_y_t5_liq_excess_b_w252_l7_v1` | 5 | `1Y` | `ledger_formal` |
| `ten_y_t5_maj3_k3_ic_static_v1` | 5 | `10Y` | `ledger_gray` |
| `ten_y_t5_maj4_k3_ic_static_v1` | 5 | `10Y` | `ledger_gray` |
| `ten_y_t5_maj4_k3_ic_yearly_v1` | 5 | `10Y` | `ledger_gray` |
| `ten_y_t5_say_k5_sharpe_static_v1` | 5 | `10Y` | `ledger_gray` |

## 已确认的业务语义

- 系统每天巡检全部 44 个 active target。
- 日频在每个交易日产生新信号。
- 周频、月频只在各自到期日产生新信号。
- 当前 gray 方案未来也写 `scheduled_live`；历史 `gray_live` 保留且不改写。
- gray 仍是模型业务标签，不再表示“禁止自动执行”。
- 任一到期 gray 或 formal target 缺失，都使当日应有集合不完整并触发告警。
- 补历史漏跑不得伪造成 `scheduled_live`，只能通过受控补缺路径写
  `gray_live` 并保留原失约事实。
- 禁止旧 Native/DataBridge generation fallback。

## 当前数据基线与缺口计划器

只读 `signal-gap-plan` 已实现。`--as-of` 是包含当日的确定性上界：
只纳入 `predict_date <= as_of_date` 的应有和已观测事实，不表达数据 vintage，
也不会产生任何数据库写入。

以 `--start 2025-01-01 --as-of 2026-07-27` 冻结 44 个 active target
后，当前权威快照为：

- `44 active / 11,652 expected / 11,623 present / 29 open`；
- canonical 回测完整，共 10,309 条，无需全量重跑；
- live 应有 1,343 条、已有 1,314 条，共缺 29 条；
- 缺口为日频 24、周频 5、月频 0；
- 现有 backtest/live 无日期重叠、重复业务键、孤儿 run 或 phase 漂移。

在合法 DataBridge current 更新至 `refresh_date=2026-07-28` 后，同一冻结
缺口重新分类为：

- 25 `GRAY_LIVE_GAP`；
- 0 `BLOCKED_NO_GENERATION`；
- 4 `BLOCKED_DATA_CONTRACT`。

这 29 个 gap 尚未写入。计划器只生成内容寻址的只读执行计划；业务实施
阶段 6（本计划 Task 7）才执行受控补缺，且不得把历史漏跑倒签为
`scheduled_live`。

补缺以业务唯一键
`base_scheme_id + target_tenor + horizon + target_date` 判断：

- 已存在：`SKIP_PRESENT`；
- live 段缺失且输入权威完整：`GRAY_LIVE_GAP`；
- 历史 canonical 段缺失：`FULL_CANONICAL_RUN_REQUIRED`，重新生成完整
  canonical run，禁止原地插一行；
- 没有合法 generation：`BLOCKED_NO_GENERATION`，禁止降级；
- 数据、Registry、观测或输入 authority 违反契约：
  `BLOCKED_DATA_CONTRACT`，必须先修根因。

完整 action 闭集为：
`SKIP_PRESENT|GRAY_LIVE_GAP|FULL_CANONICAL_RUN_REQUIRED|BLOCKED_NO_GENERATION|BLOCKED_DATA_CONTRACT`。

## 唯一执行架构

不新增第二套轻量队列。MVP直接扩展并启用现有 daily ledger：

```mermaid
flowchart LR
    A["06:30 单一 daily coordinator"] --> R["冻结 active daily 25/29"]
    R --> N["Native generation"]
    R --> D["同日 DataBridge refresh/generation"]
    N --> NP["17 Native，最大并发 2"]
    D --> VP["8 Blackbox，最大并发 2"]
    NP --> C["ledger 原子提交"]
    VP --> C
    C --> W["07:45 缺失检查"]
    W --> S["08:00 29/29 write-once SLA"]
```

现有 ledger 能力继续作为不可绕过边界：

- 单 owner 文件锁；
- 同日 occurrence 幂等复用；
- item claim、attempt fence 和孤儿清理；
- Native/V2 独立执行池；
- 单方案失败隔离；
- `TRANSIENT_INFRA` 最多自动重试一次；
- prediction、run、item、target receipt 原子提交；
- 两分钟 recovery tick；
- 08:00 `MET|BREACHED` write-once。

## 调度与数据就绪

- 06:30 只是 `not_before`。
- 先检查交易日历和 T-1 必需数据；未齐备时不创建算法 attempt。
- 数据齐备后并行构建 Native generation、刷新当天 DataBridge。
- Native 只在 Native generation 就绪后释放。
- Blackbox 只在同日 DataBridge generation 就绪后释放。
- 八个日频 Blackbox 使用同一 generation，并按固定
  `+0/+2/+4/+6/+8/+10/+12/+14` 分钟释放；后一个不等待前一个成功。
- Native/V2 最大并发均为 2，不自动扩展第三个重任务。
- 07:45 保留现有 V2 start guardrail，并新增全25 item进度投影：
  未启动、未完成和当前缺失target分别列出；只补救可恢复项。
- 08:00 少一个日频 target 即 `BREACHED`；晚到可改变 completion，但不能
  把 SLA 改回 `MET`。
- 08:30 后不启动新的自动 attempt。

## Admission 决策

Blackbox exact identity admission 继续保留 `formal|gray` 标签，但自动
执行权限必须按控制面分开：

- identity 缺失、version 漂移或 runtime 漂移仍 fail-closed；
- legacy per-scheme入口继续只允许既有formal，绝不因gray进入ledger而
  注册旧日频cron；
- 四个gray daily只允许进入ledger的25/29冻结policy；
- daily policy 从 21/25 扩展为 25/29；
- 五个gray monthly随后通过独立recurring admission在自然月频到期日
  自动写`scheduled_live`，不得复用daily ledger布尔判断；
- 四个 gray daily 从本次切换起进入 29/29 日频硬验收；
- 不因自动执行而把业务标签自动改成 formal。

capacity admission 采用用户确认的直接准入，不再把 20+20 或连续十日作为
MVP上线阻断。完整 `ADMITTED` 必须绑定精确：

- Mac identity；
- 生产MySQL identity/schema和隔离rehearsal identity；
- commit、scheme/version、policy 和环境摘要；
- 一次隔离MySQL真实forced-cold 25/29同机rehearsal；
- migration018后使用production audit-readonly source和受保护隔离sink生成
  的production-identity execute-only observation；
- 29/29 原子落库、重入无新增、无孤儿进程；
- 最后 target 不晚于 07:55，完整运行不超过 85 分钟；
- migration、storage、epoch 和 installed plist 预检。

20+20、故障扩展采样和连续稳定性转为上线后增强项，不影响本次
`ADMITTED`；同时不得把一次样本表述为统计 P95 证明。

完整admission仍保留现有签名、`not_before/expires_at`、decision
sequence和replay floor。这里的“完整”表示不引入临时或降级状态，不表示
取消最长31天的密码学有效期。

三个0629 live-source compatibility方案继续作为MVP显式兼容身份；每个
方案必须独立证明只读连接、数据水位、版本和结果契约，不能仅凭聚合29/29
计数清除阻断，也不能在失败时降级到其他输入。

## 切换边界

- `legacy` 与 `ledger` 不得同时写日频。
- 切换时只关闭旧 per-scheme 日频任务；周频、月频继续由现有自然 cron
  调用，但 gray/formal Blackbox 均受 exact identity admission 保护。
- backend、scheduler、v2-preflight 的 installed mode 必须一致。
- 必须先停止 BFL 服务、确认没有相关子进程，再发布 machine-global
  ledger epoch。
- 不修改、重启或迁移 BondProjectPro。
- 任一生产前置失败时保持 legacy，不做半切换；允许受控人工补缺，但不得
  宣称自动化已经上线。
- 计划日期2026-07-28是决策和目标日期；若ledger epoch未在当天业务运行
  前合法发布，当天漏跑只能补`gray_live`，正式`scheduled_live`起点顺延
  到实际切换后的首个交易日。

## 非目标

本MVP不包含：

- Redis、Kafka、Celery、多机 worker；
- 新增 per-scheme cron；
- 算法窗口、特征、模型、投票或 fallback 修改；
- 三个 0629 generation adapter 重构；
- migrations 019/020、长期归档和磁盘治理；
- 20+20、连续十日作为发布阻断；
- 远程推送、生产部署或 BondProjectPro 变更的隐式授权。

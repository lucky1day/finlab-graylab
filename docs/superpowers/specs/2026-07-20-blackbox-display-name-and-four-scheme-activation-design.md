# Blackbox V2 短展示名与 1Y T+5 四方案激活设计

**文档状态**：`APPROVED_DESIGN`

**决策日期**：2026-07-20，`Asia/Shanghai`

## 1. 目标

本次变更同时完成两个目标：

1. 为 Blackbox V2 明确“算法身份名”和“任务格子展示名”的边界，避免在已知目标和任务类型的格子内重复显示标的、周期和方向预测说明。
2. 将已经完成 Shadow 技术入库的四个 `1Y + T+5` 方案全部激活，并为每个方案完成 100 条持久化回测、单次 `gray_live`、API 和前端验收。

四个任务格子展示名固定为：

| base scheme ID | 任务格子展示名 |
|---|---|
| `one_y_t5_liq_excess_a_v1` | `LIQ_EXCESS_A` |
| `one_y_t5_liq_excess_a_w252_l7_v1` | `LIQ_EXCESS_A_W252_L7` |
| `one_y_t5_liq_excess_a_w350_l7_v1` | `LIQ_EXCESS_A_W350_L7` |
| `one_y_t5_liq_excess_b_w252_l7_v1` | `LIQ_EXCESS_B_W252_L7` |

四个方案继续归属同一个 `1Y国债活跃 × T+5` 任务格子。

## 2. 已确认的命名来源

当前长名称不是前端硬编码。现有上游 Metadata 的 `name` 为类似：

```text
1年期国债收益率T+5日方向预测（LIQ_EXCESS_A_W252_L7）
```

平台将该名称写入 Registry；历史回测 API 又生成：

```text
{Registry name} · {target label}
```

因此任务格子内出现了“目标、周期、方向预测和标的后缀”的重复信息。

## 3. 方案选择

采用方案 A：平台显式展示名覆盖。

- 当前交付的 `.py/.json` 已进入版本和 Gate 证据链，保持字节不变。
- Blackbox `config.yaml` 增加可选 `display_name`，只控制业务展示，不参与算法 canonical config hash 或 `scheme_version`。
- `scheduler.discovery` 优先使用平台 `display_name` 作为 Registry `name`；未配置时继续使用 Metadata `name`，保证已有方案兼容。
- Registry 仍通过正式同步入口更新，不直接执行临时 SQL 修改。
- 前端在已经明确 `target_tenor + task_type` 的任务格子内使用 `scheme_name`，不再使用附加 `· target_label` 的通用 `display_name`。
- 回测 API 保留原有 `display_name` 字段，避免破坏其它只读消费者；任务格子前端选择更符合上下文的 `scheme_name`。

未采用的方案：

- 只改 Registry 名称：仍会得到 `LIQ_EXCESS_A_W252_L7 · 1Y国债活跃`，没有完全消除格子内冗余。
- 前端从全角括号中提取短名：依赖上游自由文本格式，无法形成稳定契约。

## 4. 上游 SOP 规则

未来交付的 Metadata `name` 必须是方案的简洁业务识别名，不重复已经由 Metadata 其它字段表达的内容：

- 不重复 `target_tenor`，例如不写“1年期国债收益率”；
- 不重复 `task_type/horizon`，例如不写“T+5日”；
- 不追加“方向预测”“模型方案”等通用说明；
- 同一任务格子内必须能用 `name` 区分候选方案；
- 推荐与稳定的业务方案代号一致，例如 `LIQ_EXCESS_A_W252_L7`。

SOP 示例将改为短名，同时明确 `scheme_id` 是执行身份、`name` 是业务展示身份，两者用途不同。

## 5. 平台数据流

```text
上游 Metadata name（审计原名，当前交付不改）
              │
              ├── config.display_name 存在 ──> Registry name（短名）
              │
              └── 不存在 ────────────────> Registry name（Metadata 原名）

Registry name ──> /api/schemes.name
              └──> /api/backtests/factor-lab.scheme_name
                                      └──> 任务格子候选名称
```

`description` 继续保留 Metadata 原名，便于审计追踪。展示名变化不移动算法版本、代码摘要、Metadata 摘要、运行环境或 Gate 绑定关系。

## 6. 激活与写入流程

用户已明确授权四方案全部激活，覆盖原计划中的 Canary 等待条件。执行仍保持逐方案 fail-closed：

1. 为三个尚未激活的方案分别使用当天有效 DataBridge generation 重新执行 `--stage all`。
2. 每个方案分别签发绑定 exact version 和最新 all-stage run 的 `blackbox_activate` token。
3. 每个方案分别签发新的 `backtest_persist` token，持久化恰好 100 条回测和非空月度指标。
4. 每个方案分别签发新的 `live_write` token，显式写入 `prediction_phase=gray_live`。
5. 已激活 Canary 不重复写相同 predict date 的 gray live，只通过正式 Registry 同步更新展示名。
6. 只重启 backend 以刷新 API 和静态前端；当天仍不重启 scheduler，避免 cron 已过后的 startup catchup。

任一方案失败时，只停止该方案及后续生产动作，不修改上游算法来贴合结果，不复用其它方案 token，也不直接手改 Registry 生命周期状态。

## 7. 测试与验收

代码和契约测试必须覆盖：

- `display_name` 缺失时兼容 Metadata `name`；
- `display_name` 非空校验；
- 增加或修改 `display_name` 不改变 Blackbox canonical config hash 和 `scheme_version`；
- Registry 正式同步使用短名，description 仍保留上游原名；
- 任务格子候选表、选中方案摘要和详情标题只显示短名；
- 其它任务格子仍正常渲染，API 的通用 `display_name` 字段保持兼容；
- 上游 SOP 文档测试守护短名规范和禁止冗余示例。

生产验收必须满足：

- 四个 config、exact version 和 composite Registry 均为 active；
- 四个方案各有 100 条持久化回测和至少 1 条 `gray_live`；
- `/api/schemes` 恰好包含这四个新方案，不包含被排除四方案；
- `1Y国债活跃 × T+5` 格子显示四个候选，名称恰好为本设计第 1 节的四个短名；
- 页面不显示内部 scheme version，控制台错误为 0；
- 公网只读接口通过，管理与写接口继续返回 403；
- 保存四候选完整截图并更新批次 Markdown、机器证据、台账和当前状态。

## 8. 后续时间门槛

全部激活不改变自然调度与 actual 的时间约束：

- 下一交易日 DataBridge 刷新成功后、`07:03` 前重启 scheduler，再观察四方案自然写入 `scheduled_live`；
- `target_date=2026-07-24` 到达并完成 actual 刷新后，再验收 actual join、正式 API Gate 和准确率展示；
- actual 未到时保持 pending，不人工补写。

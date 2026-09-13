# 运行信号与调度治理

**文档状态**：`CURRENT`

本文定义自然 Writer 权属、现场证明、手工恢复和停止条件。期望日历、模板、release 与迁移恢复见
[部署手册](../../deploy/README.md)，主机连接见[访问入口](../operations/DEPLOYMENT_ACCESS.md)，
已核验基线与未完成观察分别见[当前状态](../CURRENT_STATUS.md)和[TODO](../TODO.md)。

## 唯一控制面与现场证明

每个部署目标和数据库 authority 内，每个 cadence 只有一个自然 Writer：Mac3 使用 launchd + installed
plist，ECS 独立灰度使用 systemd + installed unit/timer。Python runner 只是宿主调用的一次性执行器。
预检、手工调用或环境标记不能单独证明自然触发；退役控制面禁止重建，见[根规范](../../AGENTS.md#环境与发布)。
历史调度数据库对象仅用于审计或受控 recovery，不能重新取得 Writer 权限；物理清理须单独设计和授权。

自然运行须联合核对：

1. 仓库期望模板、installed 配置、loaded state 和真实触发时间；
2. 进程实际 cwd 与精确 release，启动环境及部署目标；
3. 同次日志、run、prediction、exact、三个日期及本机输入来源；
4. Dashboard 与已发布事实一致；它不携带 exact，不能代替数据库版本读回。

只有上述证据一致才可标记自然运行成功；仅有配置文件、环境标记、灰度补缺或人工模拟均不足以证明。
DataBridge 的 `BFL_DATABRIDGE_PRODUCER` 在 Mac3 为 `launchd-one-shot`、ECS 为 `systemd-one-shot`；
它只是防误 publish 的准入标记，不是宿主身份认证。同 UID 调用者属于受信任边界，仍须上述现场证据。

自然候选先按部署矩阵与 cadence 过滤，再解析本机生效生命周期，见[共享生命周期契约](SCHEME_CONTRACT.md#7-生命周期与分派)。Blackbox 以本机 exact version 与 composite Registry 为准，canonical 的初始 paused/draft 不能提前排除已激活方案；W4 Native 仍使用固定配置状态。收盘预规划与 one-shot 执行复用同一解析规则，执行前与事务内分别复核；数据库不可读或身份不一致时停止，不回退文件状态。不维护第二份 Admission、release queue、mode 或 capability 权限矩阵。

close-period 复用原 monthly 控制面，按[预测语义](PREDICTION_SEMANTICS.md)选择到期月中收或周期均值任务，
普通日期 no-op；期望时钟见部署手册，不能因存在 active 方案推断当天必须执行。

## 自然信号与手工恢复

自然触发的合格写入使用 `scheduled_live`；受控历史补缺使用 `gray_live`。这两者是 run 来源审计，
不得互相伪装或以日期标签替代 provenance。产品历史/实盘分界及不可变回测事实见预测语义。

历史缺口只经[平台 SOP 的单日/区间入口](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#5-可选后续动作)修复；支持范围、参数、跳过/重复键行为和输入绑定都在那里维护。先确认授权覆盖的方案与日期、已发生副作用及实际剩余缺口，不把一次失败当作全部未写入。

当天自然 one-shot 部分失败后的受控 `scheduled_live` 重试可向现有 launchd/systemd runner 重复传入
`--scheme-id <base_scheme_id>`，只缩小 active cadence 候选，不绕过部署、Registry、exact、日历、输入或
insert-only 检查。runner 的 `--predict-date` 不是第二套历史补缺授权，不能把历史点重标自然运行。
维护跨越正式触发点时须核对是否已执行；遗漏历史点走上述受控补缺，不伪造时钟证据。

普通 active completion 的 benign `skipped` 是算法已经执行、返回 records 并通过写前复核后产生的发布结果，不是调度 preflight skip，计算成本已经发生。one-shot batch 的退出码 `0` 仅表示没有 actionable failure，同一摘要可以同时包含首次发布 `success` 与完整重复 `skipped`，不能据此声称没有执行方案。区间补缺拒绝已有键的语义仍按平台 SOP，不转换为此类 skipped。

## 输入新鲜度

算法执行前必须通过本机输入 ready 与截止校验；失败不得降级旧 artifact 或制造成功信号。源表、schema、
连续性、稳定轮次和发布身份的完整检查见[DataBridge](../blackbox_v2/data_bridge_v1/README.md)。

close-period 预测前核对 current ready 是否精确覆盖所需锚点：覆盖则月中收与周期均值顺序复用同一已发布快照；
未覆盖只做一次收盘刷新再核验，失败或截止不符则该批预测零执行。它是原 monthly 入口前置动作，不新增
timer 或第二 DataBridge Writer。

## 授权、生命周期与停止条件

变更前按根规范取得对应授权，并先核对本页列出的现场证据、在途任务和恢复范围。

Blackbox 的 Intake、同 exact/当前校验策略持久化回测与激活条件见[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)；
W4 固定版本的运行检查与故障恢复见[W4 运行手册](../sop/NATIVE_V1_MAINTENANCE_SOP.md)，不再进入 Native 新版本准入。
技术 Gate 不等于安装控制面或自然观察。DB 生命周期成功后仍须完成目标机调度与产品读回。

以下情况停止副作用、保留证据并说明需确认的事项：

- active scope、exact、Registry identity、输入截止或日期语义与预检不一致；
- installed/loaded、日志、run 和 prediction 无法证明同一次自然运行；
- 两个 Writer 可能写同一业务键；
- 需要 fallback、未授权切旧版本、覆盖、自动重试或恢复已退役控制面才能继续。

恢复源码、schema 和外置状态前执行[部署恢复](../../deploy/README.md#回滚与认证部署)的兼容性与备份检查。

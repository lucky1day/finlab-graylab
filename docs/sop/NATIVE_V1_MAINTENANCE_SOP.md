# W4 Native 固定版本运行维护

**文档状态**：`CURRENT`
**适用运行时**：Mac3 W4 九套现存 `native_adapter`
**目标读者**：运行维护人员

本 SOP 只保障 W4 固定版本的输入、执行依赖与现有日/周/月调度；退役能力和版本边界以[根规范](../../AGENTS.md#算法与数据不变量)为准。后续版本走 [Blackbox 平台 SOP](BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)，不自动迁移 W4，也不授权删除原件、历史或生产资源。

## 1. 运行范围与基线

从[当前状态](../CURRENT_STATUS.md)、[部署矩阵](../../deploy/scheme_deployment_matrix_v1.json)和 Mac3 现场核对九套 W4 的 base ID、exact、Registry target 与状态。现存[Native 政策清单](../../deploy/onboarding_policy_v1.json)保留实现兼容用途，不再表示允许修订或激活 Native 版本。

保留现有 canonical、adapter/core、source package、动态入口、解释器及执行依赖；接口和配置见[Native 运行契约](../native_v1/SCHEME_CONTRACT.md)。W4 使用自己的 `legacy_db` 输入，不能改接 Blackbox DataBridge、恢复已迁移方案的旧缓存，或部署到 ECS。

操作前先按[部署访问入口](../operations/DEPLOYMENT_ACCESS.md)识别 Mac3，核对受影响方案、在途任务、下一触发及恢复边界。只读观察不能顺带执行算法、补缺、状态变更或服务重启。

## 2. 日常调度核验

按[调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)核对 installed plist、loaded state、实际命令、进程环境、日志路径和日/周/月触发规则；不以仓库模板存在或“每天检查过”代替对应 cadence 的运行结果。

每个应触发点核对本机日志、真实 `scheduled_live` run、prediction、当前 exact、输入截止与 target 集合；三个业务日期、Actual 和统计按[预测语义](../architecture/PREDICTION_SEMANTICS.md)。再按[Dashboard 验收](../operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#认证响应与合同验收)核对页面与本机事实。非到期跳过不算漏跑，模拟和补缺不算自然运行。

## 3. 输入与作业隔离

scheduled one-shot 与单日 gap-fill 使用作业级临时输入根目录。同一作业内，只有 frequency、运行日、起止日期/周、as-of、schema columns、data version 和数据库源类型全部一致的 builder 调用才复用只读 CSV；每套方案从自己的只读硬链接路径读取。作业结束或中断后清理临时目录，不向 `backtest_artifacts/runtime_inputs` 累积日常输入。

单方案成功后独立提交，后续失败不回滚已完成方案；重试按 insert-only 业务键只规划剩余点。中断使用既有 process-control 终止已启动进程组并关闭未完成 run，不新增调度器、任务表或审计字段。这些隔离行为不改变算法、三日期、exact 或标准结果。

## 4. 故障定位与恢复

先区分宿主调度未触发、解释器或依赖不可用、输入/日历失败、算法执行失败和展示问题。读取已有日志与 run/prediction，核对当前 release、输入路径及必要 source package；不得吞掉异常或将失败包装成平。正常完成后无信号的边界见[补平规则](../architecture/SOURCE_ALGORITHM_FIDELITY.md#22-正常完成后的无信号补平)。

固定 Native 版本不禁止恢复其运行基础设施：环境、依赖、服务及调度故障按[部署手册](../../deploy/README.md)和调度治理，在明确授权后修复并核验。漏跑使用现有受控恢复入口，保留成功事实，不重算、覆盖或删除已有业务键。需要改算法、canonical、adapter/core 形成 Native 新版本，或重新激活时停止该路径；后续版本按 Blackbox 合同另行交付。

## 5. 完成条件

受影响 W4 仍保持原版本、身份、输入和执行依赖；本次授权修复通过对应运行检查，且未改变其他方案或已发布事实。自然运行完成须有相应真实时钟的日志、run、prediction 与 Dashboard 一致性；只完成环境修复、模拟或补缺时分别报告，不能推定自然运行通过。

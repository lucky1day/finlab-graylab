# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-10

本文只记录当前稳定事实。实时方案、run、prediction、DataBridge、API 和 launchd 状态必须从各自权威数据源读取，不在仓库文档冻结数量、运行 ID 或 Git SHA。未批准工作见[统一后续推进计划](TODO.md)，生产调度规则见[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 当前生产规则

- `launchd + installed plist` 是唯一自然生产调度控制面；仓库模板、代码和测试不能单独证明现场已加载或已运行。
- config active、exact version active、Registry target active 且 cadence 匹配，是进入对应 one-shot runner 的唯一资格。
- 自然运行写 `scheduled_live`；单日人工补缺只经 `python -m harness signal-gap-fill --predict-date YYYY-MM-DD` 写 insert-only `gray_live`。
- Blackbox Admission、Backend 手动预测、direct scheduling、ledger、occurrence、epoch、daily-gray 和常驻 APScheduler 均已退役。
- installed plist、launchctl、服务、激活、持久化回测、业务写入和 DDL 仍是独立生产操作，必须获得明确授权。

## 当前输入权威

- Native 只使用当前权威 `bond_db`，按 `predict_date` 推导的 `feature_date` 截止，经 `shared.input_artifacts` 构建输入。
- source-backed Native 连接同一 MySQL 实例和同一 `bond_db`，仅使用独立 SELECT-only 身份；它不是第二套数据库或输入链路。
- Blackbox 使用 DataBridge current snapshot；历史补缺严格绑定冻结的 DataBridge authority。
- Native 与旧 DataBridge 的二级 generation 运行控制面已退役。`t_input_generations`、migration 017 和历史行仅保留为历史 schema/审计证据，不再参与 Native 运行资格或输入构建。
- Phase-A manifest 继续兼容 `native_generation=null`、`native_generation_changed=false` 和 `NON_PRODUCTION`，不再支持 bind/rebind。
- Phase-A 只复用 manifest v3 current generation；current 缺失时由 publisher 按当前输入完整重建，旧松散 v1 `.pkl` 不再作为迁移来源。

## 当前文档与评审边界

- 当前规则只由根规范、CURRENT 架构/契约、Onboarding、SOP、生产准备清单和运维/产品手册定义。
- 已实施计划、已关闭交接和被后续规则替代的决策草案不保留在工作树；需要追溯时使用 Git、Harness、run/prediction 和数据库审计。

## 当前 Harness 合同

- 首次技术入库 `all` 固定为六段 `static -> input -> unit -> dry-run -> compare -> backtest`；`native-maintenance` 固定为五段 `static -> native-maintenance-admission -> input -> unit -> dry-run`。两者都不访问 Backend。
- 激活后只用 `dashboard` Gate 校验 `/api/factor-lab/dashboard` 的当前业务快照；该 payload 不携带 exact version，因此不能用来证明 exact version。
- `signal-gap-fill` 只支持单个 `predict_date`，可选限定一个 base scheme；命令执行权本身就是补数授权，不另设用户、token、确认或 plan SHA 层。Native 从当前数据库重建，Blackbox 重放冻结 DataBridge authority；所有算法成功后才按 group insert-only 写 `gray_live`，最后执行一次权威读回。
- Blackbox lifecycle 存在 pending 时直接阻断后续操作；只有显式、独立 HMAC 授权的 `lifecycle-reconcile` 可修改状态，其它 Gate 不做隐式恢复。

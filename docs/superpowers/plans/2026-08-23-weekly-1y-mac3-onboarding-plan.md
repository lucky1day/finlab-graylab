# Weekly 1Y 方案 Mac3 完整入库执行计划

> 执行方式：按 `subagent-driven-development` 分任务实施和复审；生产 CAS、服务重启、Harness 副作用与数据库写入只由主 agent 执行。

**目标：** 将 `weekly_1y_causal_v1_31_0_standalone` 从 ECS-only 扩展为双环境资格，并在 Mac3 建立独立、完整、可审计的入库状态。

1. **固化部署资格和安全初态**

   修改 `deploy/scheme_deployment_matrix_v1.json`，把 canonical lifecycle 恢复为 `paused/draft`，并补精确回归测试。ECS 晋级前先确认其 exact active overlay 和数据库状态，避免 canonical fallback 暂停既有灰度。

   **验证：** 先观察新测试失败，再修改矩阵/config 使 deployment scope 与 lifecycle state 测试通过；`scheme_version` 仍为 `c93f76489d5b`。

2. **补齐 ECS host lifecycle bootstrap（A release）**

   以测试驱动增加显式授权、仅用于“canonical/DB/Registry 已 active 且 overlay 缺失”的一次性固化入口；先单独提交并构建 A release，保持旧 canonical `active/active` 和 ECS-only 矩阵。A 只晋级 ECS，然后从当前 A release 执行 bootstrap。

   **验证：** mismatch、非 active、overlay 已存在、direct operation 作用域不匹配全部 fail-closed；成功只新增 exact `active/active` overlay，并通过独立读回。

3. **创建最终双环境 release（B release）**

   在 A 已完成 ECS overlay 固化后，提交计划、矩阵、安全 canonical 初态及其测试；从 clean detached worktree 构建 B release。

   **验证：** manifest commit、archive SHA、Git tree和可重复构建校验一致；archive 不含 `.git`、运行产物或秘密。

4. **ECS 先行 lifecycle 固化、复验和晋级**

   从候选执行受控 bootstrap，把 ECS 既有 active fallback 固化为 exact host overlay；复制 archive/manifest/候选 installer，预安装；核对环境、DataBridge、现有 active 方案、timer/unit 和目标方案既有数据，然后以 expected-current CAS 晋级并只重启 Backend。

   **验证：** 新 `current`、health、目标方案 API/DB、五个 timer 和最近 service 结果通过；不改 installed unit。

5. **Mac3 候选 Gate**

   用同一 archive 预安装，核对 osx-arm64 Blackbox 环境、DataBridge current 和 deployment discovery，再运行目标方案 exact version 的 `all` Gate。

   **验证：** 四段 Gate `static/input/unit/compare` 全部持久化 passed，记录新的 Mac3 `harness_run_id`。

6. **Mac3 生命周期和历史回测**

   由 `--operator` 直接操作入口生成一次性 `shadow_register` operation，Gate 自动绑定该 run 并登记 paused/shadow；随后以同一模式执行 `backtest_persist`，完成 2025-01-01 至灰度边界前的全量回测。

   **验证：** exact version/Registry 为 shadow/paused；backtest run 成功且 `target_date` 不越过 gray start。

7. **Mac3 source CAS 与激活**

   在无 Writer 运行窗口，用 Mac3 expected-current 做 CAS；只重启 Backend。通过 `--operator` 直接激活入口自动绑定 exact run 并激活 exact version。

   **验证：** `current` 指向新提交，Backend healthy，external lifecycle、`t_scheme_versions` 和 composite Registry 一致为 active。

8. **补齐灰度和前端验收**

   对 `predict_date=2026-08-22` 执行该方案的 authorized signal gap fill，再运行 DashboardGate、API 和浏览器验证。

   **验证：** 恰有一条 insert-only gray_live 预测；API/页面展示新 composite；launchd drift audit 仍 `ok=true`，调度保持自然触发。

9. **最终复审**

   独立复审代码变更、发布身份、DB/API/前端和控制面证据。

   **验证：** fresh commands 全部通过；明确区分“完成入库”和“等待下一次自然周任务形成 Production Observed”。

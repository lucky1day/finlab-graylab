# Blackbox V2 方案 A 机器闭环实施计划

> 执行范围仅限 `codex/audit-bugfixes-20260613`；不触及 `master`、生产数据库、installed plist、launchctl 或运行中服务。

**目标：** 把已确认的方案 A 从 SOP 约定推进为机器可执行合同：正式新 Intake 必须提供 `name`、`owner`、`description`，平台原子登记 composite registry ID 的 owner，历史不可变包继续兼容；StaticGate 与 DashboardGate 对新交付做精确读回。

**设计边界：** `owner` 进入上游 Metadata 与版本哈希；平台 owner registry 仍按 composite registry ID 供 Dashboard 读取。新交付禁止 `config.yaml.display_name`，名称唯一来自 Metadata；历史已入库、缺 `owner` 或带既有 `display_name` 的包不原地改写。实现只强化交付、发现与 Gate，不改变算法、预测、回测或前端显示逻辑。

---

## Task 1：给 Metadata 与 owner registry 建立单一低层合同

**Files:**
- Create: `shared/scheme_owner_registry.py`
- Modify: `shared/blackbox_v2/contracts.py`
- Modify: `backend/scheme_owner.py`
- Test: `tests/test_blackbox_v2_contracts.py`
- Test: `tests/test_scheme_owner.py`

1. 先写失败测试：Metadata 接受合法 `owner`，历史缺失返回 `None`；空白、换行、HTML 标记和 `--` / `unknown` / `待定` 占位值均拒绝。
2. 先写失败测试：owner registry 复用相同 owner 文本合同，并能生成唯一 composite ID `{base}__h{horizon}__{tenor}`。
3. 新建 shared 低层模块，集中 owner 规范化、registry JSON 严格读取、序列化和 composite ID 生成；backend 只保留兼容导入面。
4. 在 `BlackboxMetadata` 增加可选 `owner`。通用解析保持历史兼容，但所有提供的 owner 均严格校验。
5. 验证：
   `python -m pytest -q tests/test_blackbox_v2_contracts.py tests/test_scheme_owner.py`

## Task 2：让正式 Intake 强制三字段并协调提交 owner 登记

**Files:**
- Modify: `shared/blackbox_v2/intake.py`
- Modify: `harness/cli.py`
- Test: `tests/test_blackbox_v2_intake.py`
- Test: `tests/test_blackbox_v2_harness_gates.py`
- Test: `tests/test_blackbox_revision_activation.py`

1. 先写失败测试：缺 `owner` 或缺 `description` 时 Intake 阻断，且 scheme 目录与 owner registry 均不改变。
2. 先写失败测试：成功 Intake 同时生成 paused/draft config（不含 `display_name`）并把精确 composite ID 登记为 Metadata owner；同 ID 同 owner 可幂等预检，同 ID 不同 owner 阻断。
3. 先写失败测试：模拟 owner registry 替换失败、scheme 提交失败，均不得留下半完成的 scheme；后一种情况必须把 registry 原子恢复为原内容。
4. Intake 在复制交付前完成所有验证和 owner 冲突预检；scheme 与 registry 都先写同文件系统临时项，再按“registry 原子替换 → scheme 原子替换”提交；scheme 提交异常时原子回滚 registry。所有临时项均清理。
5. CLI 成功 JSON 回读并报告 `registry_scheme_id`；不再用 warning 放行缺 description。
6. 更新共用测试 fixture，使它们显式模拟正式新交付并提供 owner registry；不改历史 discovery fixture。
7. 验证：
   `python -m pytest -q tests/test_blackbox_v2_intake.py tests/test_blackbox_v2_harness_gates.py tests/test_blackbox_revision_activation.py`

## Task 3：让 discovery 区分新交付与历史兼容包

**Files:**
- Modify: `scheduler/discovery.py`
- Test: `tests/test_blackbox_v2_discovery.py`

1. 先写失败测试：含 owner 的新 Metadata 映射到 `SchemeConfig.owner`，名称严格等于 Metadata name；若同包 config 含 `display_name` 必须失败。
2. 保留历史测试：缺 owner 的既有包仍可使用既存 `display_name` override，且不会被原地改写。
3. 在 `SchemeConfig` 追加可选 `owner`；Native 和历史 Blackbox 默认 `None`。仅当 Metadata owner 非空时启用新交付规则。
4. 验证：
   `python -m pytest -q tests/test_blackbox_v2_discovery.py`

## Task 4：StaticGate 与 DashboardGate 做精确 read-back

**Files:**
- Modify: `harness/blackbox_v2/gates.py`
- Modify: `harness/gates/dashboard_gate.py`
- Test: `tests/test_blackbox_v2_harness_gates.py`
- Test: `tests/test_dashboard_gate.py`

1. 先写失败测试：新交付的 StaticGate 要求 description 非空、owner registry 中精确 composite ID 的 owner 与 Metadata 一致；缺登记或冲突失败。历史缺 owner 包保持兼容。
2. 先写失败测试：新 Blackbox active 方案的 DashboardGate 要求 API 行的 `name`、`owner`、`description` 均非空且逐字段精确等于 config/Metadata；任一空值或偏移失败。Native 现有合同不扩张。
3. StaticGate 通过 shared owner loader 读仓库登记，不依赖 backend 或 DB；DashboardGate 只强化新交付的公开读模型验证，不改变前端/API 显示逻辑。
4. 验证：
   `python -m pytest -q tests/test_blackbox_v2_harness_gates.py tests/test_dashboard_gate.py`

## Task 5：更新 SOP 状态、全量验证并集成

**Files:**
- Modify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
- Rebuild outside Git: `/Users/macstudio0/Desktop/blackbox-v2-upstream-delivery-kit-20260813/`
- Rebuild outside Git: `/Users/macstudio0/Desktop/blackbox-v2-upstream-delivery-kit-20260813.zip`

1. 将 SOP 中“配套机器实现尚未上线”的过渡提示改为已经可核查的现状；保留历史不可变兼容边界，不能写成全量历史 Metadata 已回填。
2. 运行 `git diff --check`、改动 Python 目录 compileall、上述定向测试及完整 `python -m pytest -q`。
3. 复查 diff 只包含方案 A；确认共享工作区无 tracked 改动、无与改动路径重叠的未跟踪文件。
4. 在隔离功能分支提交；回到精确开发分支后以 merge commit 合并，再运行至少 diff/check、定向测试和完整 pytest。
5. 普通 push 到 `origin/codex/audit-bugfixes-20260613`，用 `git ls-remote` 回读 SHA；绝不 force push。
6. 用最终 SOP、`data_bridge_v1_schema.json` 和三份脱敏 sample 重建上游 ZIP，检查成员清单、JSON/CSV 可解析、无绝对路径/数据库凭据/token/内部 source_evidence，并输出新 SHA-256。

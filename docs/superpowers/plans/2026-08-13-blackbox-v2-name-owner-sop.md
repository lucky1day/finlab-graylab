# Blackbox V2 Name and Owner SOP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先更新 Blackbox V2 算法交付与本地灰度实验室入库 SOP，使正式新交付明确提供方案名称 `name`、交付来源 `owner` 和备注说明 `description`，同时准确标注机器 Contract 尚未上线的过渡状态。

**Architecture:** `name`、`owner`、`description` 都由上游两文件包的 Metadata 声明；未来 Intake 将 owner 按 composite Registry ID 原子登记到 `deploy/scheme_owner_v1.json`。本计划只修改文档，不改 Contract、Intake、Gate、数据库、算法或前端，因此两份 SOP 必须明确禁止在配套机器变更上线前运行旧 Intake。

**Tech Stack:** Markdown、Blackbox V2 Contract 1.0、Git、`rg` 文档一致性检查

---

### Task 1: 更新算法交付 SOP

**Files:**
- Modify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
- Reference: `docs/superpowers/specs/2026-08-13-blackbox-v2-name-owner-delivery-design.md`

- [ ] **Step 1: 在文档头部增加过渡状态说明**

写明：正式新包必须按新格式准备 `owner`，但当前机器 Contract 尚未接受该字段；平台在配套实现上线前只能核对，不能运行旧 `intake-blackbox`，也不能删除 owner 后代收。

- [ ] **Step 2: 更新 Metadata 示例和字段约束**

示例增加：

```json
"owner": "LW"
```

并明确：

```text
name        = 候选排行“方案”列
owner       = 候选排行“来源”列，表示交付同事或稳定团队代码
description = 候选排行“备注”详情
```

`owner` 必须为去除首尾空白后仍非空的单段纯文本，不得包含换行、`<`、`>`，不得使用 `--`、`unknown`、`待定` 等占位值。owner 不是 DataBridge 数据来源、`input_source`、审批人或运行身份。

- [ ] **Step 3: 更新上游自验表和最终交付清单**

把“历史八字段 + description”改成“历史八字段 + owner + description”；新增 name/owner/description 职责分离检查。明确最终仍只交付 `.py + .json`，owner 不应另放第三份文件或命令行参数。

- [ ] **Step 4: 检查算法 SOP 内部一致性**

Run:

```bash
rg -n "八个历史必填字段|历史八字段|description|owner|方案名称|来源" \
  docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md
```

Expected: 所有正式新交付规则同时提到 `owner` 与 `description`；没有声称当前 Intake 已支持 owner。

### Task 2: 更新本地灰度实验室入库 SOP

**Files:**
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
- Reference: `deploy/scheme_owner_v1.json`
- Reference: `backend/scheme_owner.py`

- [ ] **Step 1: 更新接收前检查和 Intake 暂停条件**

要求新包存在合法的 `name`、`owner`、`description`。在配套机器实现上线前，包含 owner 的新格式包禁止进入旧 Intake；不得手工删字段、改写只读 Metadata 或用 `--owner` 代替。

- [ ] **Step 2: 更新目标 Intake 行为**

写明未来机器上线后的目标语义：

```text
metadata.owner
→ canonical composite scheme_id
→ deploy/scheme_owner_v1.json
→ Dashboard.owner
→ 前端“来源”列
```

owner registry 缺失、非法、同 composite ID 异值冲突、原子登记失败均 fail-closed。同 ID 同 owner 可幂等接受；不得覆盖异值。

- [ ] **Step 3: 更新历史兼容和 Gate 边界**

既有不可变 Metadata 缺 owner 时不原地修改；由 owner registry 补录。新交付不提供 owner waiver。Gate 与 activation 前必须确认方案名称、来源、备注均非空且能沿权威链路读回。

- [ ] **Step 4: 更新 Dashboard 与浏览器验收清单**

前端验收加入：

- “方案”列等于 Metadata `name`；
- “来源”列等于 owner registry 中该 composite ID 的登记值，不能是 `--`；
- “备注”详情等于 Metadata `description`；
- 三者均不得由 scheme ID、target、task type 或前端硬编码推断。

- [ ] **Step 5: 检查平台 SOP 内部一致性**

Run:

```bash
rg -n "历史八字段|description|owner|scheme_owner_v1|来源|name" \
  docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md
```

Expected: 文档区分“目标 Intake 行为”和“当前尚未上线状态”，没有把未来原子登记写成当前已实现能力。

### Task 3: 文档交叉验证和提交

**Files:**
- Verify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
- Verify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
- Verify: `docs/superpowers/specs/2026-08-13-blackbox-v2-name-owner-delivery-design.md`

- [ ] **Step 1: 检查两份 SOP 的同一示例和术语**

Run:

```bash
rg -n '"owner": "LW"|name.*方案|owner.*来源|description.*备注' \
  docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md \
  docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md
```

Expected: 两份 SOP 对三个字段的职责一致。

- [ ] **Step 2: 检查过渡警告在两份 SOP 中都存在**

Run:

```bash
rg -n "机器 Contract|旧.*intake-blackbox|不得.*删除.*owner" \
  docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md \
  docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md
```

Expected: 两份文件均命中，算法同事和平台操作员不会把文档先行误解为机器已上线。

- [ ] **Step 3: 文档格式与范围检查**

Run:

```bash
git diff --check
git diff --name-only
```

Expected: 无空白错误；本轮实施只修改两份 SOP，另含已独立提交的 spec/plan，不修改代码、算法、数据库或前端。

- [ ] **Step 4: 提交 SOP**

```bash
git add \
  docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md \
  docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md
git commit -m "docs(blackbox): require scheme name and owner in V2 SOP"
```

- [ ] **Step 5: 普通推送并回读开发分支**

```bash
git push origin codex/audit-bugfixes-20260613
git ls-remote origin refs/heads/codex/audit-bugfixes-20260613
git rev-parse HEAD
```

Expected: 远程 SHA 与本地 HEAD 完全一致；不操作 `master`，不 force push。

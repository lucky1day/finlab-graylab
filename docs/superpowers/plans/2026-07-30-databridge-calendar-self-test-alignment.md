# DataBridge Calendar Self-Test Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 明确 Blackbox V2 算法自测、平台 Onboarding 验收和 scheduled live 对 `api_wind_date.csv` 与 DataBridge generation 的统一对齐规则。

**Architecture:** 保持 DataBridge 三频父 generation、`api-wind-date-v1` 平台制品和 Blackbox 两文件交付边界不变。上游通过 DataBridge 专用接口下载日历并记录四文件摘要；平台用三频 generation 与组合快照完成同代验收，生产运行继续使用当天最新 generation。

**Tech Stack:** Markdown SOP、DataBridge HTTP CSV API、Blackbox V2 Harness 术语、Python `unittest` 文档契约测试

---

### Task 1: 补齐上游 DataBridge 日历下载与自测证据

**Files:**
- Modify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
- Modify: `docs/blackbox_v2/data_bridge_v1/README.md`

- [ ] **Step 1: 在上游 SOP 的数据来源章节定义四文件自测批次**

将开发与自验说明从“三份真实 CSV”扩展为“三频业务文件 + 需要时的
`api_wind_date.csv`”，同时保留正式 delivery 只有 `.py + .json`。
明确日历是 DataBridge 可下载的开发材料和平台注册制品，不属于
`data-bridge-v1` 三频父 Schema。

- [ ] **Step 2: 增加日历下载命令**

在现有日、周、月下载命令后加入：

```bash
curl --fail-with-body --location --retry 3 \
  --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  "${DATABRIDGE_API_BASE_URL%/}/export/tables/api_wind_date/csv/" \
  --output sample_data/api_wind_date.csv
```

说明只有声明周历依赖的方案必须下载；任一文件失败或跨批切换时整批
作废，不得混用旧文件、sample 或手工日历。

- [ ] **Step 3: 增加 `api_wind_date.csv` 校验与周键规则**

要求精确两列 `rdate,week_id`，日期唯一且严格升序，周键按六位字符串
读取。明确 `week_id` 是 opaque 平台业务键，不是 ISO 周，不得 `+1`
或自行推算；`daily_cutoff_key` 的映射必须等于 Request 的
`weekly_cutoff_key`，并且 `weekly_output.csv` 必须覆盖该周。

- [ ] **Step 4: 增加同代自测证据和最终检查项**

自测报告记录下载时间、四文件 SHA256、行数、起止键和 Request 七字段。
说明普通 DataBridge 下载不返回平台内部 generation ID；上游不得编造，
由平台在 Intake 时用摘要匹配并补录。输入不匹配时必须在平台选定
generation 上重跑，不能直接判算法差异。

- [ ] **Step 5: 更新 DataBridge V1 入口文档**

在 `docs/blackbox_v2/data_bridge_v1/README.md` 增加独立“可选平台日历”
小节，给出下载入口、列契约和交付边界；维持“三类文件”表只描述父
Schema。

### Task 2: 增加平台同代验收 Gate

**Files:**
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`

- [ ] **Step 1: 在 DataBridge current 核验后增加自测对齐小节**

定义完整比较身份：

```text
generation_id
refresh_date
daily_output.csv SHA256
weekly_output.csv SHA256
monthly_output.csv SHA256
api_wind_date.csv canonical SHA256
combined_snapshot_id
```

平台先用三频 SHA 匹配 generation，再用规范化日历 SHA 构造并核对
组合快照。

- [ ] **Step 2: 明确不匹配处理**

任一身份不同即标记 `data_vintage_mismatch`，禁止把方向、样本数、
月度统计或内部字段差异归责算法。精确验收必须给上游平台选定的同代
输入并重跑。

- [ ] **Step 3: 明确生产滚动规则**

Onboarding 验收同代不等于永久固定生产数据。`scheduled_live` 仍绑定
当天最新且 SEALED 的 generation，并记录当次
`generation_id + combined_snapshot_id`；跨 generation 只能做稳定性
观察，不能宣称逐行复现。

- [ ] **Step 4: 更新 Gate 表和最终检查表**

在 Input/Compare 证据及最终检查中加入四文件摘要、日历映射、同代
匹配、跨代重测和 `data_vintage_mismatch` 处理。

### Task 3: 收口日期与周键权威语义

**Files:**
- Modify: `docs/architecture/PREDICTION_SEMANTICS.md`

- [ ] **Step 1: 增加日期与周键权威矩阵**

明确：

```text
daily_output.csv.date              算法日频观测轴
api_wind_date.csv                  rdate -> opaque week_id
t_trade_calendar                   平台调度与前后交易日计算
Request                            predict/feature/target 与三个 cutoff 的唯一运行合同
```

算法不得用任一文件覆盖 Request，也不得把 `week_id` 当 ISO 周或数值
连续键。

- [ ] **Step 2: 在周频实盘规则加入同代约束**

说明 `feature_date` 先由平台交易日历计算，再由与本次输入身份绑定的
`api_wind_date.csv` 映射 `feature_week_id`。算法自测和平台验收必须
同代；跨代差异先归类数据版本差异。

### Task 4: 验证文档一致性

**Files:**
- Test: `tests/test_onboarding_docs.py`
- Verify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
- Verify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
- Verify: `docs/architecture/PREDICTION_SEMANTICS.md`
- Verify: `docs/blackbox_v2/data_bridge_v1/README.md`

- [ ] **Step 1: 检查 Markdown 与补丁格式**

Run:

```bash
git diff --check
```

Expected: exit code `0`，无输出。

- [ ] **Step 2: 运行文档契约测试**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_onboarding_docs
```

Expected: `OK`。若测试固定断言与新 SOP 冲突，只修改
`tests/test_onboarding_docs.py` 中相应文档契约，并重新运行。

- [ ] **Step 3: 检查关键术语同时出现**

Run:

```bash
rg -n "export/tables/api_wind_date/csv|data_vintage_mismatch|combined_snapshot_id|同代|week_id" \
  docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md \
  docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md \
  docs/architecture/PREDICTION_SEMANTICS.md \
  docs/blackbox_v2/data_bridge_v1/README.md
```

Expected: 下载入口出现在上游/DataBridge 文档；同代、组合快照和跨代
处理出现在上游与平台 SOP；周键权威出现在日期语义文档。

- [ ] **Step 4: 核对提交范围**

Run:

```bash
git status --short
git diff --name-only
```

Expected: 本次仅包含计划列出的通用文档和必要的文档契约测试；现有
`docs/blackbox_v2/records/CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.md`
修改保持未暂存。

- [ ] **Step 5: 提交 SOP 修订**

```bash
git add \
  docs/superpowers/plans/2026-07-30-databridge-calendar-self-test-alignment.md \
  docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md \
  docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md \
  docs/architecture/PREDICTION_SEMANTICS.md \
  docs/blackbox_v2/data_bridge_v1/README.md
git commit -m "docs: align DataBridge calendar self-tests"
```

如 `tests/test_onboarding_docs.py` 因文档契约更新而修改，则将其一并
显式加入；不得使用 `git add .`。

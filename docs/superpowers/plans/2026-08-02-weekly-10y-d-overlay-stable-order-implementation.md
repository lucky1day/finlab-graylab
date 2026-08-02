# Weekly 10Y D-overlay 跨年排序确定性修正 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development 或 superpowers:executing-plans 逐 task 执行。步骤用 checkbox（`- [ ]`）跟踪。

**Goal:** 让 `weekly_10y_d_overlay_0529` 的 Model2 工程帧在同一受治理输入、不同滚动窗口边界下产生确定性的标签顺序，消除 `predict_date=2026-08-01` 因 Score/Model2 mismatch 被 fail-closed 拒绝的无信号问题。

**Architecture:** 唯一行为变更是 `core/d_overlay.py` 的 `load_engineered_frame()` 中，Model2 进入 `create_label()` 前的排序由单键 `date` 改为二级键 `["date", "week_id"]` 且 `kind="stable"`。`model_date` 仍是第一排序键，所有既有 segment cutoff 语义不变；仅当多个 week 映射到同一 `model_date`（如 202553 与 202601 都映射 2025-12-29）时，用 `week_id` 恢复输入已保证的时间顺序。Score 路径、`build_base()` mismatch guard、原始归档文件与日期映射函数均不修改。

**Tech Stack:** Python 3.12（测试环境 pandas 2.2.2 / numpy 1.26.4；Native 算法 forecast_env pandas 2.3–3.0 / numpy 2.3–2.4）、pytest 9.1.1、`python -m harness` 静态边界门。

## Global Constraints

- 权威设计：`docs/superpowers/specs/2026-08-02-weekly-10y-d-overlay-stable-order-design.md`（状态 APPROVED）。本计划不得引入设计之外的行为变更。
- 这是用户明确批准的 **Native V1 L2 微修正例外**，**只允许改这一个排序键 + 回归测试**；不得更换模型、改特征/窗口/分段/投票/fallback/输入来源，不写库、不碰前端。
- 开发提交**不操作** MySQL、`t_scheme_predictions`、registry、installed plist、`launchctl`、服务进程。写库与激活需另行专项授权。
- Native source-backed core 保持零 DB、零写库、零跨方案 import。
- 若 CompareGate / harness Gate 因输入 vintage 漂移阻断，**保留失败证据并停止**，不得改 benchmark、旧信号或输入快照贴合。
- 已确认事实（实证，三个 numpy 版本一致）：新排序在 299 个窗口尺寸下恒定正确；旧单键排序在 36/37/54–57/… 等尺寸翻转并列对，把 202553 的未来收益错接到 202602 得 label `-1`（正确应 `+1`）。4 行极小输入在旧代码上**不**翻转，故回归测试须用会翻转的窗口尺寸。

---

### Task 1: 跨年排序回归测试（TDD 红）

**Files:**
- Create: `tests/test_weekly_10y_d_overlay_stable_order.py`
- 依赖（只读，不改）：`schemes/weekly_10y_d_overlay_0529/core/d_overlay.py` 的 `load_engineered_frame`；`schemes/weekly_10y_d_overlay_0529/predict.py` 的 `_legacy_segment_anchor_date`（测试内复刻，避免引入 DB 依赖）。

**Interfaces:**
- Consumes: `d_overlay.load_engineered_frame(weekly: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]`，输入需含列 `week_id, week_date, model_date, TB0YWI3C, TB1YWI3C, TB5YWI3C`；返回帧含 `week_id` 与 `label_5d`。
- Produces: 两个 pytest 用例，锁定「并列 model_date 按 week_id 稳定排序」与「跨年标签窗口不变性」。

- [ ] **Step 1: 写失败测试**（完整内容）

```python
"""weekly_10y_d_overlay_0529 跨年排序确定性回归测试。

背景（docs/superpowers/specs/2026-08-02-weekly-10y-d-overlay-stable-order-design.md）：
week_id 202553 与 202601 都被 _legacy_segment_anchor_date 映射到 model_date=2025-12-29。
load_engineered_frame() 旧实现进入 create_label() 前只按单键 date（pandas 默认不稳定
quicksort）排序；滚动输入窗口行数变化时，这对并列日期行的顺序会翻转，把 202553 的未来
收益错接到 202602（label -1）而非 202601（label +1），与 Score 严格 week_id 时序冲突，
触发 fail-closed guard。修复：按 ["date","week_id"] + kind="stable" 排序。
"""
from __future__ import annotations

import pandas as pd

from schemes.weekly_10y_d_overlay_0529.core import d_overlay


def _anchor(week_id: int) -> str:
    """复刻 schemes/weekly_10y_d_overlay_0529/predict.py:_legacy_segment_anchor_date。"""
    text = str(int(week_id))
    year = int(text[:4])
    ordinal = int(text[4:])
    jan1 = pd.Timestamp(f"{year}-01-01")
    first_thu = jan1 + pd.Timedelta(days=(3 - jan1.weekday()) % 7)
    first_anchor = first_thu - pd.Timedelta(days=3)
    return (first_anchor + pd.Timedelta(weeks=ordinal - 1)).strftime("%Y-%m-%d")


def _week_id_sequence() -> list[int]:
    """含跨年碰撞的 week_id 序列：每年 1..52，额外插入 202553（与 202601 同 anchor）。"""
    weeks: list[int] = []
    for year in range(2019, 2027):
        for week in range(1, 53):
            weeks.append(year * 100 + week)
    weeks.append(202553)
    return sorted(set(weeks))


def _make_weekly(window_size: int) -> pd.DataFrame:
    """构造末端 202628、长度 window_size 的跨年周频输入。

    202601 目标利率高于 202553（正确未来 -> +1），202602 低于 202553（翻转未来 -> -1），
    故 202553 的 label_5d 直接暴露排序是否把未来收益接到了正确的下一周。
    """
    seq = [w for w in _week_id_sequence() if w <= 202628][-window_size:]
    assert {202552, 202553, 202601, 202602}.issubset(set(seq))
    rows = []
    for idx, week_id in enumerate(seq):
        model_date = _anchor(week_id)
        if week_id == 202601:
            rate = 101.0
        elif week_id == 202602:
            rate = 99.0
        else:
            rate = 100.0 + 0.01 * idx
        rows.append(
            {
                "week_id": week_id,
                "week_date": model_date,
                "model_date": model_date,
                "TB0YWI3C": rate,
                "TB1YWI3C": rate * 0.9,
                "TB5YWI3C": rate * 1.1,
            }
        )
    return pd.DataFrame(rows)


def _label_of(engineered: pd.DataFrame, week_id: int) -> int:
    return int(engineered.loc[engineered["week_id"] == week_id, "label_5d"].iloc[0])


def _position_of(engineered: pd.DataFrame, week_id: int) -> int:
    return int(engineered.index[engineered["week_id"] == week_id][0])


def test_cross_year_tied_date_orders_by_week_id_and_labels_correctly() -> None:
    """36 行翻转窗口：202553 必须排在 202601 之前，label_5d 接到 202601（+1）。"""
    engineered, _ = d_overlay.load_engineered_frame(_make_weekly(36))
    assert _position_of(engineered, 202553) < _position_of(engineered, 202601)
    assert _label_of(engineered, 202553) == 1


def test_cross_year_label_is_window_shift_invariant() -> None:
    """窗口再滚动一周也不能改变跨年周标签：多个尺寸下 202553 恒为 +1。"""
    labels = {size: _label_of(d_overlay.load_engineered_frame(_make_weekly(size))[0], 202553)
              for size in range(36, 61)}
    assert set(labels.values()) == {1}, f"跨年标签随窗口漂移: {labels}"
```

- [ ] **Step 2: 跑测试确认在旧实现上失败**

Run: `python -m pytest tests/test_weekly_10y_d_overlay_stable_order.py -v`
Expected: 两个用例均 FAIL —— `test_...orders_by_week_id...` 断言 `label_5d(202553)==1` 实得 `-1`；`test_...window_shift_invariant` 得到 `{1, -1}`。

- [ ] **Step 3: 提交失败测试**

```bash
git add tests/test_weekly_10y_d_overlay_stable_order.py
git commit -m "test: pin weekly-10y cross-year stable-order regression"
```

---

### Task 2: 应用稳定排序修复（TDD 绿）

**Files:**
- Modify: `schemes/weekly_10y_d_overlay_0529/core/d_overlay.py:1835`

**Interfaces:**
- Consumes: Task 1 的两个用例。
- Produces: 确定性的 `load_engineered_frame` 排序；不改签名与返回结构。

- [ ] **Step 1: 改这一行**

```python
# 之前：
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
# 之后：
    df = df.dropna(subset=["date"]).sort_values(["date", "week_id"], kind="stable").reset_index(drop=True)
```

- [ ] **Step 2: 跑测试确认通过**

Run: `python -m pytest tests/test_weekly_10y_d_overlay_stable_order.py -v`
Expected: 两个用例 PASS。

- [ ] **Step 3: 提交修复**

```bash
git add schemes/weekly_10y_d_overlay_0529/core/d_overlay.py
git commit -m "fix: stable cross-year order in weekly-10y d-overlay engineered frame"
```

---

### Task 3: 既有单测 + 全仓静态边界门（验收条件 3）

**Files:** 无改动（只运行）。

- [ ] **Step 1: 跑相关既有单测**

Run: `python -m pytest tests/test_compare_gate.py tests/test_executor_run_id.py tests/test_signal_policy.py -q`
Expected: 全绿（这些用例引用 weekly_10y_d_overlay，验证无回归）。

- [ ] **Step 2: 跑全仓静态边界门**

Run: `python -m harness static-gate --project-root .`（若命令名不同，用 `python -m harness --help` 查 StaticGate 对应子命令）
Expected: 依赖方向/写库单点/core 纯净等不变量全部通过。

- [ ] **Step 3: 跑全量测试收集（确保未破坏集合）**

Run: `python -m pytest -q`
Expected: 全绿或仅出现与本改动无关、且改动前既已存在的跳过项；如有失败，判断是否本改动引入，若非则记录环境前置。

---

### Task 4: 逐字段零差异比较（验收条件 4）

**Files:** Create（临时，不提交）：`reports/verification/weekly_10y_stable_order/` 下比较脚本与输出（该目录已 gitignore）。

- [ ] **Step 1: 对历史完整输入与 2026-07-25 live 输入，用修复前后两版 `load_engineered_frame` 逐字段比较**

对 `date/week_id/label_5d/future_return`（工程帧层）以及经 `build_d_overlay` 的 `d_pred_label/confidence/score_*/model2_*/overlay_*` 做 diff。
Expected: 除 202553 这一被修正的错接点外，方向、confidence、Score、Model2、overlay、label、future_return 均零差异；被修正点由错误 `-1` 变为正确 `+1`（这正是修复目标，非回归）。

- [ ] **Step 2: 记录比较结论到工作记录（Task 6）**，保留输出文件路径与逐字段计数。

---

### Task 5: 2026-08-01 no-persist 复现（验收条件 5，evidence-preserving）

**Files:** 无改动（只运行 no-write 复现）。

- [ ] **Step 1: 用正式 runner 对 2026-08-01 做 no-write 复现**

Run: `python -m scheduler.scheme_runner --scheme weekly_10y_d_overlay_0529 --predict-date 2026-08-01 --no-persist`（以实际 CLI 为准；等价 Native no-persist 入口亦可）
Expected（成功）：产出一条 `feature_date=2026-07-31`、`target_date=2026-08-07` 的结果，`week_id=202629`、方向 `-1`、confidence `0.32`，不再触发 Score/Model2 mismatch。

- [ ] **Step 2: 若被输入 vintage 漂移 / CompareGate 阻断**

保留失败证据（stdout/日志/report 路径）并停止；**不得**改 benchmark、旧信号或输入快照贴合。这与 P1「10Y D-overlay 输入 vintage 独立研究项」一致，属独立阻断，不影响本排序修复的开发验收。

---

### Task 6: 更新交接文档 + 工作记录（不越界）

**Files:**
- Modify: `docs/records/status/KNOWN_ISSUES_HANDOFF_20260802.md`（问题一标注代码修复已完成 + 验收边界）
- Create: `docs/superpowers/specs/2026-08-02-weekly-10y-d-overlay-stable-order-design.md` 状态位推进（`APPROVED` → 记录已实施）——如需，仅改状态行。
- Create: `docs/records/status/` 或 `docs/blackbox_v2/records/` 下一份简短工作记录（按仓库 design→plan→execute→record 节奏）。

- [ ] **Step 1: 在交接文档问题一追加「代码修复已完成」小节**，写清：改动点（单行）、新增回归测试、Task 3/4 验收结果、Task 5 复现结论（成功或被 vintage 阻断的证据），并重申写库/激活/launchd 仍待专项授权。
- [ ] **Step 2: 提交文档**

```bash
git add docs/records/status/KNOWN_ISSUES_HANDOFF_20260802.md docs/superpowers/plans/2026-08-02-weekly-10y-d-overlay-stable-order-implementation.md docs/records/status/*weekly*10y*stable*order*.md
git commit -m "docs: record weekly-10y stable-order fix implementation and handoff update"
```

---

### Task 7: 推特性分支 + 开 PR（不自行合并）

**Files:** 无改动。

- [ ] **Step 1: 推分支**

```bash
git push -u origin codex/fix-weekly-10y-stable-order-20260802
```

- [ ] **Step 2: 开 PR，base = `codex/audit-bugfixes-20260613`**

```bash
gh pr create --base codex/audit-bugfixes-20260613 --head codex/fix-weekly-10y-stable-order-20260802 \
  --title "fix: weekly-10y d-overlay cross-year stable order" \
  --body "见 docs/superpowers/specs/2026-08-02-weekly-10y-d-overlay-stable-order-design.md 与本分支 plan。仅一行排序键 + 回归测试；不触碰 DB/registry/plist/launchd。master 不动，dev→master 晋升待用户确认。"
```

Expected: 返回 PR URL；**不执行 merge**。把 URL 交用户审阅。

---

## 验收条件对照（来自设计 6 条）

| 设计验收条件 | 对应 Task |
|---|---|
| 1. 新测试先在旧实现失败 | Task 1 Step 2 |
| 2. 最小修改后新测试通过 | Task 2 Step 2 |
| 3. 既有单测 + 全仓静态边界通过 | Task 3 |
| 4. 历史 + 0725 逐字段零差异 | Task 4 |
| 5. 0801 no-write 复现 feature=0731/target=0807 | Task 5 |
| 6. harness Gate 若被 vintage 漂移阻断则保留证据停止 | Task 3 Step 2 / Task 5 Step 2 |

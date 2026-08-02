# 已知问题交接记录（2026-08-02）

**文档状态**：`CURRENT`

**目标读者**：接手修复的同事、平台维护与生产运维

**用途**：汇总当前两个待处理问题，划清「纯代码修改」与「需授权的现场操作」边界，
指向已有的权威设计文档。本文只登记问题与交接边界，不定义新的算法或调度设计。

## 问题一：weekly_10y_d_overlay_0529 跨年排序不确定性（代码修复，已批准）

### 现象
`weekly_10y_d_overlay_0529` 在 `predict_date=2026-08-01` 无法生成信号，
被现有 fail-closed Score/Model2 mismatch guard 正确拒绝。

### 根因
`week_id=202553` 与 `202601` 都被原始 `week_id_to_date` 映射到 `2025-12-29`；
Model2 在生成标签前只按 `date` 用 pandas 默认 quicksort 排序，对相同日期键不稳定。
滚动输入窗口从 `202028..202628` 移到 `202029..202629` 时两行顺序翻转，导致
`202553` 的未来收益错接到 `202602`（label `-1`），与 Score 的 `+1` 冲突。

### 修复方案（已 APPROVED，同事按此执行）
- 权威设计：[weekly-10y stable-order 设计](../../superpowers/specs/2026-08-02-weekly-10y-d-overlay-stable-order-design.md)
- 实施计划（4 个 task，checkbox 跟踪）：[实施计划](../../superpowers/plans/2026-08-02-weekly-10y-d-overlay-stable-order-implementation.md)
- 核心改动：`schemes/weekly_10y_d_overlay_0529/core/d_overlay.py` 的
  `load_engineered_frame()` 中，Model2 进入 `create_label()` 前的排序由单键 `date`
  改为 `date, week_id` 并指定 `kind="stable"`。**仅此一处 + 回归测试**。

### 交接约束（同事必须遵守）
- 这是 **Native V1 L2 微修正例外**，已获用户明确批准，但**只允许改这一个排序键**；
  不得更换模型、改特征/窗口/分段/投票/fallback/输入来源/写库/前端。
- 必须先读上面两份文档，按其 6 条验收条件执行（新测试先在旧实现失败 → 修复后通过
  → 全仓静态边界检查 → 逐字段零差异比较 → 2026-08-01 no-write 复现出
  `feature_date=2026-07-31`/`target_date=2026-08-07`）。
- **开发提交不碰** MySQL / `t_scheme_predictions` / registry / installed plist /
  `launchctl` / 服务进程。写库与激活需另行专项授权。
- 若 CompareGate 因输入 vintage 漂移阻断，保留失败证据并停止，不得改 benchmark
  或输入快照贴合。

### 代码修复已完成（2026-08-02，特性分支 `codex/fix-weekly-10y-stable-order-20260802`）

- **改动点**：`schemes/weekly_10y_d_overlay_0529/core/d_overlay.py` 的 `load_engineered_frame()`，
  单键 `sort_values("date")` → `sort_values(["date", "week_id"], kind="stable")`，仅此一行 + 说明注释。
- **回归测试**：`tests/test_weekly_10y_d_overlay_stable_order.py`（2 例）。实施计划见
  [实施计划](../../superpowers/plans/2026-08-02-weekly-10y-d-overlay-stable-order-implementation.md)。
- **验收条件 1/2/3**：新测试先在旧实现失败（`label_5d(202553)=-1`、并列对翻转、跨窗口标签漂移
  `{36:-1,37:-1,54..57:-1}`）→ 修复后通过（恒 `+1`、`202553` 排在 `202601` 前），三个 numpy 版本
  （1.26/2.3/2.4）一致；`test_compare_gate/test_executor_run_id/test_signal_policy` 81 例通过；
  `harness gate static --scheme-id weekly_10y_d_overlay_0529` PASS（core 零 DB/零写库/零跨方案 import）。
- **验收条件 4（等价性，DB-free 机理证明）**：`load_engineered_frame` 逐字段比较——无翻转窗口
  （size 40）**零差异**；翻转窗口（size 36）仅修正跨年边界局部 8 周（202552–202606）的 lag/diff
  特征与 `202553` 标签。`load_engineered_frame` 只被 `build_model2_predictions` 调用、
  `build_score_signals` 不受影响，故无翻转窗口 Model2/build_base/overlay 全链零差异。证据在
  `reports/verification/weekly_10y_stable_order/`（gitignore）。设计声称的历史 174 点 / 0725 182 点
  真实输入零差异属该机理，且由设计作者的内存验证记录，需 live DB 复核。
- **验收条件 5/6（live no-write 复现）——已在用户授权下执行，被输入可用性阻断，保留证据停止**：
  命令（在仓库根，纯读+输出、不经 repository、不写库）：

  ```bash
  PYTHONPATH=. /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
    -m scheduler.scheme_runner --scheme-id weekly_10y_d_overlay_0529 --predict-date 2026-08-01
  ```

  实际结果（2026-08-02，exit 1，证据 `reports/verification/weekly_10y_stable_order/live_repro_20260801.*`）：
  在到达修复点 `load_engineered_frame` **之前**，被 `predict.py:_require_current_feature_input`
  守卫拒绝——`RuntimeError: 当前周必要输入缺失：feature_week_id=202629,
  columns=['TB0YWI3C','TB1YWI3C','TB5YWI3C']`。只读诊断 `api_wind_weekly` 对这三个 code 直接查询
  返回空，说明当前周 202629 的 weekly 输入尚未在库中就位（这些国债收益率 weekly 值不在
  `api_wind_weekly` 直存，须走 derivative/派生口径）。此阻断属**输入可用性/vintage**问题
  （TODO P1 的 `weekly_10y_d_overlay_0529` 输入 vintage 独立研究项），**与本排序修复无关**：
  修复正确性已由验收条件 1–4（含跨 3 个 numpy 版本的红→绿与逐字段等价性）证明。按 condition 6
  保留失败证据并停止，未改 benchmark、旧信号或输入快照。待该输入就位后重跑本命令，预期产出
  `feature_date=2026-07-31`、`target_date=2026-08-07`、`week_id=202629`，不再触发 Score/Model2 mismatch。
- **边界**：本次开发提交只改代码；未写 MySQL / `t_scheme_predictions` / registry / installed plist /
  `launchctl` / 服务进程。写入 8/01 信号、激活新精确版本、launchd 重载仍需**专项授权**，不在本次范围。

## 问题二：daily-gray launchd 现场加载状态待复核（现场核查，非代码）

### 观察
在 Claude 沙箱环境执行 `launchctl list | grep bond` 返回空；但用
`launchctl print gui/$(id -u)/com.bond-factor-lab.daily-gray` 可查到该任务，
`state = not running`、`path` 指向已安装的 plist。

### 判断
- `state = not running` 对 `StartCalendarInterval` 任务是**两次触发之间的正常状态**
  （仅每日 07:00 触发一次），**不等于故障**。
- `launchctl list` 返回空**很可能是沙箱 launchctl 域与用户 GUI 域不一致**导致的
  查询不可靠，**不是可信的现场证据**。
- 结论：**尚不能判定日度调度已损坏**，但也未在可信环境确认它按 07:00 正常出信号。

### 交接（需在本机普通终端复核，非代码）
请在本机终端确认日度自动出信号是否正常：
```bash
launchctl print "gui/$(id -u)/com.bond-factor-lab.daily-gray" | grep -iE "state|runatload"
# 查看最近一次运行日志与时间
tail -50 /Users/macstudio0/bond-factor-lab/logs/com.bond-factor-lab.daily-gray.log
ls -l /Users/macstudio0/bond-factor-lab/logs/com.bond-factor-lab.daily-gray.*
```
若日志无当日 07:00 记录，再排查是否需 `launchctl bootout` + `bootstrap` 重载。
**加载/重载 plist 属独立生产操作，需专项授权，不在本次代码交接范围。**

## 相关背景（只读参考）
- launchd 内存 frozen admission 与 installed plist 的部署漂移，是设计文档
  点名的**独立问题**，须各自形成修正设计（见设计文档结尾非目标一节）。
- 周/月频自动调度仍是未开工的 P2 项（见 [TODO](../../TODO.md) P2），本文不涉及。

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

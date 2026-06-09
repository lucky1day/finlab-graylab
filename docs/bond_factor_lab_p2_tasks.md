# Bond Factor Lab — P2 实验室体验 任务说明（单 agent 串行）

> 前置：P0 ✅ / P1 ✅ 已完成（123/123）。本文件是 P2（前端分析体验）的可执行任务说明。
> 配套：现状勘探见本轮对话；总计划见 [bond_factor_lab_execution_plan.md](bond_factor_lab_execution_plan.md) §3、评审依据 [bond_factor_lab_architecture_review.md](bond_factor_lab_architecture_review.md) P2 清单。
> **决策：① 只做现在就有真实数据、纯前端/只读接口能落地的分析项；② 单 agent 串行（前端单文件 + 后端单 services.py，并行必撞车）。**

---

## 0. 执行规则（务必先读）

### 范围裁剪（关键）

勘探确认:评审 P2 清单里 **shadow vs active / harness gate 可视化 / input artifact 谱系** 当前是 **schema 有、数据空**——`run_type` 恒为 `active`、`t_harness_*` 为空、executor 还没调 `upsert_input_artifact`。**本轮 P2 不做这三项**(做了也是空壳)。它们需要先补后端数据管线(executor 落 shadow run / input_artifact、harness orchestrator 落库),属于"P1.5 数据管线补全",列在 §3 待排期,不在本轮。

**本轮只做下列有数据支撑的项**(数据来源已勘探确认)。

### 两条不可破坏的约束（守住 P0/P1 成果）

1. **GET 只读**：P2 新增/修改的所有 backend 接口必须只读，零写库副作用（P0 不变量）。新接口只做"聚合现有表"的查询。
2. **零新表**：P2 不加迁移、不加表。所有数据来自现有 `t_scheme_predictions`(含 P1 的 `run_id/scheme_version`)/`t_scheme_actuals`/`t_scheme_weekly_actuals`/`t_scheme_serving_pointer`/`t_scheme_runs`/`t_scheme_versions`。

### 前端惯例（必须沿用，不得引入新依赖）

- 纯 vanilla JS（ES5 风格），改 `frontend/aifin-shell.js`(IIFE 内新增 render 函数)、`frontend/aifin-shell.css`(BEM 风格类)、必要时 `frontend/index.html`(加容器 section)。
- **不引入** React/Vue/任何图表库/构建步骤。图表沿用现有"手写 SVG 字符串拼接"模式（参考 `renderFactorTrendChart` line ~939）。
- 颜色复用现有 token：`--accent`(#155C3E 绿)、`#d62828` 红、`#b98728` 金、`metric-good/warn/bad`(阈值 62%/55%)。
- 状态挂在现有 `factorLabState` 单例;数据走现有 `fetchJson` + 60s 自动刷新机制。
- 表格/抽屉/分页/模态定位复用现有组件模式(`positionFactorCalendarPanel` 等)。

### 后端惯例

- 改 `backend/main.py`(加 GET 路由)、`backend/services.py`(加只读聚合函数)。沿用现有 SQLAlchemy text() + dict 返回风格。
- 新接口路径用 kebab-case，挂 `/api/...`。响应结构对齐现有 `/api/metrics/{scheme_id}` 风格。

### 验收要求（每个任务）

- 后端改动带单测(扩展 `tests/test_backend_api.py` 或新增),断言**无写库副作用**(mock 写函数断言未调用)。
- service env 跑全套 `unittest` 全绿:`/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest discover -s tests -p "test_*.py"`(不设环境变量验软默认)。
- 前端改动:human-in-the-loop 视觉确认(可用 Preview/截图)。无法自动断言的，至少确认页面不报 JS 错、接口返回被正确渲染。
- subagent 不执行 git;由编排者按步提交(或单 agent 每步独立提交,提交信息 `feat(ui): P2-x ...` + `Co-Authored-By`)。
- **不碰** `schemes/`、`harness/`、`migrations/`、`scheduler/`。

### 现状基线（钉死）

- 前端 3 文件:`index.html`(9.6KB)、`aifin-shell.js`(51KB,1264 行,IIFE)、`aifin-shell.css`(26KB)。无构建。
- 现有视图:单页"预测准确率矩阵"——任务格子(tenor×T+1/T+5/周度)→ 方案 ranking 表 → 方案详情(月度表+SVG 折线图)→ 每日核对抽屉。
- 现有接口:`/api/schemes`、`/api/targets`、`/api/metrics/{scheme_id}?tenor=&start_month=&end_month=`、`/api/predictions`、`/api/actuals`、`/api/backtests/*`、`/api/health`。
- `/api/metrics/{id}` 的 `daily_rows` 已含:`run_id, scheme_version, input_artifact_hash, predict_date, target_date, predicted_direction, actual_direction, is_correct, confidence, model_version`。**这是 P2 大部分前端计算的数据源。**
- 准确率口径:`_metric_block`(services.py ~185)按 horizon 选 `direction_1d/5d/weekly` JOIN actuals;月度聚合在 `scheme_metrics`(~484)。

---

## 1. 本轮 P2 任务（按建议顺序 U1→U5 串行）

> 每个任务标注「数据来源(已确认有数据)」「后端」「前端」「验收」。先做纯前端项(U1/U3/U4 数据现成)，再做需要新接口的项(U2/U5)。

### U1 — Ranking 增强（最先，纯前端，零后端）

- **目标**：现有 ranking 表已按单指标排序;增强为多列可排序 + 视觉强化,让"几十方案横评"更可读。
- **数据来源**：现有 `factorTaskSchemes`(已含每方案 overall/up_precision/down_precision/samples/last_run/status)。**无需新接口。**
- **后端**：无。
- **前端**（`aifin-shell.js` + `.css`）：
  1. ranking 表头各指标列可点击切换排序(asc/desc),当前排序列高亮。
  2. 每行 overall 加一个**迷你条形**(inline SVG 或 CSS 背景宽度)直观对比;样本数过低(如 <30)加"样本不足"灰标。
  3. status pill 复用现有配色;`scheme_version` 若有则在方案名旁以小字号展示(来自 daily_rows，可在选中时补)。
- **验收**：点列头排序生效;低样本标记出现;无 JS 错误;视觉确认。
- **规模**：~80 行 JS + ~30 行 CSS。**依赖**:无。

### U2 — 跨 tenor / 跨方案横向对比视图（需 1 个只读聚合接口）

- **目标**：在任务格子之外，提供一个"同一指标、跨 tenor × 跨方案"的对比矩阵/热力表，回答"哪个方案在哪个 tenor 最强"。
- **数据来源**：现有逐方案 `/api/metrics` 可拼,但跨方案×跨tenor 前端拼 N×M 次请求太碎。**加一个聚合接口更干净。**
- **后端**（`backend/main.py` + `services.py` + 测试）：
  - 新增 `GET /api/metrics/compare?frequency=&start_month=&end_month=&metric=`:一次返回 {scheme × tenor → 指标值 + 样本数},内部复用 `_metric_block` 聚合逻辑,**只读**。
  - 测试:断言返回结构 + 无写库。
- **前端**：新增一个对比热力表 section(行=方案,列=tenor,单元格=指标值,按值着色 good/warn/bad);可切换 metric(overall/up/down)。
- **验收**：接口只读(测试断言);热力表渲染正确;切 metric 重算;全套测试绿。
- **规模**：~90 行后端 + ~100 行前端。**依赖**:无(U1 之后做,复用其排序/着色心智)。

### U3 — Confidence calibration 图（纯前端，零后端）

- **目标**：把"预测置信度 vs 实际命中率"画出来——校准曲线(可靠性图),看模型置信是否可信。
- **数据来源**：`/api/metrics/{id}` 的 `daily_rows`(已含 `confidence` + `is_correct`)。**纯前端分桶计算。**
- **后端**：无。
- **前端**（SVG，复用折线图模式）：
  1. 选中方案+tenor 后,把 daily_rows 按 confidence 分桶(如 10 桶 0–1 或按实际分布),每桶算命中率 = mean(is_correct)。
  2. 画可靠性图:x=置信度桶,y=实际命中率,叠加 y=x 理想对角线;每桶点大小/标注样本数。
  3. 放在方案详情面板,与现有月度趋势图并列(加 tab 或并排)。
- **验收**：分桶正确(空桶跳过);对角线参考存在;样本少的桶有标注;视觉确认。
- **规模**：~120 行 JS(分桶 + SVG)+ ~20 行 CSS。**依赖**:无。

### U4 — Rolling hit ratio + drawdown / 连错（纯前端，零后端）

- **目标**：时间序列健康度——滚动命中率、最大连续错误、累计净命中(drawdown 视角)。
- **数据来源**：`/api/metrics/{id}` 的 `daily_rows`(按 predict_date 排序的 `is_correct` 序列)。**纯前端计算。**
- **后端**：无。
- **前端**（SVG + 小统计卡）：
  1. 按 predict_date 升序取 is_correct 序列;计算滚动窗口(如 20 日)命中率曲线,SVG 折线。
  2. 计算最大连续错误次数(maxConsecutiveMiss)、当前连错、累计(correct-incorrect)曲线(净命中 drawdown)。
  3. 详情面板加一组小卡片(rolling hit / max 连错 / 当前连错)+ 一张滚动命中率 SVG。
- **验收**：滚动窗口数学正确(边界:序列短于窗口时退化);连错统计正确;视觉确认。
- **规模**：~130 行 JS + ~20 行 CSS。**依赖**:无(可与 U3 同在详情面板,注意两者都改详情区——同一 agent 串行做,先 U3 后 U4)。

### U5 — 方案生命周期 / 健康概览页（需 1 个只读接口；薄版）

- **目标**：一个"方案总览"小页,展示每个在册方案的生命周期状态 + 最近运行健康度。**注意:当前 status 多为 active、run_type 恒 active,所以这页是"轻量健康概览",不是完整 draft→retired 状态机(那需 §3 数据管线)。**
- **数据来源**：`t_scheme_versions.status`、`t_scheme_runs`(started/finished/status/records_written/error_message)、`t_scheme_run_log`、serving pointer。均有数据。
- **后端**（`main.py` + `services.py` + 测试）：
  - 新增 `GET /api/schemes/lifecycle`:每方案返回 {scheme_id, name, version_status, latest_run(status/date/records_written/error), 最近 N 次运行成功率, 最新预测 predict_date}。只读聚合。
  - 测试:结构 + 无写库。
- **前端**：新增一个 section/小页(或 ranking 上方的概览条):每方案一张卡——状态 pill、最近运行成功率、最新预测日期、异常(失败/缺预测)红标。
- **验收**：接口只读;卡片渲染;异常态(若构造失败 run)红标;全套测试绿。
- **规模**：~80 行后端 + ~90 行前端。**依赖**:无(放最后,因为它是"概览"，做完前面几项后信息更全)。

---

## 2. 顺序与提交

```
U1 ranking 增强(纯前端)
  → U2 横向对比(+1 只读接口)
    → U3 calibration 图(纯前端,详情面板)
      → U4 rolling/drawdown(纯前端,详情面板,接 U3)
        → U5 生命周期概览(+1 只读接口,最后)
```

- 单 agent 按 U1→U5 串行,每步独立提交:`feat(ui): P2-U{n} <简述>` + `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- 后端两处新接口(U2/U5)各自带测试;前端每步做完跑一次全套 unittest 确认无回归 + 视觉确认。

---

## 3. 明确不在本轮（需先补后端数据管线，未来 P1.5 再做）

这些评审 P2 项**当前无数据**，做了是空壳，先补管线再做：

- **shadow vs active 对比**：需 executor 支持创建 `run_type=shadow` 的运行(现恒为 active)+ serving pointer 区分;前端才有两套数据可比。
- **harness gate 结果可视化**：需 harness orchestrator 真正落 `t_harness_runs`/`t_harness_gate_results`(P1 S5 加了 persistence 能力,但需在 onboard 流程接入并产出数据)。
- **input artifact 谱系展示**：需 executor 调 `upsert_input_artifact` 真正落 `t_input_artifacts`(现 sparsely/空)。
- **完整生命周期状态机页**(draft→validated→shadow→active→paused→retired):需上面 shadow/版本流转有真实数据。

> 建议:若以后要做这几项,先排一个"P1.5 数据管线补全"小阶段(executor 落 input_artifact + shadow run、onboard 落 harness 留痕),再回头做对应前端。

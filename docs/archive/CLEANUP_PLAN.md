# 仓库清理计划（Cleanup Plan）

**更新日期**: 2026-06-09
**定位**: 基于真实盘点的冗余文档与代码清理计划。**本文只规划，不执行任何删除**。每项标注「依据」「验证方式」「风险级」，由你逐项决策。
**前置约束**: 行为保持优先——任何清理都不得影响 active 方案（t1/t5/weekly_10y）运行、已验收方案（weekly_5y/7y）、harness 全套 Gate、以及 git 历史可追溯性。

---

## 0. 盘点结论速览

| 维度 | 发现 |
|------|------|
| docs/ | 19 份 md，引用图清晰；2 份零被引（DEPLOYMENT/PRD），数份 plan 类低引用 |
| docs/superpowers/ | 2 个历史 plan 文件，非 skill，纯残留 |
| 新建 docs 未入库 | 6 份架构文档仍 `??` 未跟踪（ARCH_EXECUTION_PLAN/CODE_ARCHITECTURE/DATA_LAYER_DESIGN/HARNESS_ARCHITECTURE/HARNESS_DESIGN/SCHEME_CONTRACT） |
| reports/ 根 | 9 个一次性周频对比 CSV/JSON（已被 `.gitignore` 忽略，仅本地占用） |
| reports/harness/ | 多个空时间戳目录 + `demo_daily/` 测试残留（已忽略） |
| __pycache__ | 21 处（已忽略） |
| legacy_*.py | 10Y(2452行)被 predictors **活引用，不可删**；5Y(443)/7Y(583) 为**纯归档死代码** |
| scripts/ | 8 个全部被文档/部署引用，无纯孤儿；部分为一次性调试脚本 |
| 大体量产物 | backtest_artifacts 212M / benchmarks 19M / reports 11M（前者已忽略） |

> 关键纠正：5Y/7Y 的 `predictors.py` 是**独立重写算法**，不 import 各自 legacy；10Y 的 predictors **真复用** legacy 算法。故三个 legacy 文件处置策略不同。

---

## A 级 — 可立即清理（零风险，纯产物/残留）

这些不含源码、已被忽略或可无损重建，删除不影响任何运行路径与 git 历史。

| # | 对象 | 依据 | 验证 | 操作 |
|---|------|------|------|------|
| A1 | 全部 `__pycache__/`（21 处） | 编译缓存，自动重建，已 `.gitignore` | `find . -name __pycache__` 删后重跑测试仍绿 | `find . -name __pycache__ -not -path './.git/*' -exec rm -rf {} +` |
| A2 | `reports/harness/` 下空时间戳目录 + `demo_daily/` | onboard/单gate 提前退出留下的空目录 + 测试残留；已忽略 | `find reports/harness -type d -empty`；`demo_daily` 非真实方案 | 删空目录与 `reports/harness/demo_daily/` |
| A3 | `reports/` 根下 9 个一次性对比 CSV/JSON | 历史周频调试对比产物，已 `.gitignore` 忽略，非代码依赖 | 文件名均为 `weekly_*_diff/summary`，无代码读取它们 | 本地删除（不影响仓库，已忽略） |
| A4 | `docs/superpowers/plans/*.md`（2 个） | 历史 plan 残留，非 skill，无文档引用 | `rg "superpowers" docs` 仅自身 | 删 `docs/superpowers/` 整目录 |

> A3/A4 若你想保留历史痕迹，可移到本地仓库外备份目录而非彻底删。

---

## B 级 — 需你确认后清理（涉及源码或归档，删除有取舍）

### B1 — 5Y / 7Y legacy 归档死代码

| 对象 | `schemes/weekly_5y_direct_production/core/legacy_weekly_5y_direct_production_0529.py`（443行）、`schemes/weekly_7y_cross_d_overlay/core/legacy_weekly_7y_cross_d_overlay_0529.py`（583行） |
|------|------|
| 依据 | 经 rg 确认：两方案 `predictors.py` 为独立重写算法，**不 import** 各自 legacy；backtest runner 也不引用 |
| 取舍 | 删 → 减 1026 行死代码，但失去"算法来源原始脚本"审计留档 |
| 验证（删前必跑） | `rg -l "legacy_weekly_5y\|legacy_weekly_7y" --type py`（排除自身）为空；删后跑 weekly_5y/7y 的 dry-run + `--no-persist` 回测，等价闸 diff_count=0 |
| 风险 | 低（确认无引用后）。**但建议保留**——见 C2 的折中方案 |
| 决策点 | **删除** vs **保留为审计档** vs **移到 `docs/legacy_sources/` 归档** |

### B2 — 一次性调试脚本归档

| 对象 | 历史周频/日频差异排查的一次性脚本（doc_refs=1~2） |
|------|------|
| 依据 | 完成历史使命的调试脚本，非 harness/scheduler 运行路径依赖 |
| 取舍 | 它们仍被文档引用（验证记录里提到过），直接删会留下文档死链 |
| 操作建议 | **不删，归档**：移到 `scripts/archive/` 并在 SOP/STATUS 注明"历史调试脚本"，或保留原位加注释 |
| 风险 | 低，但需同步修文档引用 |
| 决策点 | 归档 vs 原位保留 |

### B3 — 低引用 plan 类文档整合

| 对象 | `docs/MIGRATION_PLAN.md`（1ref）、`docs/RESEARCH.md`（1ref）、`docs/IMPLEMENTATION_PLAN.md`（2ref，Phase1-11历史记录） |
|------|------|
| 依据 | 内容多为已完成阶段的历史记录，与 CURRENT_STATUS / ARCH_EXECUTION_PLAN 有时间重叠 |
| 取舍 | 删 → 减文档噪音；但它们是项目演进的历史档案，有追溯价值 |
| 操作建议 | **不删**：移到 `docs/archive/` 子目录，主 docs/ 只留"当前有效"文档；或保留并在 README 文档索引里标注"历史" |
| 风险 | 低。注意修引用它们的文档链接 |
| 决策点 | 归档到 `docs/archive/` vs 保留 |

---

## C 级 — 建议保留（看似冗余实则有用，勿删）

| # | 对象 | 为何保留 |
|---|------|----------|
| C1 | `schemes/weekly_10y_d_overlay/core/legacy_weekly_10y_d_overlay_0529.py`（2452行） | **被 `predictors.py` 活引用**（运行时 monkey-patch 注入 DataFrame）。删除即破坏 10Y 算法。**绝不可删** |
| C2 | 全部 legacy_*.py 作为"算法来源档" | 即便 5Y/7Y 无引用，它们是 SOP Normalize 步要求的"原始脚本归档"，是方案可追溯性的一部分。**折中：统一移到各方案 `core/` 内但加 `# ARCHIVED, not imported` 注释**，或集中到 `docs/legacy_sources/` |
| C3 | `scripts/` 中有文档/部署引用的脚本（apply_migrations/audit_daily/compare_refactor_outputs 等） | 部署、数据审计、等价闸工具——都是活路径或被 harness/SOP 依赖 |
| C4 | `benchmarks/`（19M） | canonical 历史基准 CSV，回测复现的金标准输入，被 backtest runner 读取 |
| C5 | 零被引的 `DEPLOYMENT.md` / `PRD.md` | 虽无文档互链，但 DEPLOYMENT 是运维手册、PRD 是需求基线，属"入口型"文档，应在 README 文档索引补上链接而非删除 |

---

## D 级 — 必须先做的"反向操作"（不是删，是补）

清理前有几件**遗漏的入库**该先处理，否则会丢失本轮成果：

| # | 操作 | 依据 |
|---|------|------|
| D1 | `git add` 6 份未跟踪架构文档（ARCH_EXECUTION_PLAN/CODE_ARCHITECTURE/DATA_LAYER_DESIGN/HARNESS_ARCHITECTURE/HARNESS_DESIGN/SCHEME_CONTRACT）并提交 | 它们是本轮架构设计核心产物，仍 `??` 未跟踪，有丢失风险 |
| D2 | 提交 4 份已改 docs（ARCHITECTURE/CURRENT_STATUS/SCHEME_ONBOARDING_SOP/TEST_PLAN）的待提交修订 | 含 V1-V4 进度回写、harness 对齐，不提交则与代码状态脱节 |
| D3 | 确认 `frontend/` 3 个脏文件改动是否要保留 | `aifin-shell.{css,js}` / `index.html` 有未提交改动，与本轮架构无关，需你确认归属 |

> D 类优先于一切删除：**先固化成果，再清理冗余**。

---

## 执行顺序建议

```
1. D（先固化）：git add+commit 6 份新 docs + 4 份改 docs；确认 frontend 脏改归属
2. A（零风险）：清 __pycache__ / 空 harness 目录 / demo 残留 / superpowers 残留 / reports 散落产物
3. 文档索引：README 补 DEPLOYMENT/PRD 等入口文档链接（C5），消除"零引用"假象
4. B（按决策）：5Y/7Y legacy 与 plan 文档——归档到 docs/archive/ 或 docs/legacy_sources/（推荐归档而非删）
5. 收尾：全量分环境测试 + harness onboard --stage all 复跑，确认清理无回归
```

---

## 一句话结论

**真正能无脑删的只有 A 级（缓存/空目录/已忽略产物/superpowers 残留）。** B 级（legacy 死代码、调试脚本、历史 plan）**建议归档而非删除**——它们是方案可追溯性与项目演进的档案。C 级看似冗余实则是活依赖或入口文档，勿删。**且清理前务必先做 D：把 6 份未入库的架构文档提交，避免丢失本轮成果。**

> 本文为清理规划。未删除或移动任何文件。

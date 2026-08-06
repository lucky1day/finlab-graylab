# D0 文档生命周期审计（2026-08-06）

**文档状态**：`HISTORICAL`

**范围**：本审计只覆盖当前开发分支中已跟踪的 `docs/` 文档及其仓库内入站引用；不读取或修改
用户未提交的 `KNOWN_ISSUES`、`.superpowers/`、`reports/operations/` 或诊断脚本。经用户逐项确认后，本记录所列
两份 `SUPERSEDED_DRAFT` 已在 2026-08-06 的独立文档清理批次中删除；任何历史证据均未被移动或改写。

## 结论

- 当前没有可无条件删除的文档。关闭的 G3.1、Native maintenance、Blackbox onboarding、系统检查和审计记录仍是
  可复核的历史证据，不能因对应工作已闭环而直接删除。
- 经用户确认，2 份 `SUPERSEDED_DRAFT` 已按精确清单删除：排除本审计记录为列举候选而产生的自引用后，design
  仅由配套 plan 入站引用，plan 没有非自身的入站引用，且实施 checkbox 已全部完成；删除前已确认其现行行为与审计价值
  均由前端测试、产品/状态文档和 Git 历史保留。
- 其余 `HISTORICAL` 文档不是当前规则，但仍由目录索引、测试、代码注释、产品/运维入口或唯一验收证据引用，继续保留。

## 审计方法与分类规则

1. 以 `git ls-files -- docs` 枚举已跟踪文档，以文档头部状态和
   [文档中心](../../README.md)的状态规则确定当前/历史边界。
2. 以 `rg -l -F <filename>` 在仓库内检索入站引用，排除 `.git/`、`.worktrees/` 和
   `reports/`。对本报告中为审计而列举的候选文件，另行排除本报告自身的候选列举引用；仅“零入站引用”不构成删除许可。
3. 只有同时满足“已被现行入口或可复核记录完整替代、没有唯一审计价值、没有文档测试/代码引用、且用户确认”的
   文件才可从工作树删除。Git 历史不能替代仍被治理或验收使用的证据。

## 分类矩阵

| 范围 | 分类 | 结论与替代入口 |
|---|---|---|
| `docs/README.md`、`docs/TODO.md`、`docs/CURRENT_STATUS.md`、现行架构/SOP/入库文档 | `CURRENT` / `CURRENT INDEX` | 保留；分别是唯一总入口、未完成工作排序、已验证事实和当前规则。 |
| `docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md` | `HISTORICAL EVIDENCE` | 保留；它是 G3.1 exact admission、gap-fill 与读回的带日期闭环证据，当前入口只摘要其结论。 |
| `docs/records/status/KNOWN_ISSUES_HANDOFF_20260802.md` | `CURRENT` | 保留且不触碰；除当前状态外，它在主工作区有用户修改。 |
| `docs/blackbox_v2/records/**`、`docs/records/audits/**`、`docs/records/system-checks/**` | `HISTORICAL EVIDENCE` | 保留；目录索引、验收记录、截图/JSON 证据或测试仍需它们。 |
| `docs/architecture/DAILY_SIGNAL_SLA.md`、`docs/internal/**`、历史产品/运维说明 | `HISTORICAL` / `LEGACY_MAINTENANCE` | 保留；虽不定义当前生产规则，仍由架构/产品/运维索引、代码边界或文档测试引用。 |
| `docs/superpowers/*/2026-08-02-weekly-10y-d-overlay-stable-order-*` | `HISTORICAL EVIDENCE` | 保留；`KNOWN_ISSUES`、测试背景和 Native core 注释仍引用其设计。 |
| `docs/superpowers/*/2026-08-04-*-native-*-validation-*`、`docs/superpowers/plans/2026-08-04-native-maintenance-g4.md` | `HISTORICAL EVIDENCE` | 保留；G4 plan 仍有 4 个未勾选的受控 activation/gap-fill/readback 步骤，且设计仍由该计划引用。 |
| `docs/superpowers/*/2026-08-06-task-cell-red-accuracy-*` | `HISTORICAL EVIDENCE` | 暂时保留；它记录用户选择的红色数值语义和 immutable CSS 内容摘要交付契约，现行计划链接其设计。 |

## 已确认并执行的删除（2026-08-06）

| 已删除文件 | 删除前入站引用审计 | 执行结论 |
|---|---|---|
| `docs/superpowers/specs/2026-08-05-factor-lab-live-backtest-cutover-design.md` | 排除本审计报告的候选列举后，唯一入站引用来自配套实施计划。 | 已确认 target-date cutover 的现行行为由前端测试与产品/状态文档覆盖；删除后由 Git 历史追溯。 |
| `docs/superpowers/plans/2026-08-05-factor-lab-live-backtest-cutover.md` | 排除本审计报告的候选列举与自身文本后，没有非自身入站引用；它只向配套设计出站链接，任务均已标记完成。 | 已确认没有独立的上线/回滚审计价值；删除后由 Git 历史追溯。 |

本批次没有删除任何其它文档；G3.1、D1、G7/G8、当前规范、状态记录和所有仍具唯一审计价值的证据均保留。

## 后续闸门

1. 用户已逐项确认上述 2 份候选进入删除批次；没有把该确认外推到其它文档。
2. 删除前已完成逐份内容映射和再次入站引用检查；仅删除获批文件，并同步状态索引、当前状态、TODO 和本审计记录。
3. 删除批次独立提交，并运行文档测试与 `git diff --check`；不与 G7/G8、D1、前端或生产操作混合。

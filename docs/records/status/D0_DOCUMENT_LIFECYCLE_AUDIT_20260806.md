# D0 文档生命周期审计（2026-08-06）

**文档状态**：`HISTORICAL`

**范围**：本审计只覆盖当前开发分支中已跟踪的 `docs/` 文档及其仓库内入站引用；不读取或修改
用户未提交的 `KNOWN_ISSUES`、`.superpowers/`、`reports/operations/` 或诊断脚本。本次不删除、移动或改写
任何历史证据。

## 结论

- 当前没有可无条件删除的文档。关闭的 G3.1、Native maintenance、Blackbox onboarding、系统检查和审计记录仍是
  可复核的历史证据，不能因对应工作已闭环而直接删除。
- 已识别 2 份**待用户确认的 `SUPERSEDED_DRAFT` 删除候选**：它们除彼此之间外没有仓库内入站引用，且实施 checkbox
  已全部完成；但删除前仍须逐份确认其内容没有仅存于该计划/设计中的审计价值。
- 其余 `HISTORICAL` 文档不是当前规则，但仍由目录索引、测试、代码注释、产品/运维入口或唯一验收证据引用，继续保留。

## 审计方法与分类规则

1. 以 `rg --files docs` 枚举已跟踪文档，以文档头部状态和
   [文档中心](../../README.md)的状态规则确定当前/历史边界。
2. 以 `rg -l -F <filename>` 在仓库内检索入站引用，排除 `.git/`、`.worktrees/` 和
   `reports/`；仅“零入站引用”不构成删除许可。
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

## 待确认删除候选（不执行删除）

| 文件 | 入站引用审计 | 当前判断 | 删除前必须补齐的确认 |
|---|---|---|---|
| `docs/superpowers/specs/2026-08-05-factor-lab-live-backtest-cutover-design.md` | 仅由配套实施计划引用 | `SUPERSEDED_DRAFT` 候选 | 确认 target-date cutover 的现行行为已完整由前端测试与产品/状态文档覆盖。 |
| `docs/superpowers/plans/2026-08-05-factor-lab-live-backtest-cutover.md` | 仅引用配套设计；任务均已标记完成 | `SUPERSEDED_DRAFT` 候选 | 确认它没有独立的上线/回滚审计价值。 |

## 后续闸门

1. 由用户逐项确认上述 2 份候选是否允许进入删除批次；未确认即保持不动。
2. 若获确认，先做逐份内容映射和再次入站引用检查，再只删除获批文件，并同步 `docs/README.md`、目录 README
   和链接。
3. 删除批次须独立提交，并运行文档测试与 `git diff --check`；不得与 G7/G8、D1、前端或生产操作混合。

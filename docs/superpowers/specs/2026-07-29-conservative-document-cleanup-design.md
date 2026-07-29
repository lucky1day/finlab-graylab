# 仓库文档保守清理设计

**文档状态**：`CURRENT`

**核验日期**：2026-07-29

## 目标

第一阶段只删除已经退出现行阅读路径、且可由 Git 历史完整追溯的兼容页和
archive 副本，同时修正所有入口、索引和文档门禁。该阶段不重写现行规则，
不合并审计记录，也不判断尚有业务价值的历史设计是否应该删除。

## 删除范围

本阶段精确删除以下 12 份 Markdown：

- `docs/archive/README.md`
- `docs/archive/SCHEME_PARADIGM.md`
- `docs/blackbox_v2/archive/README.md`
- `docs/blackbox_v2/archive/UPSTREAM_DELIVERY_SOP_EXCEL_DRAFT.md`
- `docs/native_v1/archive/README.md`
- `docs/native_v1/archive/SCHEME_ONBOARDING_SOP_PRE_FREEZE.md`
- `docs/native_v1/archive/SCHEME_ONBOARDING_T0_PRE_FREEZE.md`
- `docs/native_v1/archive/SCHEME_PARADIGM_DRAFT.md`
- `docs/native_v1/archive/SCHEME_POST_ONBOARDING_TEST_SOP_PRE_FREEZE.md`
- `docs/sop/SCHEME_ONBOARDING_SOP.md`
- `docs/sop/SCHEME_ONBOARDING_T0.md`
- `docs/sop/SCHEME_POST_ONBOARDING_TEST_SOP.md`

删除理由：

1. 三个 `docs/sop/SCHEME_*` 文件只提供历史跳转，不定义当前操作规则。
2. `docs/native_v1/archive/` 保存的是冻结前新增 Native 方案流程；当前政策只允许
   清单内存量维护，现行入口已经由 Native 维护 T0、SOP 和测试 SOP 覆盖。
3. `docs/archive/` 只为旧 Native 目标态草案提供第二层跳转。
4. `docs/blackbox_v2/archive/` 是已经被 Contract 1.0 两文件交付取代的 Excel
   工程包草案；现行上游和平台 SOP 已完整覆盖当前规则。

## 同步修正

删除文件时同步修改：

- `docs/README.md`
- `docs/sop/README.md`
- `docs/native_v1/README.md`
- `docs/blackbox_v2/README.md`
- 仍引用上述历史路径的现行架构、SOP、状态或索引
- `tests/test_onboarding_docs.py`

文档门禁不再要求历史兼容页存在，改为验证：

- 新方案入口只指向 Blackbox V2；
- Native 入口只指向存量维护文档；
- `docs/sop/` 的索引覆盖全部现存 Markdown；
- 被删除的历史路径不存在；
- 所有 Markdown 相对链接可解析。

## 明确不处理

本阶段不删除或重写：

- `docs/records/` 及 Blackbox 验收记录、JSON、截图；
- `docs/internal/`；
- 现有 `docs/superpowers/` 设计和计划；
- `CURRENT_STATUS.md`、`TODO.md` 和日频恢复 handoff；
- 现行架构、契约、SOP、产品手册和运维手册；
- `PRODUCTION_READINESS.md`、历史 PRD 或云环境记录。

这些内容留待下一阶段逐类评估，不能因为标记为 `HISTORICAL` 就在本阶段删除。

## 验证

至少执行：

```bash
python -m unittest tests.test_onboarding_docs
git diff --check
```

随后运行完整测试和 `compileall`。只有全部通过、文档引用图无断链、
`AGENTS.md` 与 `CLAUDE.md` 保持一致，才提交并推送当前开发分支。

## Git 边界

- 保留当前未提交的日频恢复 handoff 修改，不把它混入本设计提交。
- 清理实现使用独立提交。
- 只推送 `codex/audit-bugfixes-20260613`。
- 不合并、覆盖或推送 `master`。

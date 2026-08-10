# 权威文档收敛与 Harness 评审设计

**状态：** 用户已批准执行（2026-08-10）。本文件只服务本次实施，实施完成后从工作树删除。

## 目标

将仓库文档收敛为当前规则、当前状态、当前待办和必要操作手册四类；已实施计划、已关闭交接、被后续规则替代的决策草案只保留在 Git 历史。随后对 `harness/` 做只读结构评审，不在本次顺带删除 Harness 代码。

## 权威边界

- `AGENTS.md` 与 `CLAUDE.md`：根工程约束，必须字节一致。
- `docs/architecture/`、`docs/onboarding/`、`docs/sop/`：当前规则与操作入口。
- `docs/CURRENT_STATUS.md`：只写当前事实，不冻结易漂移的 Git SHA、方案数量或一次性 run。
- `docs/TODO.md`：只写未批准或未完成的下一步，不保存关闭清单。
- `docs/records/SCHEME_ISSUE_LEDGER.md`：唯一当前问题入口；无问题时明确为空。
- `docs/superpowers/` 与 `docs/records/status/`：本轮过程文件完成后全部删除，历史由 Git 追溯。

## 删除范围

删除当前全部 5 份 `docs/superpowers` 设计/计划、`docs/records/status` 的 4 份历史/草案及索引。Weekly 10Y 稳定排序的代码注释与测试说明改成自包含根因描述，不继续依赖过程文档路径。

## 保留与更新范围

保留 Native legacy maintenance 文档、Blackbox/Native 契约、当前架构/SOP、产品与运维手册。`PRODUCTION_READINESS.md` 保留其独立检查清单职责，压缩为 CURRENT 且不记录历史 rollout。所有当前索引、状态、待办和相对链接同步更新。

## 发布与评审

文档门禁、链接检查、静态冲突搜索和全量回归通过后，在开发分支提交。`master` 是开发分支祖先，因此只允许 fast-forward；两个分支推送并精确读回远程 SHA。最后只读评审 `harness/` 的职责、调用者、重复门禁、授权、哈希与 fallback，输出问题、根因和最小方案，不实施该评审建议。

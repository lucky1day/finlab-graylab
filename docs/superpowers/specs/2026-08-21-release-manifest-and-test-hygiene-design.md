# Release Manifest And Test Hygiene Design

## 目标

删除源码 release manifest 中没有参与真实性判断的 Git `tree` 字段，并让 release 测试在
teardown 后把临时 release 目录恢复为 owner-writable，避免 pytest 临时目录清理 warning。

## Manifest 最小合同

- manifest schema 升为 `bfl-source-release-v2`，字段精确为
  `schema_version`、`commit`、`root_prefix`、`archive`。
- builder 不再查询 `HEAD^{tree}`，`BuiltSourceRelease` 和 CLI JSON 也不再暴露 `tree`。
- installer 不再要求或格式校验 `tree`；v2 manifest 出现额外 `tree` 字段时按未知字段拒绝。
- release 的真实性继续由三项证据闭合：operator 独立批准的 archive SHA-256、archive pax
  commit 标记与 manifest commit 一致、archive/解包/已安装 source tree SHA-256 一致。
- 不实现 Git tree 重建、双 schema 兼容层或 manifest 迁移工具。既有 v1 archive 使用其自身
  同版本安装器执行复验或回滚，符合现有“archive 同版本 installer”合同。

## 测试临时目录合同

- `tests/test_source_release_tools.py` 增加 module-local autouse fixture；每个测试完成后只遍历
  自己的 `tmp_path`，为其中目录补回 owner-write 权限。
- fixture 只改变 pytest 临时目录，不改变生产安装器的 `0444/0555` 只读策略。
- `tests/test_launchd_release_launcher.py` 已有同等 teardown；不抽象共享 fixture，不扩大作用域。
- 已确认当前 30 条 warning 来自该 fixture 合入前遗留的六个 pytest `garbage-*` 目录；在精确
  核对 owner、路径和内容边界后清理这些临时垃圾，再用隔离 basetemp 连续运行验证不再产生。

## 文档规则

- `deploy/README.md` 只描述真实校验链，不再写 commit/tree manifest 或 archive/tree 校验。
- `docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md` 的发布步骤只固定 commit 和 archive
  SHA-256，并明确 source digest 校验。
- `docs/CURRENT_STATUS.md` 只记录该能力进入 `codex/develop` 候选，不能声称 Mac3/ECS 已部署。
- 完成后删除本设计与实施计划；长期规则只留在上述 authority 文档。

## 明确不做

- 不修改 production release 权限。
- 不修改安装/激活 CAS、systemd、launchd、数据库或 Registry。
- 不加入 Git tree 计算算法、兼容 adapter、后台清理服务或新的测试框架。

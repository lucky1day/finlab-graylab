# Native 二级输入 Generation 控制面退役实施计划

## 目标

将运行时输入权威收敛为两条链路：Native 只从当前权威 `bond_db`
按 `feature_date` 截止构建输入；Blackbox 继续使用 DataBridge current
snapshot/replay。删除 Native 和旧 DataBridge sealed generation 控制面，不新增
fallback、双读、自动重建或兼容执行路径。

## 实施顺序

1. 以测试锁定新 Executor、Signal Gap v7、Phase-A manifest 和 Repository 契约。
2. 删除 Native/旧 DataBridge generation 上下文、环境变量和平台输入分支。
3. Signal Gap Native action 使用 `input_authority=null`，Blackbox 继续严格绑定
   DataBridge authority；v6 及更早计划 fail-closed。
4. Phase-A manifest v3 固定 `native_generation=null`、
   `native_generation_changed=false`，删除 bind/rebind。
5. 删除 Repository 无调用者的 generation runtime API，保留表、migration 和历史行。
6. 更新当前架构与 Onboarding 文档，运行聚焦、架构和全量测试后推送
   `codex/audit-bugfixes-20260613`。

## 验收边界

- 普通 Native 与 9 个 source-backed Native 使用同一 `bond_db`；后者继续使用
  SELECT-only 身份，不改变账号、权限或连接配置。
- 保留 Blackbox DataBridge generation/snapshot/replay 和 opaque launchd token。
- 不执行真实补缺、DDL、数据库写入、launchctl、plist 或服务操作。
- 不更新 `master`，不处理 Native `inference.py` hash 或 Phase-A
  `NON_PRODUCTION` 生产接受语义。

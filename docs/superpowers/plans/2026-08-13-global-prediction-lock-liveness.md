# 全局预测锁存活性实施计划

> 规格：`docs/superpowers/specs/2026-08-13-global-prediction-lock-liveness-design.md`

1. 增加真实文件锁并发回归测试，证明第二个 cadence 不能在首个锁释放前进入。
   验证：测试在现有非阻塞实现上失败。
2. 将 `_runner_lock` 收敛为阻塞式全局互斥，并删除 `lock_conflict` 分支及布尔上下文契约。
   验证：新增回归测试与 runner 相关测试通过，源码中无 `LOCK_NB`/`lock_conflict`。
3. 执行静态检查、定向测试和完整测试套件。
   验证：`git diff --check`、`compileall`、定向 pytest、完整 pytest 均为退出码 0。
4. 合并至 `codex/audit-bugfixes-20260613` 并普通推送，回读远程 SHA；以真实提交与测试证据关闭 #43/#44。
   验证：远程开发分支 SHA 等于本地合并提交，两个 issue 均为 closed。

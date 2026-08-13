# 全局预测锁存活性设计

## 范围

本阶段只根治 GitHub #43 与 #44：当 daily、weekly、monthly 的 launchd one-shot 批次重叠时，后启动的批次不得因锁竞争直接退出并永久丢失。

不修改 launchd 触发时刻、方案超时、候选发现、执行顺序、数据库写入规则或任何业务算法。

## 根因

三个 cadence 共用 `predictions.lock` 是正确的安全边界，但当前使用 `LOCK_EX | LOCK_NB`。竞争者收到 `BlockingIOError` 后返回 `lock_conflict`，launchd 本次自然触发因此结束且没有补偿入口。daily 最坏 12 小时执行窗口与 monthly 18:00 触发存在确定重叠区间，所以这不是偶发性能问题，而是批次存活性错误。

## 设计

- 继续使用一个跨 cadence 的全局文件锁，保证任意时刻只有一个 generic prediction writer。
- 改用阻塞式 `LOCK_EX`。竞争者停留在内核锁等待中，前一批次释放后进入原有执行路径。
- `_runner_lock` 不再返回布尔令牌；成功进入上下文即表示已持锁。
- 删除 `lock_conflict` 终态和提前返回路径。
- 正常退出在 `finally` 中解锁；进程异常终止时由操作系统关闭文件描述符并释放锁。

不增加 deadline、重试、应用层队列、观察者、补偿任务、分 cadence 锁或兼容 fallback。

## 不变量与验收

1. 互斥安全：同一时刻只有一个批次进入锁内执行区。
2. 批次存活：第二个 cadence 在锁被占用时等待，释放后继续，而不是返回 `lock_conflict`。
3. 范围稳定：锁外的调度和业务逻辑不变。
4. 验证：真实文件锁并发回归测试、runner 相关定向测试和全量 pytest 全部通过。

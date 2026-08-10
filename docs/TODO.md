# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-10

本文只列当前尚未批准实施的工作。当前事实见[当前状态](CURRENT_STATUS.md)，生产规则见[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成工作通过 Git 和控制面审计追溯，不在这里保存关闭清单。

## 当前队列

| 顺序 | 工作流 | 当前状态 | 下一步 |
|---|---|---|---|
| 1 | Blackbox 生命周期隐式恢复退役 | `REVIEW_READY` | 审阅是否删除 activate/shadow-register 的授权前自动 reconciliation；发现未完成 journal 时直接阻断，只允许显式、专项授权的 lifecycle-reconcile 修改状态 |
| 2 | Harness 单日信号补缺控制面收敛 | `REVIEW_READY` | 审阅是否删除 canonical/range scope、自签名 token 与重复 plan-hash 重放，只保留单日 active live 缺口、Blackbox DataBridge authority 和 insert-only 写入 |
| 3 | Harness 授权模式收敛 | `REVIEW_READY` | 审阅是否删除无密钥明文 token、旧无信封兼容和无调用者异常类型；保留副作用操作的唯一 HMAC 模式，单日补缺不再自签自验 |
| 4 | Harness API Gate 统一 | `REVIEW_READY` | 审阅是否移除 runtime-specific `api-readiness` 与旧分项 API Gate，改为激活前生命周期检查 + 激活后统一 dashboard 验收 |
| 5 | Harness 死路径与配置读取清理 | `REVIEW_READY` | 审阅是否删除无文档/无测试调用的 Blackbox certification bootstrap，并合并重复 YAML 子集解析器、去掉 CompareGate 配置读取 fail-open |

## 评审边界

- 评审阶段只读，不修改 Harness、数据库、Backend、前端、plist、launchd 或服务。
- 不因文件较大就拆分，不因极小概率事件增加 fallback、兼容层、重试、第二控制面或额外 hash。
- 只有能证明业务职责已经重复、没有调用者、被现行规则替代或妨碍错误直接暴露的设计，才进入删除候选。
- 任一实施建议都必须独立获得用户确认，并以可复现现象和聚焦回归证明删除没有放宽 Gate、授权、insert-only、生命周期或输入截止约束。

## 统一停止条件

- 需要改变算法逻辑、生产调度、数据库 schema、installed plist、服务状态或业务数据；
- 需要用 fallback、旧版本双读或隐式兼容才能让测试通过；
- 发现当前文档、代码、数据库或现场状态与评审前提不一致。

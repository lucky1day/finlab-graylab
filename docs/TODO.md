# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-20

本文只列当前尚待推进或尚需独立生产授权的工作。当前事实见[当前状态](CURRENT_STATUS.md)，生产规则见[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成工作通过 Git 和控制面审计追溯，不在这里保存关闭清单。

## 当前队列

当前部署迁移、ECS immutable prediction release 晋级、Native active completion 隔离 MySQL 三态及
共享 repository 写入核心验收已经闭环，不再作为待办；Blackbox 入口本次由本地完整回归覆盖，不表述为
已单独真库调用。剩余工作均为新的开发或未来生产项目：

1. 安排独立 Mac3 夜间生产窗口，把 Mac3 从 `e692285d47e41c384dc915758abe0c51f9ac3aaf`
   晋级到 ECS 已验证的同一份 `5c5603a23266e563e142e319d4e5d13907649598` archive，使生产 Writer
   取得 insert-only prediction completion。该操作须先用 ECS 已验证的同一 archive 完成 Mac3 只读
   预检、预安装和候选核验，再以 CAS 更新 `current`；不替换 installed plist、调度或环境合同，只重启
   常驻 Backend，后续 prediction one-shot 自然从新 `current` 启动。操作必须避开运行批次并读回
   launchd、Backend、release identity 与数据库状态，不改变域名、数据库 authority 或 Writer 所属主机。
2. 开发线可以立即并行开始：先确认前端变更清单和验收口径；两个新方案各自接收独立的
   Blackbox V2 两文件交付。前端先走 ECS 灰度 release；两个方案分别走 Intake/Gate/ECS-only
   activation，不把两个方案绑成一个提交、发布或回滚单元。
3. 若未来决定把生产域名或 Writer 从 Mac3 切到 ECS，必须作为新的生产项目重新设计 Web、数据库
   authority、单 Writer、DNS/Nginx、切换窗口和回滚范围，不从本次灰度或 R2 验收外推授权。

ECS 继续独立灰度运行。Mac3 域名、Nginx、DNS、数据库 authority 和生产 Writer 均保持不变；是否
未来切到 ECS 是新的生产项目，不是本轮双主机代码治理闭环的前提。

## 评审边界

- 后续 release 更新仍须避开正在运行的生产批次，并分别授权 installed plist、loaded state、服务和
  数据库操作。
- 不因文件较大就拆分，不因极小概率事件增加 fallback、兼容层、重试、第二控制面或额外 hash。
- 只有能证明业务职责已经重复、没有调用者、被现行规则替代或妨碍错误直接暴露的设计，才进入删除候选。
- 任一实施建议都必须独立获得用户确认，并以可复现现象和聚焦回归证明删除没有放宽 Gate、授权、insert-only、生命周期或输入截止约束。

## 统一停止条件

- 需要改变算法逻辑、生产调度、数据库 schema、installed plist、服务状态或业务数据；
- 需要用 fallback、旧版本双读或隐式兼容才能让测试通过；
- 发现当前文档、代码、数据库或现场状态与评审前提不一致。

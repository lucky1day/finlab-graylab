# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-20

本文只列当前尚待推进或尚需独立生产授权的工作。当前事实见[当前状态](CURRENT_STATUS.md)，生产规则见[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成工作通过 Git 和控制面审计追溯，不在这里保存关闭清单。

## 当前队列

当前顺序固定为：

1. 立即开始前端和两个新方案开发，不等待夜间窗口；前端先走 ECS 灰度 release，
   两个方案分别走 Blackbox V2 Intake/Gate/ECS-only activation，不把两个方案绑成一个回滚单元。
2. 在不与 06:30 DataBridge、07:03 daily、08:30/19:00/23:45 Actuals、周/月批次重叠的夜间窗口，
   另行授权执行 R1 预安装/激活、六个应用 plist 与一个 SSH tunnel plist 切换、Backend 重启、七个
   loaded state 的只读健康检查和回滚读回；tunnel 必须保留真实 key/user，短暂重连只在窗口内执行。
3. 现场确认所有应用进程均从 immutable `current` 运行、tunnel 日志已外置后，保留未跟踪文件并把
   Mac3 Git 开发根切到 `codex/develop`；不得在生产解耦前先切分支。

ECS 继续独立灰度运行。Mac3 域名、Nginx、DNS、数据库 authority 和生产 Writer 均保持不变；是否
未来切到 ECS 是新的生产项目，不是本轮双主机代码治理闭环的前提。

## 评审边界

- 等待 R1 夜间窗口期间不得修改 installed plist、launchd loaded state、数据库、Backend 进程或
  生产任务。
- 不因文件较大就拆分，不因极小概率事件增加 fallback、兼容层、重试、第二控制面或额外 hash。
- 只有能证明业务职责已经重复、没有调用者、被现行规则替代或妨碍错误直接暴露的设计，才进入删除候选。
- 任一实施建议都必须独立获得用户确认，并以可复现现象和聚焦回归证明删除没有放宽 Gate、授权、insert-only、生命周期或输入截止约束。

## 统一停止条件

- 需要改变算法逻辑、生产调度、数据库 schema、installed plist、服务状态或业务数据；
- 需要用 fallback、旧版本双读或隐式兼容才能让测试通过；
- 发现当前文档、代码、数据库或现场状态与评审前提不一致。

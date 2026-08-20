# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-20

本文只列当前尚待推进或尚需独立生产授权的工作。当前事实见[当前状态](CURRENT_STATUS.md)，生产规则见[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成工作通过 Git 和控制面审计追溯，不在这里保存关闭清单。

## 当前队列

当前分为两条可并行推进的工作线：

1. 开发线立即开始，不等待 Mac3 窗口：先确认前端变更清单和验收口径；两个新方案各自接收独立的
   Blackbox V2 两文件交付。前端先走 ECS 灰度 release；两个方案分别走 Intake/Gate/ECS-only
   activation，不把两个方案绑成一个提交、发布或回滚单元。
2. 部署线先实现并冻结 R2：launcher 从外置 runtime `config/service.env` 加载 Mac3 本机配置，经过权限、
   所有权、语法、保留键和冲突校验；audit 只报告变量名/缺失，不输出值。首次迁移从现有生产 `.env`
   复制一次并复用现有 admin token，之后开发 `.env` 与生产 `service.env` 永久独立、永不自动同步；
   不增加 watcher、热加载、自动 token 轮换或第二套配置控制面。精确 R2 先完成 ECS source/hash、Backend、
   DataBridge 和 systemd identity 读回，不得用后续 develop HEAD 替代。
3. 在不与 06:30 DataBridge、07:03 daily、08:30/19:00/23:45 Actuals、周/月批次重叠的 Mac3 窗口，
   另行授权执行同一 R2 的预安装/激活、六个应用 plist 与一个 SSH tunnel
   plist 切换、Backend 重启、七个 loaded state 的只读健康检查和回滚读回；tunnel 必须保留真实
   key/user，短暂重连只在窗口内执行。
4. 现场确认所有应用进程均从 immutable `current` 运行、tunnel 日志已外置后，保留未跟踪文件并把
   Mac3 Git 开发根切到 `codex/develop`；不得在生产解耦前先切分支。

ECS 继续独立灰度运行。Mac3 域名、Nginx、DNS、数据库 authority 和生产 Writer 均保持不变；是否
未来切到 ECS 是新的生产项目，不是本轮双主机代码治理闭环的前提。

## 评审边界

- 等待 R2 窗口期间不得修改 installed plist、launchd loaded state、数据库、Backend 进程或
  生产任务。
- 不因文件较大就拆分，不因极小概率事件增加 fallback、兼容层、重试、第二控制面或额外 hash。
- 只有能证明业务职责已经重复、没有调用者、被现行规则替代或妨碍错误直接暴露的设计，才进入删除候选。
- 任一实施建议都必须独立获得用户确认，并以可复现现象和聚焦回归证明删除没有放宽 Gate、授权、insert-only、生命周期或输入截止约束。

## 统一停止条件

- 需要改变算法逻辑、生产调度、数据库 schema、installed plist、服务状态或业务数据；
- 需要用 fallback、旧版本双读或隐式兼容才能让测试通过；
- 发现当前文档、代码、数据库或现场状态与评审前提不一致。

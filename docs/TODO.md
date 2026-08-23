# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-23

本文只列当前尚待推进或尚需独立生产授权的工作。当前事实见[当前状态](CURRENT_STATUS.md)，生产规则见[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成工作通过 Git 和控制面审计追溯，不在这里保存关闭清单。

## 当前队列

本页只保留尚未闭环的观察、诊断与未来生产项目：

1. `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 已完成 ECS 技术 Gate、shadow、
   canonical backtest、activation、`gray_live` 和 DashboardGate，当前状态为 Onboarding Complete。等待
   2026-08-24 07:03 Asia/Shanghai 的首次真实 daily systemd 自然触发；触发后须联合核验 service 日志、
   `scheduled_live` run/prediction 与 Dashboard。不得为提前取得观察证据而 kickstart、覆盖日期或倒签信号。
2. 独立诊断 Mac3 2026-08-21 daily 的 17 个 `execution_failed`。该批次已结束且无残留 runner，
   本次 R4 发布未重跑、未补写、未修改其历史 run；诊断须先按失败原因分组和读取既有 run/log evidence，
   不得把前端发布成功外推为 daily 已恢复，也不得在没有新的业务写入授权时手工重跑。
3. 若未来决定把生产域名或 Writer 从 Mac3 切到 ECS，必须作为新的生产项目重新设计 Web、数据库
   authority、单 Writer、DNS/Nginx、切换窗口和回滚范围，不能从当前灰度或 immutable release 验收外推授权。
4. 独立评估 systemd/launchd runner 的 CLI 日期入口；设计并取得生产授权后替换 ECS installed
   DataBridge、daily、weekly、monthly 四个 unit，执行 `systemctl daemon-reload` 并读回现场状态。
   执行前只读确认 `/run/bond-factor-lab/manual-run.env` 不存在且相关 one-shot 任务 idle，不得从仓库
   模板变更外推现场状态或授权。

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
